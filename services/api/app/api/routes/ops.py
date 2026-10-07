"""Operations (M8): metrics for monitoring, status for administrators, retention and ROI.

GET /ops/metrics      Prometheus text for the infrastructure scraper; needs METRICS_TOKEN (404 when unset).
GET /ops/status       administrator view of this company's health, alerts, send pause and retention.
Retention (FR25)      dry run, purge, holds; administrators only; every action audited.
ROI (A10)             measured pilot figures against the administrator-entered baseline.
"""

import hmac
import uuid
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import etag, idempotency_key, if_match, require
from app.audit import service as audit
from app.auth.principal import Principal
from app.core.config import get_settings
from app.core.errors import ApiError, conflict, not_found, precondition_failed, validation_failed
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import dispatcher_tx, tenant_tx
from app.domain.enums import Role
from app.ops import monitor, retention, roi
from app.records.query import local_today

router = APIRouter(tags=["operations"])


@router.get("/ops/metrics", response_class=PlainTextResponse, summary="Prometheus metrics (token required)")
def metrics(authorization: str | None = Header(default=None)) -> PlainTextResponse:
    token = get_settings().metrics_token
    if not token:
        raise not_found()
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented.encode(), token.encode()):
        raise ApiError(401, "UNAUTHENTICATED", "A valid metrics token is required.")
    queue, tenants = monitor.collect()
    return PlainTextResponse(monitor.prometheus(queue, tenants, {"sends_paused": float(get_settings().sends_paused)}))


@router.get("/ops/status", summary="Company health, alerts and retention (administrators)")
def status(principal: Principal = Depends(require(Role.ADMIN))) -> dict:
    with dispatcher_tx() as conn:
        queue = monitor.queue_metrics(conn)
    with tenant_tx(principal.tenant_id) as conn:
        tenant = monitor.tenant_metrics(conn)
        last = conn.execute(select(t.retention_run).order_by(t.retention_run.c.started_at.desc()).limit(5)).all()
        now = conn.execute(select(func.now())).scalar_one()
        preview = retention._counts(retention.eligible(conn, principal.tenant_id, now))
        holds = conn.execute(
            select(t.retention_hold)
            .where(t.retention_hold.c.released_at.is_(None))
            .order_by(t.retention_hold.c.created_at.desc())
        ).all()
    return {
        "data": {
            "sends_paused": get_settings().sends_paused,
            "queue": queue,
            "company": tenant,
            "alerts": [a.__dict__ for a in monitor.alerts(queue, tenant)],
            "retention": {
                "eligible_now": preview,
                "runs": [
                    {
                        "id": str(r.id),
                        "dry_run": r.dry_run,
                        "purged": r.purged,
                        "held": r.held,
                        "failed": r.failed,
                        "started_at": r.started_at.isoformat(),
                        "eligible": r.eligible,
                    }
                    for r in last
                ],
                "holds": [
                    {
                        "id": str(h.id),
                        "object_type": h.object_type,
                        "object_id": str(h.object_id),
                        "reason": h.reason,
                        "created_at": h.created_at.isoformat(),
                    }
                    for h in holds
                ],
            },
        }
    }


class RetentionRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True


@router.post("/retention/run", summary="Count (dry run) or purge files past their retention period")
def retention_run(
    body: RetentionRunIn,
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> dict:
    if not key or len(key) > 200:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header (1–200 characters).")
    # Deletions happen outside a transaction, so this is not wrapped in run_idempotent; a repeated purge finds
    # nothing more to delete, which makes it safe to repeat.
    return {"data": retention.run(principal.tenant_id, dry_run=body.dry_run, requested_by=principal.membership_id)}


class HoldIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_type: Literal["upload", "batch", "report"]
    object_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=500)


