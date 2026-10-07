"""Durable job ledger with database leases (FR27, spec §9 "Idempotency and recovery").

- Workers are at-least-once. A job is claimed by setting a fresh lease_token and lease_until.
- A worker that loses its lease (crash, pause, network) is fenced: every commit re-checks the token,
  so a stale worker cannot overwrite the outcome of the worker that reclaimed the job.
- Transient failures back off exponentially with jitter (honouring Retry-After) up to max_attempts,
  then the job is FAILED (dead-letter) and stays visible for support. Poison input is not retried.
- One row per (tenant, kind, object, generation): a retry of a finished job is a new generation.

The database clock (now()) is the only clock used for leases and scheduling.
"""

import random
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, and_, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.context import current_request_id
from app.core.logging import redact
from app.db import tables as t

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
BACKOFF_SECONDS = (2, 8, 30, 120, 600)
TERMINAL = ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED")
j, a = t.job, t.job_attempt


def _after(seconds: float):
    """Database now() plus a bound number of seconds."""
    return func.now() + func.make_interval(0, 0, 0, 0, 0, 0, literal(float(seconds)))


class StaleLease(Exception):
    """This worker no longer owns the job; its results must be discarded."""


@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    object_id: uuid.UUID
    generation: int
    attempt_no: int
    lease_token: uuid.UUID
    correlation_id: uuid.UUID | None


def create_job(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    object_id: uuid.UUID,
    generation: int = 1,
    max_attempts: int = 5,
    total: int | None = None,
    created_by: uuid.UUID | None = None,
    source_event_key: str | None = None,
) -> uuid.UUID:
    """Idempotent: returns the existing job for the same (tenant, kind, object, generation)."""
    job_id = conn.execute(
        insert(j)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            kind=kind,
            object_id=object_id,
            generation=generation,
            max_attempts=max_attempts,
            total=total,
            created_by=created_by,
            source_event_key=source_event_key,
            correlation_id=current_request_id(),
        )  # fmt: skip
        .on_conflict_do_nothing(index_elements=["tenant_id", "kind", "object_id", "generation"])
        .returning(j.c.id)
    ).scalar_one_or_none()
    if job_id is None:
        job_id = conn.execute(
            select(j.c.id).where(
                j.c.tenant_id == tenant_id, j.c.kind == kind, j.c.object_id == object_id, j.c.generation == generation
            )
        ).scalar_one()
    return job_id


def ensure_job(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    object_id: uuid.UUID,
    delay_seconds: float = 0,
    max_attempts: int = 5,
    created_by: uuid.UUID | None = None,
    source_event_key: str | None = None,
) -> uuid.UUID:
    """Coalescing enqueue for "bring this destination up to date" work (M5 syncs and refreshes).

    Reuses a job of the same (kind, object) that has not started yet; otherwise creates the next
    generation, so a change arriving while a run is in progress is picked up by a follow-up run.
    A transaction-scoped advisory lock serialises concurrent callers for the same object.
    """
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"job:{kind}:{object_id}", 0))))
    waiting = conn.execute(
        select(j.c.id).where(j.c.tenant_id == tenant_id, j.c.kind == kind, j.c.object_id == object_id,
                             j.c.state.in_(("QUEUED", "RETRY_WAIT")), j.c.cancel_requested.is_(False))
        .order_by(j.c.generation.desc()).limit(1)
    ).scalar_one_or_none()  # fmt: skip
    if waiting is not None:
        return waiting
    generation = conn.execute(
        select(func.coalesce(func.max(j.c.generation), 0) + 1).where(
            j.c.tenant_id == tenant_id, j.c.kind == kind, j.c.object_id == object_id
        )
    ).scalar_one()
    job_id = create_job(
        conn, tenant_id=tenant_id, kind=kind, object_id=object_id, generation=generation,
        max_attempts=max_attempts, created_by=created_by, source_event_key=source_event_key,
    )  # fmt: skip
    if delay_seconds > 0:
        conn.execute(update(j).where(j.c.id == job_id).values(next_attempt_at=_after(delay_seconds)))
    return job_id


