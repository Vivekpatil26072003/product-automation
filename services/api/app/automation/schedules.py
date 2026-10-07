"""Schedules (FR24; API operations 46-53, spec §12 "Scheduling rules").

- Optional module: every endpoint answers 404 FEATURE_DISABLED unless feature_flags.scheduling is on.
- A Sender owns a schedule for departments they are granted. Every content edit creates a new immutable
  version and revokes auto-send approval; pausing does not change the version.
- Auto-send is off unless feature_flags.auto_send is on AND the schedule's current policy (version, scope,
  recipients, sender mailbox, time zone, cadence, template) was confirmed by a Sender, and still matches at
  dispatch. Otherwise runs stop at a draft.
- Claiming: a due occurrence becomes a schedule_run whose unique key (schedule, version, period, kind)
  makes a second claim of the same period impossible. On recovery within 24 hours only the latest missed
  occurrence runs; older ones are recorded SKIPPED_MISSED and need an explicit Run now.
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.audit import service as audit
from app.auth.principal import Principal
from app.automation.occurrences import Cadence, latest_completed_period, next_occurrences, occurrences_between
from app.core.company import DEFAULTS
from app.core.config import get_settings
from app.core.errors import ApiError, Issue, conflict, forbidden, not_found, precondition_failed, validation_failed
from app.db import tables as t
from app.domain.enums import Role
from app.integrations import service as integ
from app.jobs import ledger
from app.mail.service import normalize_recipients
from app.reports.facts import TEMPLATE_VERSION

s, sv, sr = t.schedule, t.schedule_version, t.schedule_run
RUN_KIND = "schedule.run"
TERMINAL = ("DRAFTED", "SENT", "SKIPPED_EMPTY", "SKIPPED_MISSED", "FAILED", "CANCELLED", "HALTED")
MISSED_WINDOW = timedelta(hours=24)
OVERLAP_WAIT = timedelta(hours=2)
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    cadence: Literal["DAILY", "WEEKLY", "MONTHLY"]
    local_time: str
    weekday: int | None = Field(default=None, ge=1, le=7)
    monthday: int | None = Field(default=None, ge=1, le=31)
    timezone: str | None = None
    department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    units: list[Literal["m", "kg", "pcs"]] = Field(default_factory=list, max_length=3)
    title: str = Field(default="Daily Production Report", min_length=1, max_length=200)
    include_detail: bool = True
    to: list[str] = Field(default_factory=list, max_length=60)
    cc: list[str] = Field(default_factory=list, max_length=60)
    bcc: list[str] = Field(default_factory=list, max_length=60)
    subject: str | None = Field(default=None, max_length=200)
    mode: Literal["DRAFT_ONLY", "AUTO_SEND"] = "DRAFT_ONLY"
    empty_policy: Literal["SKIP", "DRAFT"] = "SKIP"

    @field_validator("local_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("use 24-hour HH:mm")
        return v


def settings_of(conn: Connection, tenant_id: uuid.UUID) -> dict[str, Any]:
    row = conn.execute(select(t.tenant.c.settings, t.tenant.c.timezone).where(t.tenant.c.id == tenant_id)).one()
    stored = row.settings or {}
    flags = {**DEFAULTS["feature_flags"], **stored.get("feature_flags", {})}
    return {"feature_flags": flags, "timezone": row.timezone}


def require_enabled(conn: Connection, tenant_id: uuid.UUID) -> dict[str, Any]:
    settings = settings_of(conn, tenant_id)
    if not settings["feature_flags"]["scheduling"]:
        raise ApiError(404, "FEATURE_DISABLED", "Scheduled reports are turned off for this company.")
    return settings


def cadence_of(config: dict[str, Any]) -> Cadence:
    hh, mm = (int(x) for x in config["local_time"].split(":"))
    return Cadence(config["cadence"], time(hh, mm), config["timezone"], config.get("weekday"), config.get("monthday"))


def validate_config(conn: Connection, principal: Principal, body: ScheduleInput) -> dict[str, Any]:
    """Checks cadence, time zone, scope within the owner's grants and recipients. Returns the stored config."""
    issues: list[Issue] = []
    tz = body.timezone or settings_of(conn, principal.tenant_id)["timezone"]
    config = body.model_dump(mode="json") | {"timezone": tz}
    try:
        cadence_of(config)
    except Exception as exc:  # noqa: BLE001 - ValueError or ZoneInfoNotFoundError
        field = "timezone" if "zone" in type(exc).__name__.lower() or "zone" in str(exc).lower() else "cadence"
        issues.append(Issue("INVALID_SCHEDULE", str(exc) if field == "cadence" else "Use an IANA time zone.", field))
    if body.cadence != "WEEKLY":
        config["weekday"] = None
    if body.cadence != "MONTHLY":
        config["monthday"] = None
    requested = set(body.department_ids)
    if requested - principal.department_ids:
        raise forbidden("You can only schedule reports for departments you are granted.")
    config["department_ids"] = sorted(str(x) for x in (requested or principal.department_ids))
    recipients, rissues = normalize_recipients(body.to, body.cc, body.bcc)
    issues += rissues
    if body.subject and any(ch in body.subject for ch in "\r\n"):
        issues.append(Issue("INVALID_SUBJECT", "The subject cannot contain line breaks.", "subject"))
    if body.mode == "AUTO_SEND" and not any(r["kind"] == "TO" for r in recipients):
        issues.append(Issue("NO_TO", "Automatic sending needs at least one To recipient.", "to"))
    if issues:
        raise validation_failed(issues)
    config.pop("to"), config.pop("cc"), config.pop("bcc")
    config["recipients"] = recipients
    return config