@router.post("/retention/holds", status_code=201, summary="Place a hold that prevents purging (e.g. an investigation)")
def add_hold(
    body: HoldIn,
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    table = {"upload": t.upload, "batch": t.batch, "report": t.report}[body.object_type]

    def effect() -> tuple[int, dict]:
        if conn.execute(select(table.c.id).where(table.c.id == body.object_id)).first() is None:
            raise not_found()
        hold_id = conn.execute(
            pg_insert(t.retention_hold)
            .values(
                id=uuid.uuid4(),
                tenant_id=principal.tenant_id,
                object_type=body.object_type,
                object_id=body.object_id,
                reason=body.reason,
                created_by=principal.membership_id,
            )
            .on_conflict_do_nothing()
            .returning(t.retention_hold.c.id)
        ).scalar_one_or_none()
        if hold_id is None:
            raise conflict("ALREADY_HELD", "This item already has an active hold.")
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action="RETENTION_HOLD_PLACED",
            object_type=body.object_type,
            object_id=body.object_id,
            reason=body.reason,
        )
        return 201, {"data": {"id": str(hold_id)}}

    with tenant_tx(principal.tenant_id) as conn:
        status_code, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route="POST /retention/holds",
            key=key,
            payload=body.model_dump(mode="json"),
            effect=effect,
        )
    return JSONResponse(out, status_code=status_code)


@router.post("/retention/holds/{hold_id}/release", summary="Release a hold; the item follows normal retention again")
def release_hold(
    hold_id: uuid.UUID,
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    h = t.retention_hold

    def effect() -> tuple[int, dict]:
        row = conn.execute(select(h).where(h.c.id == hold_id).with_for_update()).one_or_none()
        if row is None:
            raise not_found()
        if row.released_at is None:
            conn.execute(
                update(h).where(h.c.id == row.id).values(released_at=func.now(), released_by=principal.membership_id)
            )
            audit.record(
                conn,
                tenant_id=principal.tenant_id,
                actor=principal.actor,
                action="RETENTION_HOLD_RELEASED",
                object_type=row.object_type,
                object_id=row.object_id,
            )
        return 200, {"data": {"id": str(row.id), "released": True}}

    with tenant_tx(principal.tenant_id) as conn:
        status_code, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=f"POST /retention/holds/{hold_id}/release",
            key=key,
            payload=None,
            effect=effect,
        )
    return JSONResponse(out, status_code=status_code)


# --- ROI (A10) ---------------------------------------------------------------------------------------------


@router.get("/roi", summary="Measured pilot figures against the entered baseline")
def roi_view(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    principal: Principal = Depends(require(Role.ADMIN)),
) -> dict:
    date_to = date_to or local_today(principal.timezone)
    date_from = date_from or date_to - timedelta(days=27)
    if date_from > date_to or (date_to - date_from).days > 366:
        raise validation_failed([])
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": roi.measure(conn, principal.tenant_id, date_from, date_to)}


class BaselineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    measured_from: date | None = None
    measured_to: date | None = None
    manual_minutes_per_report: float | None = Field(default=None, ge=0, le=100_000)
    manual_minutes_per_entry: float | None = Field(default=None, ge=0, le=10_000)
    manual_minutes_per_email: float | None = Field(default=None, ge=0, le=10_000)
    manual_followups_per_week: float | None = Field(default=None, ge=0, le=10_000)
    manual_correction_rate_pct: float | None = Field(default=None, ge=0, le=100)
    notes: str | None = Field(default=None, max_length=2000)


@router.put("/roi/baseline", summary="Record the measured manual baseline (If-Match; version 0 to create)")
def put_baseline(
    body: BaselineIn,
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    b = t.roi_baseline
    values = body.model_dump()

    def effect() -> tuple[int, dict]:
        row = conn.execute(select(b).where(b.c.tenant_id == principal.tenant_id).with_for_update()).one_or_none()
        current = row.version if row else 0
        if current != expected:
            raise precondition_failed(current)
        if row is None:
            conn.execute(insert(b).values(tenant_id=principal.tenant_id, updated_by=principal.membership_id, **values))
        else:
            conn.execute(
                update(b)
                .where(b.c.tenant_id == principal.tenant_id)
                .values(updated_by=principal.membership_id, updated_at=func.now(), version=b.c.version + 1, **values)
            )
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action="ROI_BASELINE_SET",
            object_type="roi_baseline",
            object_id=principal.tenant_id,
            after={k: str(v) if v is not None else None for k, v in values.items()},
        )
        return 200, {"data": {"version": current + 1}}

    with tenant_tx(principal.tenant_id) as conn:
        status_code, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route="PUT /roi/baseline",
            key=key,
            payload=[expected, body.model_dump(mode="json")],
            effect=effect,
        )
    return JSONResponse(out, status_code=status_code, headers={"ETag": etag(out["data"]["version"])})
