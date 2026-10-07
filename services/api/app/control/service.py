"""Operational control tower for one company-local day (A2 pending-entry monitoring, A9; spec v1.1 §3).

Per expected department (active, `expected_daily_submission`, granted to the caller):
  SUBMITTED        at least one approved record for the day
  REVIEW_PENDING   entries for the day are waiting for review
  PROCESSING       notes uploaded that day are still being scanned, read or extracted
  MISSING          nothing yet and the configured cutoff has passed on a working day
  AWAITING         nothing yet, before the cutoff
  NOT_EXPECTED     not a working day, or the department is not expected to submit daily
Later milestones add sync (M5), report/email (M6) and exception/reminder (M7) state; until then those
panels say plainly that they are not available rather than showing invented values.
"""

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, and_, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONPATH, UUID

from app.auth.principal import Principal
from app.automation import exceptions as exc_engine
from app.core.company import DEFAULTS
from app.db import tables as t
from app.domain.enums import Role
from app.integrations import service as integ
from app.records import query

RUNNING = ("QUEUED", "RUNNING", "RETRY_WAIT")
BLOCKING_ISSUE = '$[*] ? (@.severity == "error")'  # JSON path: the entry has at least one blocking problem


def control_tower(conn: Connection, principal: Principal, day: date | None) -> dict[str, Any]:
    tenant = conn.execute(select(t.tenant).where(t.tenant.c.id == principal.tenant_id)).one()
    settings = {**DEFAULTS, **(tenant.settings or {})}
    tz = ZoneInfo(tenant.timezone)
    now = datetime.now(tz)
    today = now.date()
    day = day or today
    cutoff = time.fromisoformat(settings["submission_cutoff_local_time"])
    working = day.isoweekday() in settings["working_days"]
    past_cutoff = day < today or (day == today and now.time() >= cutoff)

    grants = list(principal.department_ids)
    depts = conn.execute(
        select(t.department)
        .where(t.department.c.id.in_(grants), t.department.c.active)
        .order_by(t.department.c.sort_order, t.department.c.name)
    ).all()

    f = query.build_filter(principal, date_from=day, date_to=day)
    totals = query.aggregate(conn, f)
    approved_counts: dict[str, int] = {}
    for dep_id, n in ((x["department_id"], x["record_count"]) for x in totals["departments"]):
        approved_counts[dep_id] = approved_counts.get(dep_id, 0) + n

    c = t.candidate
    pending_rows = conn.execute(
        select(
            c.c.fields["department_id"]["value"].astext.label("dep"),
            func.count().label("n"),
            func.count().filter(c.c.issues.op("@?")(cast(literal(BLOCKING_ISSUE), JSONPATH))).label("blocked"),
        )
        .where(c.c.state == "NEEDS_REVIEW", c.c.fields["production_date"]["value"].astext == day.isoformat())
        .group_by("dep")
    ).all()
    pending = {row.dep: row for row in pending_rows}

    b, u, j = t.batch, t.upload, t.job
    local_day = func.date(func.timezone(tenant.timezone, b.c.created_at))
    processing_rows = conn.execute(
        select(b.c.department_id, func.count(func.distinct(u.c.id)))
        .join(u, u.c.batch_id == b.c.id)
        .outerjoin(j, and_(j.c.object_id == u.c.id, j.c.state.in_(RUNNING)))
        .where(local_day == day, or_(u.c.state.in_(("UPLOADING", "QUARANTINED")), j.c.id.is_not(None)))
        .group_by(b.c.department_id)
    ).all()
    processing = {str(dep): n for dep, n in processing_rows}
    failed_rows = conn.execute(
        select(b.c.department_id, func.count(func.distinct(u.c.id)))
        .join(u, u.c.batch_id == b.c.id)
        .outerjoin(j, and_(j.c.object_id == u.c.id, j.c.state == "FAILED"))
        .where(local_day == day, or_(u.c.state == "REJECTED", j.c.id.is_not(None)))
        .group_by(b.c.department_id)
    ).all()
    failed = {str(dep): n for dep, n in failed_rows}

    rejected = {
        dep: n
        for dep, n in conn.execute(
            select(c.c.fields["department_id"]["value"].astext.label("dep"), func.count())
            .where(c.c.state == "REJECTED", c.c.fields["production_date"]["value"].astext == day.isoformat())
            .group_by("dep")
        ).all()
    }
    rs, pr = t.record_sync, t.production_record
    sync_failed = {
        str(dep): n
        for dep, n in conn.execute(
            select(pr.c.department_id, func.count(func.distinct(pr.c.id)))
            .join(rs, rs.c.record_id == pr.c.id)
            .where(pr.c.production_date == day, rs.c.state.in_(("FAILED", "CONFLICT")))
            .group_by(pr.c.department_id)
        ).all()
    }

    departments = []
    for dep in depts:
        key = str(dep.id)
        p = pending.get(key)
        entry = {
            "department_id": key,
            "name": dep.name,
            "code": dep.code,
            "approved_records": approved_counts.get(key, 0),
            "pending_review": p.n if p else 0,
            "pending_blocked": p.blocked if p else 0,
            "processing": processing.get(key, 0),
            "failed": failed.get(key, 0),
            "rejected": rejected.get(key, 0),
            "sync_failed": sync_failed.get(key, 0),
            "metrics": [x for x in totals["departments"] if x["department_id"] == key],
        }
        if not (working and dep.expected_daily_submission):
            status = "NOT_EXPECTED" if not entry["approved_records"] else "SUBMITTED"
        elif entry["approved_records"]:
            status = "SUBMITTED"
        elif entry["pending_review"]:
            status = "REVIEW_PENDING"
        elif entry["processing"]:
            status = "PROCESSING"
        else:
            status = "MISSING" if past_cutoff else "AWAITING"
        departments.append(entry | {"status": status})

    counts = {
        s: sum(1 for x in departments if x["status"] == s)
        for s in ("SUBMITTED", "REVIEW_PENDING", "PROCESSING", "MISSING", "AWAITING", "NOT_EXPECTED")
    }
    return {
        "date": day.isoformat(),
        "timezone": tenant.timezone,
        "working_day": working,
        "cutoff_local_time": settings["submission_cutoff_local_time"],
        "past_cutoff": past_cutoff,
        "computed_at": datetime.now(tz).isoformat(timespec="seconds"),
        "data_version": tenant.data_version,
        "totals": totals,
        "status_counts": counts,
        "departments": departments,
        "review_queue": {
            "entries": sum(x["pending_review"] for x in departments),
            "with_problems": sum(x["pending_blocked"] for x in departments),
        },
        "exceptions": exc_engine.counts(conn, principal),
        "reminders": _reminders_state(conn, day, settings),
        "integrations": {
            "google_sheets": _sheets_state(conn),
            "power_bi": integ.powerbi_status(conn, principal.tenant_id) | {"available_from": "M5"},
            "reports": _reports_state(conn, principal, day),
            "email": _email_state(conn, principal, day, tenant.timezone),
        },
    }