def policy_hash(schedule_id: uuid.UUID, version: int, config: dict[str, Any], sender_mailbox: str | None) -> str:
    """What a Sender approves for auto-send. Any change to any of these invalidates the approval."""
    doc = {
        "schedule": str(schedule_id),
        "version": version,
        "scope": config["department_ids"],
        "units": config["units"],
        "recipients": config["recipients"],
        "sender_mailbox": sender_mailbox,
        "timezone": config["timezone"],
        "cadence": [config["cadence"], config["local_time"], config.get("weekday"), config.get("monthday")],
        "template": TEMPLATE_VERSION,
        "title": config["title"],
        "subject": config.get("subject"),
        "empty_policy": config["empty_policy"],
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def sender_mailbox(conn: Connection) -> str | None:
    mail = integ.live(conn, "ms_graph_mail")
    return mail.config["sender_mailbox"] if mail is not None and mail.state == "CONNECTED" else None


def _now(conn: Connection) -> datetime:
    return conn.execute(select(func.now())).scalar_one()


def _next_due(config: dict[str, Any], after: datetime) -> datetime | None:
    occ = next_occurrences(cadence_of(config), after, 1)
    return occ[0].due_at if occ else None


# --- CRUD -----------------------------------------------------------------------------------------


def load(conn: Connection, principal: Principal, schedule_id: uuid.UUID, lock: bool = False) -> Any:
    if not principal.has_any(Role.SENDER):
        raise forbidden()
    q = select(s).where(s.c.id == schedule_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None or not principal.can_access_all({uuid.UUID(x) for x in row.config["department_ids"]}):
        raise not_found()
    return row


def create(conn: Connection, principal: Principal, body: ScheduleInput) -> Any:
    if not principal.has_any(Role.SENDER):
        raise forbidden()
    require_enabled(conn, principal.tenant_id)
    config = validate_config(conn, principal, body)
    sid = uuid.uuid4()
    now = _now(conn)
    conn.execute(
        insert(s).values(
            id=sid,
            tenant_id=principal.tenant_id,
            owner_id=principal.membership_id,
            name=body.name,
            config=config,
            last_due_at=now,
            next_due_at=_next_due(config, now),
        )
    )
    conn.execute(
        insert(sv).values(
            tenant_id=principal.tenant_id, schedule_id=sid, version=1, config=config, created_by=principal.membership_id
        )
    )
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="SCHEDULE_CREATED",
        object_type="schedule",
        object_id=sid,
        object_revision=1,
        after=config,
    )
    return conn.execute(select(s).where(s.c.id == sid)).one()


def update_schedule(
    conn: Connection, principal: Principal, schedule_id: uuid.UUID, expected: int, changes: dict[str, Any]
) -> Any:
    require_enabled(conn, principal.tenant_id)
    row = load(conn, principal, schedule_id, lock=True)
    if row.row_version != expected:
        raise precondition_failed(row.row_version)
    values: dict[str, Any] = {"row_version": s.c.row_version + 1}
    active = changes.pop("active", None)
    if changes:
        current = {k: v for k, v in row.config.items() if k in ScheduleInput.model_fields}
        for kind in ("to", "cc", "bcc"):
            current[kind] = [r["address"] for r in row.config["recipients"] if r["kind"] == kind.upper()]
        current["department_ids"] = row.config["department_ids"]
        body = ScheduleInput.model_validate({**current, "name": row.name, **changes})
        config = validate_config(conn, principal, body)
        if config != row.config or body.name != row.name:
            version = row.version + 1
            conn.execute(
                insert(sv).values(
                    tenant_id=row.tenant_id,
                    schedule_id=row.id,
                    version=version,
                    config=config,
                    created_by=principal.membership_id,
                )
            )
            values |= {
                "version": version,
                "config": config,
                "name": body.name,
                "approval_state": "UNAPPROVED",
                "approval_hash": None,
                "approved_version": None,
                "approved_by": None,
                "approved_at": None,
                "next_due_at": _next_due(config, _now(conn)),
            }
    if active is not None:
        values["active"] = bool(active)
        values["paused_reason"] = None if active else "PAUSED_BY_USER"
        if active:
            now = _now(conn)
            values |= {"last_due_at": now, "next_due_at": _next_due(values.get("config", row.config), now)}
    conn.execute(update(s).where(s.c.id == row.id).values(**values))
    after = conn.execute(select(s).where(s.c.id == row.id)).one()
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="SCHEDULE_UPDATED",
        object_type="schedule",
        object_id=row.id,
        object_revision=after.version,
        before={"version": row.version, "active": row.active, "approval": row.approval_state},
        after={"version": after.version, "active": after.active, "approval": after.approval_state},
    )
    return after


