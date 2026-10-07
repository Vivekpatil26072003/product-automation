"""Automation (FR24, A1-A3; API operations 46-53 plus exceptions and notifications).

Schedules: Sender, own scope, optional module (404 FEATURE_DISABLED when off).
Exceptions: role- and department-scoped; Reviewers, Senders and Administrators act on what they can see.
Notifications: each user's own inbox.
"""

import uuid
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from app.api.deps import etag, idempotency_key, if_match, require
from app.auth.principal import Principal
from app.automation import exceptions as exc_engine
from app.automation import schedules as sched
from app.core.errors import ApiError, Issue, not_found, validation_failed
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import tenant_tx
from app.domain.enums import Role

router = APIRouter(tags=["automation"])


def _idem(principal: Principal, route: str, key: str | None, payload: Any, effect) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=route,
            key=key,
            payload=payload,
            effect=lambda: effect(conn),
        )
    data = out.get("data")
    headers = {"ETag": etag(data["row_version"])} if isinstance(data, dict) and "row_version" in data else None
    return JSONResponse(out, status_code=status, headers=headers)


# --- schedules -------------------------------------------------------------------------------------------


@router.get("/schedules", summary="Scheduled reports the caller may manage")
def list_schedules(
    active: bool | None = Query(default=None), principal: Principal = Depends(require(Role.SENDER))
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        sched.require_enabled(conn, principal.tenant_id)
        q = select(t.schedule).order_by(t.schedule.c.created_at.desc())
        if active is not None:
            q = q.where(t.schedule.c.active == active)
        rows = [
            r
            for r in conn.execute(q).all()
            if principal.can_access_all({uuid.UUID(x) for x in r.config["department_ids"]})
        ]
        return {"data": [sched.view(conn, r) for r in rows]}


@router.get("/schedules/preview", summary="Next three occurrences for a cadence (no changes)")
def preview(
    cadence: Literal["DAILY", "WEEKLY", "MONTHLY"],
    local_time: str = Query(pattern=r"^([01]\d|2[0-3]):[0-5]\d$"),
    timezone: str | None = Query(default=None, max_length=64),
    weekday: int | None = Query(default=None, ge=1, le=7),
    monthday: int | None = Query(default=None, ge=1, le=31),
    principal: Principal = Depends(require(Role.SENDER)),
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        tz = timezone or sched.require_enabled(conn, principal.tenant_id)["timezone"]
    try:
        return {
            "data": sched.preview(
                {"cadence": cadence, "local_time": local_time, "timezone": tz, "weekday": weekday, "monthday": monthday}
            )
        }
    except Exception as exc:  # noqa: BLE001 - ValueError or unknown time zone
        raise validation_failed([Issue("INVALID_SCHEDULE", str(exc) or "Invalid schedule.", "cadence")]) from exc


@router.post("/schedules", status_code=201, summary="Create a schedule (auto-send starts unapproved)")
def create_schedule(
    body: sched.ScheduleInput,
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    return _idem(
        principal,
        "POST /schedules",
        key,
        body.model_dump(mode="json"),
        lambda conn: (201, {"data": sched.view(conn, sched.create(conn, principal, body))}),
    )


@router.get("/schedules/{schedule_id}", summary="Schedule with next runs and recent runs")
def get_schedule(schedule_id: uuid.UUID, principal: Principal = Depends(require(Role.SENDER))) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        sched.require_enabled(conn, principal.tenant_id)
        data = sched.view(conn, sched.load(conn, principal, schedule_id), with_runs=True)
    return JSONResponse({"data": data}, headers={"ETag": etag(data["row_version"])})


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    cadence: Literal["DAILY", "WEEKLY", "MONTHLY"] | None = None
    local_time: str | None = None
    weekday: int | None = None
    monthday: int | None = None
    timezone: str | None = None
    department_ids: list[uuid.UUID] | None = None
    units: list[str] | None = None
    title: str | None = None
    include_detail: bool | None = None
    to: list[str] | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    subject: str | None = None
    mode: Literal["DRAFT_ONLY", "AUTO_SEND"] | None = None
    empty_policy: Literal["SKIP", "DRAFT"] | None = None
    active: bool | None = None


@router.patch("/schedules/{schedule_id}", summary="Edit (new version, approval revoked) or pause/resume")
def patch_schedule(
    schedule_id: uuid.UUID,
    body: SchedulePatch,
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    changes = body.model_dump(mode="json", exclude_unset=True)
    return _idem(
        principal,
        f"PATCH /schedules/{schedule_id}",
        key,
        [expected, changes],
        lambda conn: (
            200,
            {
                "data": sched.view(
                    conn, sched.update_schedule(conn, principal, schedule_id, expected, dict(changes)), with_runs=True
                )
            },
        ),
    )


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    confirmed_policy_hash: str = Field(min_length=64, max_length=64)
    confirmation: bool


@router.post("/schedules/{schedule_id}/approve-auto-send", summary="Confirm the auto-send policy for this version")
def approve(
    schedule_id: uuid.UUID,
    body: ApproveIn,
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    if not body.confirmation:
        raise ApiError(422, "CONFIRMATION_REQUIRED", "Confirm the recipients, scope and timing first.")
    return _idem(
        principal,
        f"POST /schedules/{schedule_id}/approve-auto-send",
        key,
        body.model_dump(),
        lambda conn: (
            200,
            {
                "data": sched.view(
                    conn, sched.approve(conn, principal, schedule_id, body.version, body.confirmed_policy_hash)
                )
            },
        ),
    )


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_start: date | None = None
    period_end: date | None = None
    mode: Literal["DRAFT_ONLY", "AUTO_SEND"] = "DRAFT_ONLY"


@router.post(
    "/schedules/{schedule_id}/run", status_code=202, summary="Run now for a completed period (draft by default)"
)
def run_now(
    schedule_id: uuid.UUID,
    body: RunIn,
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect(conn):
        status, run = sched.run_now(conn, principal, schedule_id, body.period_start, body.period_end, body.mode)
        return status, {"data": sched.run_view(run)}

    return _idem(principal, f"POST /schedules/{schedule_id}/run", key, body.model_dump(mode="json"), effect)


@router.get("/schedules/{schedule_id}/runs", summary="Run history (each run keeps its schedule version)")
def runs(
    schedule_id: uuid.UUID,
    state: str | None = Query(default=None, max_length=20),
    principal: Principal = Depends(require(Role.SENDER)),
) -> dict:
    sr = t.schedule_run
    with tenant_tx(principal.tenant_id) as conn:
        sched.require_enabled(conn, principal.tenant_id)
        sched.load(conn, principal, schedule_id)
        q = select(sr).where(sr.c.schedule_id == schedule_id)
        if state:
            q = q.where(sr.c.state == state)
        return {"data": [sched.run_view(r) for r in conn.execute(q.order_by(sr.c.created_at.desc()).limit(100))]}


@router.post(
    "/schedule-runs/{run_id}/cancel", status_code=202, summary="Cancel queued work (never a handed-over email)"
)
def cancel(
    run_id: uuid.UUID,
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    return _idem(
        principal,
        f"POST /schedule-runs/{run_id}/cancel",
        key,
        None,
        lambda conn: (202, {"data": sched.run_view(sched.cancel_run(conn, principal, run_id))}),
    )


# --- exceptions ------------------------------------------------------------------------------------------


@router.get("/exceptions", summary="Open exceptions the caller may see (A1)")
def list_exceptions(
    status: Literal["LIVE", "OPEN", "ACKNOWLEDGED", "RESOLVED", "DISMISSED"] = Query(default="LIVE"),
    kind: str | None = Query(default=None, max_length=40),
    principal: Principal = Depends(require()),
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {
            "data": exc_engine.list_items(conn, principal, status, kind),
            "counts": exc_engine.counts(conn, principal),
        }


@router.get("/exceptions/{exception_id}/history", summary="Every status change of one exception")
def exception_history(exception_id: uuid.UUID, principal: Principal = Depends(require())) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": exc_engine.history(conn, principal, exception_id)}


class ExceptionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["ACKNOWLEDGE", "RESOLVE", "DISMISS"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/exceptions/{exception_id}/actions", summary="Acknowledge, resolve or dismiss (with a note)")
def act(
    exception_id: uuid.UUID,
    body: ExceptionAction,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    if body.action != "ACKNOWLEDGE" and not (body.note and len(body.note.strip()) >= 3):
        raise validation_failed([Issue("NOTE_REQUIRED", "Explain the resolution or dismissal.", "note")])
    return _idem(
        principal,
        f"POST /exceptions/{exception_id}/actions",
        key,
        body.model_dump(),
        lambda conn: (200, {"data": exc_engine.act(conn, principal, exception_id, body.action, body.note)}),
    )


# --- notifications -----------------------------------------------------------------------------------------


@router.get("/notifications", summary="The caller's reminders and alerts, newest first")
def notifications(principal: Principal = Depends(require())) -> dict:
    n = t.notification
    with tenant_tx(principal.tenant_id) as conn:
        rows = conn.execute(
            select(n).where(n.c.recipient_id == principal.membership_id).order_by(n.c.created_at.desc()).limit(50)
        ).all()
        unread = conn.execute(
            select(func.count()).where(n.c.recipient_id == principal.membership_id, n.c.read_at.is_(None))
        ).scalar_one()
    return {
        "data": [
            {
                "id": str(x.id),
                "kind": x.kind,
                "title": x.title,
                "body": x.body,
                "link": x.link,
                "day": x.day.isoformat() if x.day else None,
                "read": x.read_at is not None,
                "email_state": x.email_state,
                "created_at": x.created_at.isoformat(),
            }
            for x in rows
        ],
        "unread": unread,
    }


class ReadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


@router.post("/notifications/read", summary="Mark notifications read (all when ids is empty)")
def mark_read(
    body: ReadIn, principal: Principal = Depends(require(mutation=True)), key: str | None = Depends(idempotency_key)
) -> JSONResponse:
    ids = body.ids
    n = t.notification

    def effect(conn):
        q = update(n).where(n.c.recipient_id == principal.membership_id, n.c.read_at.is_(None))
        if ids:
            q = q.where(n.c.id.in_(ids))
        done = conn.execute(q.values(read_at=func.now()).returning(n.c.id)).all()
        if (
            ids
            and not done
            and not conn.execute(
                select(n.c.id).where(n.c.id.in_(ids), n.c.recipient_id == principal.membership_id)
            ).first()
        ):
            raise not_found()
        return 200, {"data": {"marked": len(done)}}

    return _idem(principal, "POST /notifications/read", key, {"ids": [str(x) for x in ids]}, effect)
