"""Worker runtime: claim -> run handler with heartbeat -> fenced completion or failure."""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Connection

from app.core.context import set_request_id
from app.db.engine import dispatcher_tx, tenant_tx
from app.jobs import ledger
from app.jobs.ledger import Claim, StaleLease

log = logging.getLogger("workers")


class RetryableError(Exception):
    """Transient failure (timeout, 429, 5xx). The job backs off and retries."""

    def __init__(self, code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.message, self.retry_after = code, message, retry_after


class PermanentError(Exception):
    """Poison input or a non-retryable provider answer. The job fails without automatic retry."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Cancelled(Exception):
    """Raised by handlers between chunks when cancellation was requested."""


@dataclass
class JobContext:
    claim: Claim

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Tenant-scoped transaction that commits only while this worker still owns the job."""
        with tenant_tx(self.claim.tenant_id) as conn:
            ledger.fence(conn, self.claim)
            yield conn

    def check_cancelled(self) -> None:
        with tenant_tx(self.claim.tenant_id) as conn:
            if ledger.is_cancel_requested(conn, self.claim):
                raise Cancelled()


class _Heartbeat(threading.Thread):
    def __init__(self, claim: Claim, interval: float):
        super().__init__(daemon=True, name=f"heartbeat-{claim.job_id}")
        self.claim, self.interval, self.stop, self.lost = claim, interval, threading.Event(), False

    def run(self) -> None:
        while not self.stop.wait(self.interval):
            try:
                with tenant_tx(self.claim.tenant_id) as conn:
                    if not ledger.heartbeat(conn, self.claim):
                        self.lost = True
                        return
            except Exception:  # noqa: BLE001 - keep beating; the lease covers short outages
                log.warning("heartbeat failed job=%s", self.claim.job_id)


def run_one(kinds: list[str], worker_id: str, heartbeat_seconds: float = ledger.HEARTBEAT_SECONDS) -> bool:
    """Claim and execute one job. Returns False when nothing was due."""
    from workers.registry import HANDLERS

    with dispatcher_tx() as conn:
        claim = ledger.claim_next(conn, kinds, worker_id)
    if claim is None:
        return False
    if claim.correlation_id:
        set_request_id(claim.correlation_id)

    beat = _Heartbeat(claim, heartbeat_seconds)
    beat.start()
    try:
        outcome = HANDLERS[claim.kind](JobContext(claim))
        with tenant_tx(claim.tenant_id) as conn:
            ledger.complete(conn, claim, state=outcome.state, result=outcome.result,
                            error_code=outcome.error_code, error_message=outcome.error_message)  # fmt: skip
        log.info("job done kind=%s job=%s state=%s", claim.kind, claim.job_id, outcome.state)
    except StaleLease:
        log.warning("stale lease discarded kind=%s job=%s", claim.kind, claim.job_id)
    except Cancelled:
        _record(claim, "CANCELLED")
    except RetryableError as exc:
        _record(claim, "FAIL", code=exc.code, message=exc.message, transient=True, retry_after=exc.retry_after)
    except PermanentError as exc:
        _record(claim, "FAIL", code=exc.code, message=exc.message, transient=False)
    except Exception as exc:  # noqa: BLE001 - unknown errors are treated as transient but bounded
        log.exception("job error kind=%s job=%s", claim.kind, claim.job_id)
        _record(claim, "FAIL", code="INTERNAL_ERROR", message=type(exc).__name__, transient=True)
    finally:
        beat.stop.set()
    return True


def _record(
    claim: Claim,
    action: str,
    *,
    code: str = "",
    message: str = "",
    transient: bool = False,
    retry_after: float | None = None,
) -> None:
    try:
        with tenant_tx(claim.tenant_id) as conn:
            if action == "CANCELLED":
                ledger.complete(conn, claim, state="CANCELLED")
            else:
                ledger.fail(conn, claim, error_code=code, error_message=message, transient=transient,
                            retry_after=retry_after)  # fmt: skip
    except StaleLease:
        log.warning("stale lease on failure path job=%s", claim.job_id)