def _safe(message: str | None) -> str | None:
    return None if message is None else redact(message)[:500]


def _backoff(attempt_no: int, retry_after: float | None) -> float:
    base = BACKOFF_SECONDS[min(attempt_no - 1, len(BACKOFF_SECONDS) - 1)]
    delay = base * (1 + random.uniform(0, 0.25))  # noqa: S311 - jitter, not security
    return max(delay, retry_after or 0)


def claim_next(conn: Connection, kinds: list[str], worker_id: str) -> Claim | None:
    """Dispatcher-scoped. Claims one due job, recovering expired leases along the way.

    Single writer per object: a job is not claimed while another generation of the same (kind, object)
    holds a live lease, so two workers never write to the same destination at once.
    """
    other = j.alias("other")
    busy = (
        select(other.c.id)
        .where(other.c.tenant_id == j.c.tenant_id, other.c.kind == j.c.kind, other.c.object_id == j.c.object_id,
               other.c.id != j.c.id, other.c.state == "RUNNING", other.c.lease_until >= func.now())
        .exists()
    )  # fmt: skip
    while True:
        row = conn.execute(
            select(j)
            .where(
                j.c.kind.in_(kinds),
                or_(
                    and_(j.c.state.in_(("QUEUED", "RETRY_WAIT")), j.c.next_attempt_at <= func.now()),
                    and_(j.c.state == "RUNNING", j.c.lease_until < func.now()),
                ),
                ~busy,
            )
            .order_by(j.c.next_attempt_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if row is None:
            return None
        # Close the race between two claimers picking different generations of one object: the lock is
        # held until this transaction commits, and the re-check then sees the other claimer's RUNNING row.
        conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"job:{row.kind}:{row.object_id}", 0))))
        running = conn.execute(
            select(j.c.id).where(j.c.tenant_id == row.tenant_id, j.c.kind == row.kind, j.c.object_id == row.object_id,
                                 j.c.id != row.id, j.c.state == "RUNNING", j.c.lease_until >= func.now()).limit(1)
        ).first()  # fmt: skip
        if running is not None:
            return None

        if row.state == "RUNNING":  # previous worker's lease expired
            _close_attempt(conn, row.id, row.lease_token, "LEASE_EXPIRED", "LEASE_EXPIRED", "Worker lease expired.")
            if row.attempts >= row.max_attempts:
                _finish(conn, row.id, "FAILED", error_code="LEASE_EXPIRED",
                        error_message="Stopped after repeated worker interruptions.", retryable=True)  # fmt: skip
                continue
        if row.cancel_requested:
            _finish(conn, row.id, "CANCELLED")
            continue

        token = uuid.uuid4()
        attempt_no = row.attempts + 1
        conn.execute(
            update(j)
            .where(j.c.id == row.id)
            .values(
                state="RUNNING",
                attempts=attempt_no,
                lease_token=token,
                worker_id=worker_id,
                lease_until=_after(LEASE_SECONDS),
                error_code=None,
                error_message=None,
            )  # fmt: skip
        )
        conn.execute(
            a.insert().values(
                id=uuid.uuid4(),
                tenant_id=row.tenant_id,
                job_id=row.id,
                attempt_no=attempt_no,
                lease_token=token,
                worker_id=worker_id,
            )  # fmt: skip
        )
        return Claim(row.id, row.tenant_id, row.kind, row.object_id, row.generation, attempt_no, token,
                     row.correlation_id)  # fmt: skip


def heartbeat(conn: Connection, claim: Claim) -> bool:
    renewed = conn.execute(
        update(j)
        .where(j.c.id == claim.job_id, j.c.lease_token == claim.lease_token, j.c.state == "RUNNING")
        .values(lease_until=_after(LEASE_SECONDS))
        .returning(j.c.id)
    ).scalar_one_or_none()
    return renewed is not None


