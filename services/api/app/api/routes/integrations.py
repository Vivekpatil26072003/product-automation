"""Integrations (FR13, FR15, FR22, A5): connections, sync jobs and Power BI refresh.

Connections are administrator-only. Credentials are write-only: accepted on create/update, encrypted,
and never returned, logged or written to the audit trail (only "secret changed: yes/no").
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from app.api.deps import etag, idempotency_key, if_match, require
from app.audit import service as audit
from app.auth.principal import Principal
from app.core.crypto import SecretsUnavailable, encrypt
from app.core.crypto import configured as crypto_configured
from app.core.errors import ApiError, conflict, precondition_failed
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import tenant_tx
from app.domain.enums import Role
from app.ingestion.service import job_view
from app.integrations import service as integ
from app.jobs import ledger

router = APIRouter(tags=["integrations"])
c = t.integration_connection
SYNC_JOB_KINDS = ("sheets.sync", "erp.sync", integ.REFRESH_KIND, integ.TEST_KIND)


class ConnectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)
    secret: dict[str, Any] | None = None


class ConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: dict[str, Any] | None = None
    secret: dict[str, Any] | None = None


def _no_keys() -> ApiError:
    return ApiError(
        503,
        "SECRETS_UNAVAILABLE",
        "Credential encryption is not configured on the server. Ask the operator to set INTEGRATION_KEYS.",
    )


def _encrypt(connection_id: uuid.UUID, secret: dict[str, Any]) -> tuple[bytes, str]:
    try:
        return encrypt(connection_id, secret)
    except SecretsUnavailable as exc:
        raise _no_keys() from exc


def _one_view(conn, row) -> dict[str, Any]:
    return integ.view(row, integ.sync_counts(conn, [row.id])[row.id])


def _queue_test(conn, row, principal: Principal) -> None:
    ledger.ensure_job(
        conn,
        tenant_id=row.tenant_id,
        kind=integ.TEST_KIND,
        object_id=row.id,
        max_attempts=1,
        created_by=principal.membership_id,
    )


def _respond(status: int, out: dict[str, Any]) -> JSONResponse:
    data = out.get("data", {})
    headers = {"ETag": etag(data["version"])} if isinstance(data, dict) and "version" in data else None
    return JSONResponse(out, status_code=status, headers=headers)


@router.get("/integrations", summary="Integration connections (administrators)")
def list_connections(principal: Principal = Depends(require(Role.ADMIN))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        rows = conn.execute(select(c).order_by(c.c.provider, c.c.created_at.desc())).all()
        live = [x for x in rows if x.state != "DISCONNECTED"]
        counts = integ.sync_counts(conn, [x.id for x in live])
        erp_enabled = integ.feature_enabled(conn, principal.tenant_id, "erp")
        history = [integ.view(x) for x in rows if x.state == "DISCONNECTED"][:20]
    return {
        "data": [integ.view(x, counts[x.id]) for x in live],
        "disconnected": history,
        "providers": [{"provider": p, "available": p != "erp" or erp_enabled} for p in integ.PROVIDERS],
        "encryption_configured": crypto_configured(),
    }


@router.post("/integrations", status_code=201, summary="Add a connection; a connection test is queued")
def create_connection(
    body: ConnectionIn,
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    config, secret = integ.validate(body.provider, body.config, body.secret)
    if secret is None and body.provider != "erp":
        raise ApiError(422, "VALIDATION_FAILED", "Enter the credentials for this connection.")

    def effect() -> tuple[int, dict]:
        if not integ.feature_enabled(conn, principal.tenant_id, body.provider):
            raise conflict("FEATURE_DISABLED", "Turn on ERP integration in company settings first.")
        if integ.live(conn, body.provider) is not None:
            raise conflict("ALREADY_CONNECTED", "This destination is already set up. Edit or disconnect it first.")
        cid = uuid.uuid4()
        blob, key_id = _encrypt(cid, secret) if secret is not None else (None, None)
        conn.execute(
            c.insert().values(
                id=cid,
                tenant_id=principal.tenant_id,
                provider=body.provider,
                name=body.name,
                config=config,
                secret_ciphertext=blob,
                secret_key_id=key_id,
                created_by=principal.membership_id,
            )
        )
        row = integ.load(conn, cid)
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action="INTEGRATION_CREATED",
            object_type="integration_connection",
            object_id=cid,
            after={"provider": body.provider, "name": body.name, "config": config, "secret_set": secret is not None},
        )
        _queue_test(conn, row, principal)
        return 201, {"data": _one_view(conn, row)}

    with tenant_tx(principal.tenant_id) as conn:
        # The payload hash covers the secret too, so a replay with different credentials is refused.
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route="POST /integrations",
            key=key,
            payload=body.model_dump(),
            effect=effect,
        )
    return _respond(status, out)


@router.get("/integrations/{connection_id}", summary="One connection (credentials are never returned)")
def get_connection(connection_id: uuid.UUID, principal: Principal = Depends(require(Role.ADMIN))) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        data = _one_view(conn, integ.load(conn, connection_id))
    return _respond(200, {"data": data})


@router.patch("/integrations/{connection_id}", summary="Change settings or replace credentials; re-tests")
def patch_connection(
    connection_id: uuid.UUID,
    body: ConnectionPatch,
    expected_version: int = Depends(if_match),
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        row = integ.load(conn, connection_id, lock=True)
        if row.state == "DISCONNECTED":
            raise conflict("DISCONNECTED", "This connection was disconnected. Add a new one instead.")
        if row.version != expected_version:
            raise precondition_failed(row.version)
        merged = {**(row.config or {}), **(body.config or {})}
        config, secret = integ.validate(row.provider, merged, body.secret)
        values: dict[str, Any] = {"version": c.c.version + 1}
        if body.name is not None:
            values["name"] = body.name
        changed = config != row.config or secret is not None
        if secret is not None:
            values["secret_ciphertext"], values["secret_key_id"] = _encrypt(row.id, secret)
        if changed:
            values |= {
                "config": config,
                "config_version": c.c.config_version + 1,
                "state": "NEEDS_TEST",
                "last_error_code": None,
                "last_error_message": None,
            }
        conn.execute(update(c).where(c.c.id == row.id).values(**values))
        after = integ.load(conn, row.id)
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action="INTEGRATION_UPDATED",
            object_type="integration_connection",
            object_id=row.id,
            before={"name": row.name, "config": row.config, "state": row.state},
            after={
                "name": after.name,
                "config": after.config,
                "state": after.state,
                "secret_changed": secret is not None,
            },
        )
        if changed:
            _queue_test(conn, after, principal)
        return 200, {"data": _one_view(conn, after)}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=f"PATCH /integrations/{connection_id}",
            key=key,
            payload=[expected_version, body.model_dump(exclude_unset=True)],
            effect=effect,
        )
    return _respond(status, out)


def _command(connection_id: uuid.UUID, principal: Principal, key: str | None, name: str, fn) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        row = integ.load(conn, connection_id, lock=True)
        if row.state == "DISCONNECTED":
            raise conflict("DISCONNECTED", "This connection was disconnected.")
        result = fn(conn, row) or {}
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action=f"INTEGRATION_{name.upper()}",
            object_type="integration_connection",
            object_id=row.id,
            after=result or None,
        )
        return 202, {"data": _one_view(conn, integ.load(conn, row.id)), "result": result}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=f"POST /integrations/{connection_id}/{name}",
            key=key,
            payload=None,
            effect=effect,
        )
    return _respond(status, out)


@router.post("/integrations/{connection_id}/test", status_code=202, summary="Queue a connection test")
def test_connection(
    connection_id: uuid.UUID,
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    return _command(connection_id, principal, key, "test", lambda conn, row: _queue_test(conn, row, principal))


def _require_connected_target(row) -> None:
    if row.provider not in integ.RECORD_TARGETS:
        raise conflict("NOT_A_RECORD_DESTINATION", "Only Google Sheets and the ERP receive individual records.")
    if row.state != "CONNECTED":
        raise conflict("NOT_CONNECTED", "Test the connection successfully first.")


@router.post(
    "/integrations/{connection_id}/reconcile",
    status_code=202,
    summary="Re-send every approved record (the destination keeps newer revisions)",
)
def reconcile(
    connection_id: uuid.UUID,
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def fn(conn, row) -> dict:
        _require_connected_target(row)
        n = integ.mark_pending(conn, row.tenant_id, row.id)
        integ.schedule_sync(conn, row, principal.membership_id)
        return {"queued_records": n}

    return _command(connection_id, principal, key, "reconcile", fn)


@router.post("/integrations/{connection_id}/retry-failed", status_code=202, summary="Retry records that failed")
def retry_failed(
    connection_id: uuid.UUID,
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def fn(conn, row) -> dict:
        _require_connected_target(row)
        rs = t.record_sync
        n = conn.execute(
            update(rs)
            .where(rs.c.connection_id == row.id, rs.c.state == "FAILED")
            .values(state="PENDING", attempts=0, error_code=None, error_message=None)
            .returning(rs.c.id)
        ).all()
        if n:
            integ.schedule_sync(conn, row, principal.membership_id)
        return {"requeued_records": len(n)}

    return _command(connection_id, principal, key, "retry_failed", fn)


@router.delete("/integrations/{connection_id}", summary="Disconnect: credentials are erased, history is kept")
def disconnect(
    connection_id: uuid.UUID,
    expected_version: int = Depends(if_match),
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        row = integ.load(conn, connection_id, lock=True)
        if row.state == "DISCONNECTED":
            return 200, {"data": integ.view(row)}
        if row.version != expected_version:
            raise precondition_failed(row.version)
        conn.execute(
            update(c)
            .where(c.c.id == row.id)
            .values(
                state="DISCONNECTED",
                secret_ciphertext=None,
                secret_key_id=None,
                disconnected_at=func.now(),
                version=c.c.version + 1,
            )
        )
        j = t.job
        cancelled = 0
        for job_id in conn.execute(
            select(j.c.id).where(j.c.object_id == row.id, j.c.state.notin_(ledger.TERMINAL))
        ).scalars():
            ledger.request_cancel(conn, job_id)
            cancelled += 1
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action="INTEGRATION_DISCONNECTED",
            object_type="integration_connection",
            object_id=row.id,
            before={"state": row.state},
            after={"state": "DISCONNECTED", "cancelled_jobs": cancelled},
        )
        return 200, {"data": integ.view(integ.load(conn, row.id))}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=f"DELETE /integrations/{connection_id}",
            key=key,
            payload=[expected_version],
            effect=effect,
        )
    return _respond(status, out)


@router.get("/sync-jobs", summary="Recent sync, refresh and connection-test jobs")
def sync_jobs(
    connection_id: uuid.UUID | None = Query(default=None),
    principal: Principal = Depends(require(Role.ADMIN)),
) -> dict:
    j = t.job
    q = select(j).where(j.c.kind.in_(SYNC_JOB_KINDS))
    if connection_id is not None:
        q = q.where(j.c.object_id == connection_id)
    with tenant_tx(principal.tenant_id) as conn:
        rows = conn.execute(q.order_by(j.c.created_at.desc()).limit(50)).all()
    return {"data": [job_view(x) for x in rows]}


@router.get("/powerbi/status", summary="Power BI freshness (the native dashboard never waits for it)")
def powerbi_status(principal: Principal = Depends(require())) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": integ.powerbi_status(conn, principal.tenant_id)}


@router.post("/powerbi/refresh", status_code=202, summary="Request a Power BI refresh (coalesced)")
def powerbi_refresh(
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        row = integ.live(conn, "power_bi")
        if row is None or row.state != "CONNECTED":
            raise conflict("NOT_CONNECTED", "Connect and test Power BI first.")
        job_id = integ.schedule_sync(conn, row, principal.membership_id)
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action="POWERBI_REFRESH_REQUESTED",
            object_type="integration_connection",
            object_id=row.id,
            after={"job_id": str(job_id)},
        )
        return 202, {"data": integ.powerbi_status(conn, principal.tenant_id), "job_id": str(job_id)}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route="POST /powerbi/refresh",
            key=key,
            payload=None,
            effect=effect,
        )
    return JSONResponse(out, status_code=status)