def approve(conn: Connection, principal: Principal, schedule_id: uuid.UUID, version: int, confirmed_hash: str) -> Any:
    settings = require_enabled(conn, principal.tenant_id)
    row = load(conn, principal, schedule_id, lock=True)
    if not settings["feature_flags"]["auto_send"]:
        raise conflict("AUTO_SEND_DISABLED", "Automatic sending is turned off for this company.")
    if get_settings().email_provider != "graph":
        raise conflict("BROWSER_CHANNEL", "Automatic sending needs server-side email; with EmailJS each email is "
                       "sent by a person from the browser. Scheduled runs prepare drafts.")  # fmt: skip
    if row.config["mode"] != "AUTO_SEND":
        raise conflict("NOT_AUTO_SEND", "This schedule only prepares drafts. Set the mode to automatic send first.")
    mailbox = sender_mailbox(conn)
    if mailbox is None:
        raise conflict("EMAIL_NOT_CONNECTED", "Email sending is not connected.")
    current = policy_hash(row.id, row.version, row.config, mailbox)
    if version != row.version or confirmed_hash != current:
        raise conflict("STALE_POLICY", "The schedule changed since you reviewed it. Review the policy again.")
    conn.execute(
        update(s)
        .where(s.c.id == row.id)
        .values(
            approval_state="APPROVED",
            approval_hash=current,
            approved_version=row.version,
            approved_by=principal.membership_id,
            approved_at=func.now(),
            row_version=s.c.row_version + 1,
        )
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="SCHEDULE_AUTO_SEND_APPROVED",
        object_type="schedule",
        object_id=row.id,
        object_revision=row.version,
        after={"policy_hash": current, "recipients": len(row.config["recipients"]), "mailbox": mailbox},
    )
    return conn.execute(select(s).where(s.c.id == row.id)).one()


def auto_send_allowed(conn: Connection, row: Any) -> tuple[bool, str | None]:
    """Recomputed at every dispatch: flag, mode, approval of this version, unchanged policy (incl. mailbox)."""
    if not settings_of(conn, row.tenant_id)["feature_flags"]["auto_send"]:
        return False, "AUTO_SEND_DISABLED"
    if get_settings().email_provider != "graph":  # EmailJS sends from a person's browser, never unattended
        return False, "BROWSER_CHANNEL"
    if row.config["mode"] != "AUTO_SEND":
        return False, "DRAFT_ONLY"
    if row.approval_state != "APPROVED" or row.approved_version != row.version:
        return False, "AUTO_SEND_NOT_APPROVED"
    if row.approval_hash != policy_hash(row.id, row.version, row.config, sender_mailbox(conn)):
        return False, "POLICY_CHANGED"
    return True, None


# --- claiming occurrences -------------------------------------------------------------------------