def fence(conn: Connection, claim: Claim) -> Any:
    """Lock the job row and prove this worker still owns it. Call first in every job transaction."""
    row = conn.execute(
        select(j)
        .where(j.c.id == claim.job_id, j.c.lease_token == claim.lease_token, j.c.state == "RUNNING")
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise StaleLease(str(claim.job_id))
    return row


def is_cancel_requested(conn: Connection, claim: Claim) -> bool:
    return bool(conn.execute(select(j.c.cancel_requested).where(j.c.id == claim.job_id)).scalar())


def report_progress(conn: Connection, claim: Claim, processed: int, total: int | None) -> None:
    fence(conn, claim)
    conn.execute(update(j).where(j.c.id == claim.job_id).values(processed=processed, total=total))


def complete(
    conn: Connection,
    claim: Claim,
    *,
    state: str = "SUCCEEDED",
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    retryable: bool | None = None,
) -> None:
    """Finish the job. FAILED here means the handler ran to completion but produced no usable
    output (e.g. every page failed); by default PARTIAL and FAILED may be retried by a person."""
    if state not in ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"):
        raise ValueError(state)
    fence(conn, claim)
    _close_attempt(conn, claim.job_id, claim.lease_token, state, error_code, error_message)
    if retryable is None:
        retryable = state in ("PARTIAL", "FAILED")
    _finish(conn, claim.job_id, state, result=result, error_code=error_code, error_message=error_message,
            retryable=retryable)  # fmt: skip


def fail(
    conn: Connection,
    claim: Claim,
    *,
    error_code: str,
    error_message: str,
    transient: bool,
    retry_after: float | None = None,
) -> str:
    """Record a failed attempt. Returns the resulting job state (RETRY_WAIT or FAILED)."""
    row = fence(conn, claim)
    if transient and claim.attempt_no < row.max_attempts and not row.cancel_requested:
        _close_attempt(conn, claim.job_id, claim.lease_token, "RETRY", error_code, error_message)
        delay = _backoff(claim.attempt_no, retry_after)
        conn.execute(
            update(j)
            .where(j.c.id == claim.job_id)
            .values(
                state="RETRY_WAIT",
                lease_token=None,
                lease_until=None,
                worker_id=None,
                next_attempt_at=_after(delay),
                error_code=error_code,
                error_message=_safe(error_message),
            )  # fmt: skip
        )
        return "RETRY_WAIT"
    _close_attempt(conn, claim.job_id, claim.lease_token, "FAILED", error_code, error_message)
    # Exhausted transient errors may be retried by a person; poison input may not.
    _finish(conn, claim.job_id, "FAILED", error_code=error_code, error_message=error_message, retryable=transient)
    return "FAILED"


def request_cancel(conn: Connection, job_id: uuid.UUID) -> str:
    """Cooperative cancellation. Returns the state after the request, or raises LookupError."""
    row = conn.execute(select(j).where(j.c.id == job_id).with_for_update()).one_or_none()
    if row is None:
        raise LookupError(job_id)
    if row.state in TERMINAL:
        return row.state
    if row.state in ("QUEUED", "RETRY_WAIT"):
        _finish(conn, job_id, "CANCELLED")
        return "CANCELLED"
    conn.execute(update(j).where(j.c.id == job_id).values(cancel_requested=True))
    return "RUNNING"


def _close_attempt(conn: Connection, job_id, token, outcome: str, code: str | None, message: str | None) -> None:
    conn.execute(
        update(a)
        .where(a.c.job_id == job_id, a.c.lease_token == token, a.c.outcome == "RUNNING")
        .values(outcome=outcome, finished_at=func.now(), error_code=code, error_message=_safe(message))
    )


def _finish(conn: Connection, job_id, state: str, *, result=None, error_code=None, error_message=None,
            retryable: bool = False) -> None:  # fmt: skip
    conn.execute(
        update(j)
        .where(j.c.id == job_id)
        .values(
            state=state,
            lease_token=None,
            lease_until=None,
            worker_id=None,
            finished_at=func.now(),
            result=result,
            error_code=error_code,
            error_message=_safe(error_message),
            retryable=retryable,
        )  # fmt: skip
    )
