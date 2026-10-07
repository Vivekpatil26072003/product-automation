"""M5 integration jobs (FR13, FR15, FR22, A5).

integrations.fanout (object = record)      record changed -> PENDING in every connected record destination,
                                            then one coalesced sync job per destination and a Power BI refresh.
integration.test    (object = connection)  provider test without writing production data; a pass connects.
sheets.sync / erp.sync (object = connection) push PENDING records; single writer per destination.
powerbi.refresh     (object = connection)  coalesced refresh with a minimum interval, then status polling.

Network calls never happen inside a database transaction. Decrypted credentials live only in the handler.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import func, insert, select, update

from app.audit import service as audit
from app.core.crypto import SecretsUnavailable, decrypt
from app.db import tables as t
from app.integrations import adapter_for
from app.integrations import service as integ
from app.integrations.base import IntegrationError
from app.jobs import ledger
from workers.registry import Outcome, handler, route
from workers.runtime import JobContext, RetryableError

log = logging.getLogger("workers.integrations")
SERVICE = audit.Actor("service", None)
c, rs, pr = t.integration_connection, t.record_sync, t.powerbi_refresh
BATCH = 500
POLL_SECONDS = 60

route("production_record.changed", "integrations.fanout", "record_id", coalesce=True)


def _secret(row: Any) -> dict[str, Any]:
    if row.secret_ciphertext is None:
        return {}
    return decrypt(row.id, row.secret_ciphertext, row.secret_key_id)


def _set_state(conn, row: Any, state: str, error: IntegrationError | None = None, **extra: Any) -> None:
    values: dict[str, Any] = {"state": state, **extra}
    if error is not None:
        values |= {"last_error_code": error.code, "last_error_message": integ.safe_error(error.message)}
    conn.execute(update(c).where(c.c.id == row.id).values(**values))
    if state != row.state:
        audit.record(
            conn,
            tenant_id=row.tenant_id,
            actor=SERVICE,
            action="INTEGRATION_STATE_CHANGED",
            object_type="integration_connection",
            object_id=row.id,
            before={"state": row.state},
            after={"state": state, "error_code": error.code if error else None},
        )


def _stop_for(conn, row: Any, exc: IntegrationError) -> Outcome:
    """Conflict or broken credentials: stop writing to this destination until an administrator acts."""
    _set_state(conn, row, "CONFLICT" if exc.conflict else "RECONNECT_REQUIRED", exc)
    return Outcome(state="FAILED", error_code=exc.code, error_message=exc.message)


# --- fan-out ------------------------------------------------------------------------------------


@handler("integrations.fanout")
def fanout(ctx: JobContext) -> Outcome:
    record_id = ctx.claim.object_id
    queued: list[str] = []
    with ctx.transaction() as conn:
        rows = conn.execute(select(c).where(c.c.state == "CONNECTED")).all()
        for row in rows:
            if row.provider in integ.RECORD_TARGETS:
                if not integ.feature_enabled(conn, row.tenant_id, row.provider):
                    continue
                integ.mark_pending(conn, row.tenant_id, row.id, [record_id])
            if row.provider in integ.RECORD_TARGETS or row.provider == "power_bi":
                integ.schedule_sync(conn, row)
                queued.append(row.provider)
    return Outcome(result={"destinations": sorted(queued)})


# --- connection test ------------------------------------------------------------------------------


@handler(integ.TEST_KIND)
def test_connection(ctx: JobContext) -> Outcome:
    with ctx.transaction() as conn:
        row = conn.execute(select(c).where(c.c.id == ctx.claim.object_id)).one()
        if row.state == "DISCONNECTED":
            return Outcome(result={"skipped": "DISCONNECTED"})
        if not integ.feature_enabled(conn, row.tenant_id, row.provider):
            _set_state(
                conn,
                row,
                "TEST_FAILED",
                IntegrationError("FEATURE_DISABLED", "ERP integration is turned off in company settings."),
                last_test_at=func.now(),
                last_test_ok=False,
            )
            return Outcome(state="FAILED", error_code="FEATURE_DISABLED", error_message="ERP integration is off.")
    tested_version = row.config_version

    error: IntegrationError | None = None
    details: dict[str, Any] = {}
    try:
        details = adapter_for(row.provider, row.config, _secret(row)).test()
    except SecretsUnavailable:
        error = IntegrationError("SECRETS_UNAVAILABLE", "Stored credentials cannot be read. Enter them again.")
    except IntegrationError as exc:
        error = exc

    with ctx.transaction() as conn:
        row = conn.execute(select(c).where(c.c.id == row.id).with_for_update()).one()
        if row.state == "DISCONNECTED" or row.config_version != tested_version:
            return Outcome(result={"skipped": "CHANGED_DURING_TEST"})  # a newer test is queued
        if error is not None:
            _set_state(conn, row, "TEST_FAILED", error, last_test_at=func.now(), last_test_ok=False)
            return Outcome(state="FAILED", error_code=error.code, error_message=error.message)
        _set_state(
            conn,
            row,
            "CONNECTED",
            last_test_at=func.now(),
            last_test_ok=True,
            last_error_code=None,
            last_error_message=None,
        )
        queued = 0
        if row.provider in integ.RECORD_TARGETS:
            # (Re)connecting reconciles every approved record: the destination may have been replaced.
            queued = integ.mark_pending(conn, row.tenant_id, row.id)
        integ.schedule_sync(conn, row)
    return Outcome(result={"details": details, "queued_records": queued})


# --- record destinations ------------------------------------------------------------------------


def _pending(conn, connection_id: uuid.UUID) -> list[Any]:
    return conn.execute(
        select(rs)
        .where(rs.c.connection_id == connection_id, rs.c.state == "PENDING")
        .order_by(rs.c.updated_at, rs.c.id)
        .limit(BATCH)
    ).all()


def _begin_sync(ctx: JobContext) -> tuple[Any, list[Any], list[dict[str, Any]]] | Outcome:
    with ctx.transaction() as conn:
        row = conn.execute(select(c).where(c.c.id == ctx.claim.object_id)).one()
        if row.state != "CONNECTED":
            return Outcome(result={"skipped": row.state})
        pending = _pending(conn, row.id)
        payloads = integ.record_payloads(conn, [p.record_id for p in pending])
    return row, pending, payloads


def _transient(ctx: JobContext, row: Any, pending: list[Any], exc: IntegrationError) -> RetryableError:
    """Count the attempt on each record; records past the limit become FAILED (retry from the settings page)."""
    with ctx.transaction() as conn:
        for p in pending:
            conn.execute(
                update(rs)
                .where(rs.c.id == p.id, rs.c.updated_at == p.updated_at)
                .values(
                    attempts=rs.c.attempts + 1,
                    error_code=exc.code,
                    error_message=integ.safe_error(exc.message),
                    state="FAILED" if p.attempts + 1 >= integ.MAX_SYNC_ATTEMPTS else "PENDING",
                )
            )
        conn.execute(
            update(c)
            .where(c.c.id == row.id)
            .values(last_error_code=exc.code, last_error_message=integ.safe_error(exc.message))
        )
    return RetryableError(exc.code, exc.message, exc.retry_after)


def _finish_sync(
    ctx: JobContext,
    row: Any,
    done: dict[uuid.UUID, tuple[str, int, str | None]],
    pending: list[Any],
    failed: dict[uuid.UUID, IntegrationError],
) -> Outcome:
    """done: record_id -> (state, revision, external_ref). Rows changed meanwhile stay PENDING."""
    with ctx.transaction() as conn:
        for p in pending:
            if p.record_id in failed:
                exc = failed[p.record_id]
                conn.execute(
                    update(rs)
                    .where(rs.c.id == p.id, rs.c.updated_at == p.updated_at)
                    .values(
                        state="FAILED",
                        attempts=rs.c.attempts + 1,
                        error_code=exc.code,
                        error_message=integ.safe_error(exc.message),
                    )
                )
            elif p.record_id in done:
                state, revision, ref = done[p.record_id]
                values: dict[str, Any] = {"state": state, "external_ref": ref, "attempts": rs.c.attempts + 1}
                if state == "SYNCED":
                    # The destination may now be ahead of a fan-out that has not run yet: raise the target too.
                    values |= {
                        "synced_revision": revision,
                        "target_revision": func.greatest(rs.c.target_revision, revision),
                        "synced_at": func.now(),
                        "error_code": None,
                        "error_message": None,
                    }
                else:
                    values |= {
                        "error_code": "DESTINATION_NEWER",
                        "error_message": "The destination holds a newer revision than this system.",
                    }
                conn.execute(
                    update(rs)
                    .where(rs.c.id == p.id, rs.c.updated_at == p.updated_at, rs.c.target_revision <= revision)
                    .values(**values)
                )
        conn.execute(
            update(c)
            .where(c.c.id == row.id)
            .values(last_sync_at=func.now(), last_error_code=None, last_error_message=None)
        )
        more = bool(_pending(conn, row.id))
        if more:
            integ.schedule_sync(conn, row)
    synced = sum(1 for v in done.values() if v[0] == "SYNCED")
    return Outcome(result={"synced": synced, "failed": len(failed), "more": more})


@handler("sheets.sync")
def sheets_sync(ctx: JobContext) -> Outcome:
    begun = _begin_sync(ctx)
    if isinstance(begun, Outcome):
        return begun
    row, pending, payloads = begun
    if not pending:
        return Outcome(result={"synced": 0})
    try:
        written = adapter_for(row.provider, row.config, _secret(row)).upsert(payloads)
    except SecretsUnavailable:
        with ctx.transaction() as conn:
            return _stop_for(
                conn, row, IntegrationError("SECRETS_UNAVAILABLE", "Stored credentials cannot be read.", reconnect=True)
            )
    except IntegrationError as exc:
        if exc.transient:
            raise _transient(ctx, row, pending, exc) from exc
        if exc.conflict or exc.reconnect:
            with ctx.transaction() as conn:
                return _stop_for(conn, row, exc)
        failed = {p.record_id: exc for p in pending}
        return _finish_sync(ctx, row, {}, pending, failed)
    done = {
        uuid.UUID(rid): ("CONFLICT" if w["action"] == "NEWER_IN_SHEET" else "SYNCED", w["revision"], f"row:{w['row']}")
        for rid, w in written.items()
    }
    # A NEWER_IN_SHEET row reports the sheet's revision; compare against what we tried to write.
    for p in payloads:
        key = uuid.UUID(p["record_id"])
        if key in done and done[key][0] == "CONFLICT":
            done[key] = ("CONFLICT", p["revision"], done[key][2])
    return _finish_sync(ctx, row, done, pending, {})


@handler("erp.sync")
def erp_sync(ctx: JobContext) -> Outcome:
    begun = _begin_sync(ctx)
    if isinstance(begun, Outcome):
        return begun
    row, pending, payloads = begun
    if not pending:
        return Outcome(result={"synced": 0})
    try:
        adapter = adapter_for(row.provider, row.config, _secret(row))
    except (SecretsUnavailable, IntegrationError) as exc:
        err = (
            exc
            if isinstance(exc, IntegrationError)
            else IntegrationError("SECRETS_UNAVAILABLE", "Stored credentials cannot be read.", reconnect=True)
        )
        with ctx.transaction() as conn:
            return _stop_for(conn, row, err)
    done: dict[uuid.UUID, tuple[str, int, str | None]] = {}
    failed: dict[uuid.UUID, IntegrationError] = {}
    attempts: list[dict[str, Any]] = []
    transient: IntegrationError | None = None
    for p in payloads:
        key = uuid.UUID(p["record_id"])
        try:
            ref = adapter.push_record(p, row.mapping_version)
            done[key] = ("SYNCED", p["revision"], ref)
            attempts.append({"record_id": key, "revision": p["revision"], "outcome": "SUCCEEDED", "external_ref": ref})
        except IntegrationError as exc:
            outcome = "RETRY" if exc.transient else "FAILED"
            attempts.append(
                {
                    "record_id": key,
                    "revision": p["revision"],
                    "outcome": outcome,
                    "error_code": exc.code,
                    "error_message": integ.safe_error(exc.message),
                }
            )
            if exc.transient:
                transient = exc
                break
            if exc.reconnect or exc.conflict:
                with ctx.transaction() as conn:
                    _log_attempts(conn, row, attempts)
                    return _stop_for(conn, row, exc)
            failed[key] = exc
    with ctx.transaction() as conn:
        _log_attempts(conn, row, attempts)
    if transient is not None:
        remaining = [p for p in pending if p.record_id not in done and p.record_id not in failed]
        if done or failed:
            _finish_sync(ctx, row, done, [p for p in pending if p not in remaining], failed)
        raise _transient(ctx, row, remaining, transient)
    return _finish_sync(ctx, row, done, pending, failed)


def _log_attempts(conn, row: Any, attempts: list[dict[str, Any]]) -> None:
    if attempts:
        conn.execute(
            insert(t.erp_sync_attempt),
            [
                {
                    "id": uuid.uuid4(),
                    "tenant_id": row.tenant_id,
                    "connection_id": row.id,
                    "direction": "OUTBOUND",
                    "mapping_version": row.mapping_version,
                    **a,
                }
                for a in attempts
            ],
        )


# --- Power BI -------------------------------------------------------------------------------------


@handler(integ.REFRESH_KIND)
def powerbi_refresh(ctx: JobContext) -> Outcome:
    with ctx.transaction() as conn:
        row = conn.execute(select(c).where(c.c.id == ctx.claim.object_id)).one()
        if row.state != "CONNECTED":
            return Outcome(result={"skipped": row.state})
        data_version = conn.execute(select(t.tenant.c.data_version).where(t.tenant.c.id == row.tenant_id)).scalar_one()
        latest = conn.execute(
            select(pr).where(pr.c.connection_id == row.id).order_by(pr.c.requested_at.desc()).limit(1)
        ).one_or_none()
        covered = conn.execute(
            select(func.max(pr.c.source_data_version)).where(pr.c.connection_id == row.id, pr.c.state == "COMPLETED")
        ).scalar()
        since_last = (
            None
            if latest is None
            else conn.execute(select(func.extract("epoch", func.now() - latest.requested_at))).scalar()
        )

    try:
        adapter = adapter_for(row.provider, row.config, _secret(row))
        if latest is not None and latest.state in ("REQUESTED", "IN_PROGRESS"):
            state, code = adapter.refresh_state(latest.provider_request_id)
            with ctx.transaction() as conn:
                values: dict[str, Any] = {"state": state, "error_code": code}
                if state in ("COMPLETED", "FAILED"):
                    values["completed_at"] = func.now()
                    values["error_message"] = None if state == "COMPLETED" else "Power BI reported the refresh failed."
                conn.execute(update(pr).where(pr.c.id == latest.id).values(**values))
                if (
                    state == "IN_PROGRESS"
                    or (state == "COMPLETED" and latest.source_data_version < data_version)
                    or (state == "FAILED" and (covered or 0) < data_version)
                ):
                    ledger.ensure_job(
                        conn,
                        tenant_id=row.tenant_id,
                        kind=integ.REFRESH_KIND,
                        object_id=row.id,
                        delay_seconds=POLL_SECONDS,
                    )
            return Outcome(result={"refresh": state})

        if covered is not None and covered >= data_version:
            return Outcome(result={"skipped": "UP_TO_DATE"})
        min_interval = int(row.config.get("min_interval_minutes", 30)) * 60
        if since_last is not None and float(since_last) < min_interval:
            with ctx.transaction() as conn:  # coalesce: one refresh after the interval covers every change
                ledger.ensure_job(
                    conn,
                    tenant_id=row.tenant_id,
                    kind=integ.REFRESH_KIND,
                    object_id=row.id,
                    delay_seconds=min_interval - float(since_last),
                )
            return Outcome(result={"deferred_seconds": round(min_interval - float(since_last))})

        request_id = adapter.start_refresh()
    except SecretsUnavailable:
        with ctx.transaction() as conn:
            return _stop_for(
                conn, row, IntegrationError("SECRETS_UNAVAILABLE", "Stored credentials cannot be read.", reconnect=True)
            )
    except IntegrationError as exc:
        if exc.transient:
            raise RetryableError(exc.code, exc.message, exc.retry_after) from exc
        with ctx.transaction() as conn:
            if exc.reconnect or exc.conflict:
                return _stop_for(conn, row, exc)
            conn.execute(
                update(c)
                .where(c.c.id == row.id)
                .values(last_error_code=exc.code, last_error_message=integ.safe_error(exc.message))
            )
        return Outcome(state="FAILED", error_code=exc.code, error_message=exc.message)

    with ctx.transaction() as conn:
        conn.execute(
            insert(pr).values(
                id=uuid.uuid4(),
                tenant_id=row.tenant_id,
                connection_id=row.id,
                source_data_version=data_version,
                state="REQUESTED",
                provider_request_id=request_id,
            )
        )
        conn.execute(
            update(c)
            .where(c.c.id == row.id)
            .values(last_sync_at=func.now(), last_error_code=None, last_error_message=None)
        )
        ledger.ensure_job(
            conn, tenant_id=row.tenant_id, kind=integ.REFRESH_KIND, object_id=row.id, delay_seconds=POLL_SECONDS
        )
    return Outcome(result={"requested_data_version": data_version})