def _insert_run(
    conn: Connection,
    row: Any,
    *,
    period: tuple[date, date],
    kind: str,
    mode: str,
    due_at: datetime,
    state: str = "QUEUED",
    requested_by: uuid.UUID | None = None,
    note: str | None = None,
) -> Any:
    stmt = (
        pg_insert(sr)
        .values(
            id=uuid.uuid4(),
            tenant_id=row.tenant_id,
            schedule_id=row.id,
            version=row.version,
            period_start=period[0],
            period_end=period[1],
            run_kind=kind,
            mode=mode,
            due_at=due_at,
            state=state,
            requested_by=requested_by,
            note=note,
            finished_at=func.now() if state in TERMINAL else None,
        )
        .on_conflict_do_nothing(index_elements=["schedule_id", "version", "period_start", "period_end", "run_kind"])
        .returning(sr.c.id)
    )
    run_id = conn.execute(stmt).scalar_one_or_none()
    if run_id is None:
        return None
    if state == "QUEUED":
        ledger.create_job(conn, tenant_id=row.tenant_id, kind=RUN_KIND, object_id=run_id, created_by=requested_by)
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=audit.Actor("service", None),
        action=f"SCHEDULE_RUN_{state}",
        object_type="schedule_run",
        object_id=run_id,
        after={
            "schedule_id": str(row.id),
            "version": row.version,
            "period": [str(period[0]), str(period[1])],
            "kind": kind,
            "mode": mode,
        },
    )
    return run_id


def claim_due(conn: Connection, row: Any, now: datetime) -> list[uuid.UUID]:
    """Turn occurrences due since last_due_at into runs. Call with the schedule row locked."""
    cadence = cadence_of(row.config)
    due = occurrences_between(cadence, row.last_due_at or row.created_at, now)
    created = []
    for i, occ in enumerate(due):
        latest = i == len(due) - 1
        if latest and now - occ.due_at <= MISSED_WINDOW:
            run = _insert_run(
                conn,
                row,
                period=(occ.period_start, occ.period_end),
                kind="SCHEDULED",
                mode=row.config["mode"],
                due_at=occ.due_at,
            )
        else:  # backlog: recorded, never run implicitly
            run = _insert_run(
                conn,
                row,
                period=(occ.period_start, occ.period_end),
                kind="SCHEDULED",
                mode=row.config["mode"],
                due_at=occ.due_at,
                state="SKIPPED_MISSED",
                note="Missed while the scheduler was unavailable. Use Run now if it is still needed.",
            )
        if run:
            created.append(run)
    conn.execute(
        update(s)
        .where(s.c.id == row.id)
        .values(last_due_at=due[-1].due_at if due else (row.last_due_at or now), next_due_at=_next_due(row.config, now))
    )
    return created


def run_now(
    conn: Connection,
    principal: Principal,
    schedule_id: uuid.UUID,
    period_start: date | None,
    period_end: date | None,
    mode: str,
) -> tuple[int, Any]:
    require_enabled(conn, principal.tenant_id)
    row = load(conn, principal, schedule_id, lock=True)
    cadence = cadence_of(row.config)
    if period_start is None or period_end is None:
        period_start, period_end = latest_completed_period(cadence, _now(conn))
    today = _now(conn).astimezone(ZoneInfo(row.config["timezone"])).date()
    if period_start > period_end or period_end >= today:
        raise validation_failed([Issue("PERIOD_NOT_COMPLETE", "Choose a period that has already ended.", "period_end")])
    if (period_end - period_start).days > 366:
        raise validation_failed([Issue("DATE_RANGE", "Choose at most 366 days.", "period_start")])
    if mode == "AUTO_SEND":
        ok, why = auto_send_allowed(conn, row)
        if not ok:
            raise conflict(why or "AUTO_SEND_NOT_APPROVED", "Automatic sending is not approved for this version.")
    existing = conn.execute(
        select(sr).where(
            sr.c.schedule_id == row.id,
            sr.c.version == row.version,
            sr.c.period_start == period_start,
            sr.c.period_end == period_end,
            sr.c.run_kind == "MANUAL",
        )
    ).one_or_none()
    if existing is not None:  # the unique period/run kind: a second Run now returns the first run
        return 200, existing
    run_id = _insert_run(
        conn,
        row,
        period=(period_start, period_end),
        kind="MANUAL",
        mode=mode,
        due_at=_now(conn),
        requested_by=principal.membership_id,
    )
    return 202, conn.execute(select(sr).where(sr.c.id == run_id)).one()