def _sheets_state(conn: Connection) -> dict[str, Any]:
    """Connection state plus per-record sync counts (FR13); NOT_CONFIGURED without a connection."""
    row = integ.live(conn, "google_sheets")
    if row is None:
        return {"state": "NOT_CONFIGURED", "available_from": "M5"}
    counts = integ.sync_counts(conn, [row.id])[row.id]
    return {"state": row.state, "available_from": "M5", "last_sync_at": integ.view(row)["last_sync_at"],
            "sync": {s: counts.get(s, 0) for s in ("PENDING", "SYNCED", "FAILED", "CONFLICT")},
            "error_code": row.last_error_code}  # fmt: skip


def _reports_state(conn: Connection, principal: Principal, day: date) -> dict[str, Any]:
    """Reports covering the day that the caller may open: counts by state and outdated (FR16, FR18)."""
    if not principal.has_any(Role.REVIEWER, Role.SENDER):
        return {"state": "NOT_PERMITTED", "available_from": "M6"}
    rp = t.report
    scoped = rp.c.department_ids.op("<@")(cast(list(principal.department_ids), ARRAY(UUID(as_uuid=True))))
    rows = conn.execute(
        select(rp.c.state, (rp.c.outdated_at.is_not(None)).label("outdated"), func.count())
        .where(scoped, rp.c.date_from <= day, rp.c.date_to >= day)
        .group_by(rp.c.state, "outdated")
    ).all()
    counts = {"READY": 0, "OUTDATED": 0, "FAILED": 0, "IN_PROGRESS": 0}
    for state, outdated, n in rows:
        key = "OUTDATED" if outdated else {"QUEUED": "IN_PROGRESS", "GENERATING": "IN_PROGRESS"}.get(state, state)
        counts[key] += n
    state = "NONE" if not any(counts.values()) else ("ATTENTION" if counts["FAILED"] else "OK")
    return {"state": state, "available_from": "M6", "counts": counts}


def _email_state(conn: Connection, principal: Principal, day: date, timezone: str) -> dict[str, Any]:
    """Emails confirmed on the day, by state. ACCEPTED means accepted by the provider, not delivered."""
    if not principal.has_any(Role.SENDER):
        return {"state": "NOT_PERMITTED", "available_from": "M6"}
    em = t.email_message
    local_day = func.date(func.timezone(timezone, em.c.created_at))
    rows = conn.execute(select(em.c.state, func.count()).where(local_day == day).group_by(em.c.state)).all()
    counts = {s: 0 for s in ("QUEUED", "SENDING", "ACCEPTED", "UNKNOWN", "FAILED")} | dict(rows)
    state = "NONE" if not any(counts.values()) else ("ATTENTION" if counts["UNKNOWN"] or counts["FAILED"] else "OK")
    return {"state": state, "available_from": "M6", "counts": counts}


def _reminders_state(conn: Connection, day: date, settings: dict[str, Any]) -> dict[str, Any]:
    """A3: whether reminders are on, and how many went out for the day by stage."""
    cfg = {**DEFAULTS["reminders"], **(settings.get("reminders") or {})}
    n = t.notification
    rows = conn.execute(select(n.c.kind, func.count()).where(n.c.day == day).group_by(n.c.kind)).all()
    return {
        "enabled": cfg["enabled"],
        "sent": {k: 0 for k in ("REMINDER_FIRST", "REMINDER_SECOND", "ESCALATION")} | dict(rows),
    }