def cancel_run(conn: Connection, principal: Principal, run_id: uuid.UUID) -> Any:
    run = conn.execute(select(sr).where(sr.c.id == run_id).with_for_update()).one_or_none()
    if run is None:
        raise not_found()
    load(conn, principal, run.schedule_id)
    if run.state in TERMINAL:
        return run
    if run.email_id is not None or run.state == "SENDING":
        raise conflict("TOO_LATE", "The email was already handed to the provider and cannot be recalled.")
    conn.execute(
        update(sr).where(sr.c.id == run.id).values(state="CANCELLED", cancel_requested=True, finished_at=func.now())
    )
    audit.record(
        conn,
        tenant_id=run.tenant_id,
        actor=principal.actor,
        action="SCHEDULE_RUN_CANCELLED",
        object_type="schedule_run",
        object_id=run.id,
    )
    return conn.execute(select(sr).where(sr.c.id == run.id)).one()


# --- views ------------------------------------------------------------------------------------------------


def _iso(v: datetime | None) -> str | None:
    return v.astimezone(UTC).isoformat(timespec="seconds") if v else None


def run_view(r: Any) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "schedule_id": str(r.schedule_id),
        "version": r.version,
        "period_start": r.period_start.isoformat(),
        "period_end": r.period_end.isoformat(),
        "run_kind": r.run_kind,
        "mode": r.mode,
        "state": r.state,
        "due_at": _iso(r.due_at),
        "report_id": str(r.report_id) if r.report_id else None,
        "draft_id": str(r.draft_id) if r.draft_id else None,
        "email_id": str(r.email_id) if r.email_id else None,
        "excluded_pending": r.excluded_pending,
        "note": r.note,
        "error": {"code": r.error_code, "message": r.error_message} if r.error_code else None,
        "created_at": _iso(r.created_at),
        "finished_at": _iso(r.finished_at),
    }


def view(conn: Connection, row: Any, with_runs: bool = False) -> dict[str, Any]:
    cfg = row.config
    mailbox = sender_mailbox(conn)
    allowed, why = auto_send_allowed(conn, row)
    data = {
        "id": str(row.id),
        "name": row.name,
        "version": row.version,
        "row_version": row.row_version,
        "owner_id": str(row.owner_id),
        "active": row.active,
        "paused_reason": row.paused_reason,
        "config": {k: v for k, v in cfg.items() if k != "recipients"},
        "to": [r["address"] for r in cfg["recipients"] if r["kind"] == "TO"],
        "cc": [r["address"] for r in cfg["recipients"] if r["kind"] == "CC"],
        "bcc": [r["address"] for r in cfg["recipients"] if r["kind"] == "BCC"],
        "approval_state": row.approval_state if row.approved_version == row.version else "UNAPPROVED",
        "auto_send_active": allowed,
        "auto_send_blocked_reason": why,
        "policy": {"hash": policy_hash(row.id, row.version, cfg, mailbox), "sender_mailbox": mailbox},
        "next_runs": [
            {
                "due_at": _iso(o.due_at),
                "local_date": o.local_date.isoformat(),
                "period_start": o.period_start.isoformat(),
                "period_end": o.period_end.isoformat(),
            }
            for o in next_occurrences(cadence_of(cfg), datetime.now(UTC))
        ]
        if row.active
        else [],
        "created_at": _iso(row.created_at),
    }
    if with_runs:
        runs = conn.execute(select(sr).where(sr.c.schedule_id == row.id).order_by(sr.c.created_at.desc()).limit(20))
        data["runs"] = [run_view(r) for r in runs.all()]
    return data


def preview(config: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "due_at": _iso(o.due_at),
            "local_date": o.local_date.isoformat(),
            "period_start": o.period_start.isoformat(),
            "period_end": o.period_end.isoformat(),
        }
        for o in next_occurrences(cadence_of(config), datetime.now(UTC))
    ]


def principal_for(conn: Connection, membership_id: uuid.UUID) -> Principal | None:
    """The owner's current authority, rebuilt from the database for a background send (spec §12)."""
    m = conn.execute(select(t.membership).where(t.membership.c.id == membership_id)).one_or_none()
    if m is None or not m.active:
        return None
    depts = conn.execute(
        select(t.membership_department.c.department_id)
        .join(t.department, t.department.c.id == t.membership_department.c.department_id)
        .where(t.membership_department.c.membership_id == m.id, t.department.c.active)
    ).scalars()
    tz = conn.execute(select(t.tenant.c.timezone).where(t.tenant.c.id == m.tenant_id)).scalar_one()
    return Principal(
        membership_id=m.id,
        tenant_id=m.tenant_id,
        session_id=uuid.UUID(int=0),
        subject=m.subject,
        display_name=m.display_name,
        email=m.email,
        roles=frozenset(Role(r) for r in m.roles),
        department_ids=frozenset(depts),
        timezone=tz,
        auth_method="scheduler",
    )
