"""Production exception engine (addendum A1; spec "AUTOMATION FEATURES TO IMPLEMENT" A).

A periodic scan evaluates explainable rules over current state and keeps one live item per condition
(dedupe_key). Items open when a condition appears and resolve automatically when it clears; a person can
acknowledge, resolve or dismiss them, and every change is an append-only exception_event. A dismissed
condition is not reopened while it persists. Exceptions never change production data.

Rules (thresholds in company settings `exception_rules`):
  ENTRY_MISSING_FIELDS, MACHINE_DEPARTMENT_MISMATCH, DUPLICATE_UNRESOLVED   entries waiting for review
  UNUSUAL_PRODUCTION, UNUSUAL_STOP                                         approved records (recent days)
  MISSING_SUBMISSION                                                       control tower, today past cutoff
  SYNC_FAILURE, POWERBI_STALE                                              integrations (administrators)
  REPORT_FAILED, EMAIL_UNKNOWN, SCHEDULE_RUN_FAILED                        reporting (Reviewers, Senders)
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.company import DEFAULTS
from app.core.errors import conflict, forbidden, not_found
from app.db import tables as t
from app.domain.enums import Role
from app.domain.metrics import achievement_pct, format_pct
from app.integrations import service as integ

ex, ev = t.exception_item, t.exception_event
LIVE = ("OPEN", "ACKNOWLEDGED")


@dataclass
class Finding:
    kind: str
    severity: str
    audience: str
    reason: str
    object_type: str
    object_id: uuid.UUID | None
    dedupe_key: str
    department_id: uuid.UUID | None = None
    production_date: date | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def rules_of(settings: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULTS["exception_rules"], **((settings or {}).get("exception_rules") or {})}


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


# --- rules ------------------------------------------------------------------------------------------


def _entry_findings(conn: Connection) -> list[Finding]:
    c = t.candidate
    rows = conn.execute(
        select(c.c.id, c.c.issues, c.c.fields, t.batch.c.department_id)
        .join(t.batch, t.batch.c.id == c.c.batch_id)
        .where(c.c.state == "NEEDS_REVIEW")
    ).all()
    out = []
    for r in rows:
        codes = {i.get("code") for i in (r.issues or [])}
        day = _date((r.fields.get("production_date") or {}).get("value"))
        common = {
            "object_type": "candidate",
            "object_id": r.id,
            "department_id": r.department_id,
            "production_date": day,
            "audience": "DEPARTMENT",
        }
        missing = sorted(
            i.get("field") for i in (r.issues or []) if i.get("code") == "MISSING_VALUE" and i.get("field")
        )
        if missing:
            out.append(
                Finding(
                    "ENTRY_MISSING_FIELDS",
                    "WARNING",
                    reason=f"Required values missing: {', '.join(missing)}.",
                    dedupe_key=f"candidate:{r.id}:missing",
                    detail={"fields": missing},
                    **common,
                )
            )
        if "MACHINE_DEPARTMENT_MISMATCH" in codes:
            out.append(
                Finding(
                    "MACHINE_DEPARTMENT_MISMATCH",
                    "WARNING",
                    reason="The machine does not belong to the entry's department.",
                    dedupe_key=f"candidate:{r.id}:machine",
                    **common,
                )
            )
        if "DUPLICATE_UNRESOLVED" in codes:
            out.append(
                Finding(
                    "DUPLICATE_UNRESOLVED",
                    "WARNING",
                    reason="Possible duplicate of an existing record or file; a reviewer must decide.",
                    dedupe_key=f"candidate:{r.id}:duplicate",
                    **common,
                )
            )
    return out


def _record_findings(conn: Connection, rules: dict[str, Any], today: date) -> list[Finding]:
    r, rev = t.production_record, t.record_revision
    since = today - timedelta(days=int(rules["lookback_days"]) - 1)
    rows = conn.execute(
        select(
            r.c.id,
            rev.c.number,
            rev.c.production_date,
            rev.c.department_id,
            rev.c.production_qty,
            rev.c.target_qty,
            rev.c.unit,
            rev.c.stop_minutes,
            t.machine.c.code,
        )
        .join(rev, rev.c.id == r.c.current_revision_id)
        .join(t.machine, t.machine.c.id == rev.c.machine_id)
        .where(r.c.state == "ACTIVE", rev.c.production_date >= since, rev.c.production_date <= today)
    ).all()
    low, high, stop = (
        Decimal(str(rules["low_achievement_pct"])),
        Decimal(str(rules["high_achievement_pct"])),
        int(rules["stop_minutes"]),
    )
    out = []
    for x in rows:
        common = {
            "object_type": "production_record",
            "object_id": x.id,
            "department_id": x.department_id,
            "production_date": x.production_date,
            "audience": "DEPARTMENT",
        }
        ach = achievement_pct(Decimal(x.production_qty), Decimal(x.target_qty))
        if ach is not None and (ach < low or ach > high):
            out.append(
                Finding(
                    "UNUSUAL_PRODUCTION",
                    "WARNING" if ach < low else "INFO",
                    reason=f"{x.code}: achievement {format_pct(ach)}% is outside the expected {low}-{high}% range. "
                    "Check the entry; this is not a finding about its cause.",
                    dedupe_key=f"record:{x.id}:rev{x.number}:production",
                    detail={"achievement_pct": format_pct(ach)},
                    **common,
                )
            )
        if x.stop_minutes >= stop:
            out.append(
                Finding(
                    "UNUSUAL_STOP",
                    "WARNING",
                    reason=f"{x.code}: {x.stop_minutes} stop minutes recorded (threshold {stop}).",
                    dedupe_key=f"record:{x.id}:rev{x.number}:stop",
                    detail={"stop_minutes": x.stop_minutes},
                    **common,
                )
            )
    return out


def _submission_findings(conn: Connection, tenant_id: uuid.UUID, day: date) -> list[Finding]:
    from app.control.service import control_tower

    tower = control_tower(conn, system_principal(conn, tenant_id), day)
    return [
        Finding(
            "MISSING_SUBMISSION",
            "WARNING",
            "DEPARTMENT",
            reason=f"{d['name']} has no production entry for {day.isoformat()} and the "
            f"{tower['cutoff_local_time']} cutoff has passed.",
            object_type="department",
            object_id=uuid.UUID(d["department_id"]),
            dedupe_key=f"submission:{d['department_id']}:{day.isoformat()}",
            department_id=uuid.UUID(d["department_id"]),
            production_date=day,
        )
        for d in tower["departments"]
        if d["status"] == "MISSING"
    ]


def _integration_findings(conn: Connection, tenant_id: uuid.UUID) -> list[Finding]:
    out = []
    c, rs = t.integration_connection, t.record_sync
    for row in conn.execute(select(c).where(c.c.state != "DISCONNECTED")).all():
        failed = conn.execute(
            select(func.count()).where(rs.c.connection_id == row.id, rs.c.state.in_(("FAILED", "CONFLICT")))
        ).scalar_one()
        if row.state in ("CONFLICT", "RECONNECT_REQUIRED", "TEST_FAILED") or failed:
            what = row.state.replace("_", " ").lower() if row.state != "CONNECTED" else f"{failed} record(s) not synced"
            out.append(
                Finding(
                    "SYNC_FAILURE",
                    "WARNING",
                    "ADMIN",
                    reason=f"{row.name}: {what}.",
                    object_type="integration_connection",
                    object_id=row.id,
                    dedupe_key=f"connection:{row.id}:sync",
                    detail={"failed_records": failed, "state": row.state},
                )
            )
    pbi = integ.powerbi_status(conn, tenant_id)
    if pbi["state"] in ("STALE", "FAILED"):
        out.append(
            Finding(
                "POWERBI_STALE",
                "INFO",
                "ADMIN",
                reason=f"Power BI is {pbi['state'].lower()}: it holds data "
                f"version {pbi.get('refreshed_data_version')} of {pbi.get('data_version')}.",
                object_type="integration_connection",
                object_id=uuid.UUID(pbi["connection_id"]),
                dedupe_key=f"connection:{pbi['connection_id']}:powerbi",
            )
        )
    return out


def _reporting_findings(conn: Connection) -> list[Finding]:
    out = []
    for r in conn.execute(
        select(t.report.c.id, t.report.c.title, t.report.c.version).where(t.report.c.state == "FAILED")
    ).all():
        out.append(
            Finding(
                "REPORT_FAILED",
                "WARNING",
                "REPORTING",
                reason=f"The PDF for {r.title} (version {r.version}) could not be created.",
                object_type="report",
                object_id=r.id,
                dedupe_key=f"report:{r.id}:failed",
            )
        )
    for e in conn.execute(
        select(t.email_message.c.id, t.email_message.c.subject).where(t.email_message.c.state == "UNKNOWN")
    ).all():
        out.append(
            Finding(
                "EMAIL_UNKNOWN",
                "CRITICAL",
                "REPORTING",
                reason=f"Outcome of '{e.subject}' is unknown; reconcile it before any resend.",
                object_type="email_message",
                object_id=e.id,
                dedupe_key=f"email:{e.id}:unknown",
            )
        )
    sr = t.schedule_run
    for run in conn.execute(
        select(sr.c.id, sr.c.state, sr.c.error_message, t.schedule.c.name)
        .join(t.schedule, t.schedule.c.id == sr.c.schedule_id)
        .where(sr.c.state.in_(("FAILED", "HALTED")))
    ).all():
        out.append(
            Finding(
                "SCHEDULE_RUN_FAILED",
                "CRITICAL" if run.state == "HALTED" else "WARNING",
                "REPORTING",
                reason=f"Scheduled report '{run.name}': {run.error_message or run.state.lower()}.",
                object_type="schedule_run",
                object_id=run.id,
                dedupe_key=f"schedule_run:{run.id}",
            )
        )
    return out


def system_principal(conn: Connection, tenant_id: uuid.UUID) -> Principal:
    """Read-only principal covering every active department, for system scans only (never for sends)."""
    depts = conn.execute(select(t.department.c.id).where(t.department.c.active)).scalars()
    tz = conn.execute(select(t.tenant.c.timezone).where(t.tenant.c.id == tenant_id)).scalar_one()
    return Principal(
        membership_id=uuid.UUID(int=0),
        tenant_id=tenant_id,
        session_id=uuid.UUID(int=0),
        subject="system",
        display_name="System",
        email=None,
        roles=frozenset({Role.VIEWER}),
        department_ids=frozenset(depts),
        timezone=tz,
        auth_method="system",
    )


# --- scan -------------------------------------------------------------------------------------------------


def scan(conn: Connection, tenant_id: uuid.UUID, today: date) -> dict[str, int]:
    settings = conn.execute(select(t.tenant.c.settings).where(t.tenant.c.id == tenant_id)).scalar_one()
    rules = rules_of(settings)
    findings = (
        _entry_findings(conn)
        + _record_findings(conn, rules, today)
        + _submission_findings(conn, tenant_id, today)
        + _integration_findings(conn, tenant_id)
        + _reporting_findings(conn)
    )
    by_key = {f.dedupe_key: f for f in findings}
    live = {r.dedupe_key: r for r in conn.execute(select(ex).where(ex.c.status.in_(LIVE))).all()}
    dismissed = set(
        conn.execute(
            select(ex.c.dedupe_key).where(ex.c.status == "DISMISSED", ex.c.dedupe_key.in_(list(by_key)))
        ).scalars()
    )
    opened = resolved = 0
    for key, f in by_key.items():
        if key in live:
            conn.execute(update(ex).where(ex.c.id == live[key].id).values(last_seen_at=func.now(), reason=f.reason))
            continue
        if key in dismissed:
            continue
        new_id = conn.execute(
            pg_insert(ex)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                kind=f.kind,
                severity=f.severity,
                audience=f.audience,
                reason=f.reason,
                object_type=f.object_type,
                object_id=f.object_id,
                department_id=f.department_id,
                production_date=f.production_date,
                dedupe_key=key,
                detail=f.detail,
            )
            .on_conflict_do_nothing()
            .returning(ex.c.id)
        ).scalar_one_or_none()
        if new_id:
            conn.execute(
                insert(ev).values(
                    id=uuid.uuid4(), tenant_id=tenant_id, exception_id=new_id, action="OPENED", note=f.reason
                )
            )
            opened += 1
    for key, row in live.items():
        if key not in by_key:
            conn.execute(
                update(ex)
                .where(ex.c.id == row.id)
                .values(
                    status="RESOLVED",
                    resolved_at=func.now(),
                    resolution="The condition no longer applies.",
                    version=ex.c.version + 1,
                )
            )
            conn.execute(
                insert(ev).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    exception_id=row.id,
                    action="AUTO_RESOLVED",
                    note="The condition no longer applies.",
                )
            )
            resolved += 1
    return {"open": len(by_key) - len(dismissed & set(by_key)), "opened": opened, "auto_resolved": resolved}


# --- API side ---------------------------------------------------------------------------------------------


def visible(principal: Principal):
    """Department items need a grant and a Reviewer/Admin role; reporting items Reviewer/Sender/Admin;
    integration items Admin only."""
    conds = []
    if principal.has_any(Role.REVIEWER, Role.ADMIN):
        conds.append(
            and_(
                ex.c.audience == "DEPARTMENT",
                or_(ex.c.department_id.is_(None), ex.c.department_id.in_(list(principal.department_ids))),
            )
        )
    if principal.has_any(Role.REVIEWER, Role.SENDER, Role.ADMIN):
        conds.append(ex.c.audience == "REPORTING")
    if principal.has_any(Role.ADMIN):
        conds.append(ex.c.audience == "ADMIN")
    return or_(*conds) if conds else ex.c.id.is_(None)


LINKS = {
    "candidate": None,
    "production_record": "/records/{id}",
    "department": "/control-tower",
    "integration_connection": "/settings/integrations",
    "report": "/reports/{id}",
    "email_message": "/emails/{id}",
    "schedule_run": "/automation",
}


def item_view(r: Any, batch_id: uuid.UUID | None = None) -> dict[str, Any]:
    link = LINKS.get(r.object_type)
    if r.object_type == "candidate" and batch_id:
        link = f"/batches/{batch_id}/review?candidate={r.object_id}"
    elif link and "{id}" in link:
        link = link.format(id=r.object_id)
    return {
        "id": str(r.id),
        "kind": r.kind,
        "severity": r.severity,
        "audience": r.audience,
        "reason": r.reason,
        "object_type": r.object_type,
        "object_id": str(r.object_id) if r.object_id else None,
        "link": link,
        "department_id": str(r.department_id) if r.department_id else None,
        "production_date": r.production_date.isoformat() if r.production_date else None,
        "status": r.status,
        "first_seen_at": r.first_seen_at.isoformat(),
        "last_seen_at": r.last_seen_at.isoformat(),
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "resolution": r.resolution,
        "version": r.version,
    }


def list_items(conn: Connection, principal: Principal, status: str | None, kind: str | None) -> list[dict[str, Any]]:
    q = (
        select(ex, t.candidate.c.batch_id)
        .outerjoin(t.candidate, and_(ex.c.object_type == "candidate", t.candidate.c.id == ex.c.object_id))
        .where(visible(principal))
    )
    q = q.where(ex.c.status.in_(LIVE)) if status in (None, "LIVE") else q.where(ex.c.status == status)
    if kind:
        q = q.where(ex.c.kind == kind)
    rows = conn.execute(q.order_by(ex.c.severity.desc(), ex.c.last_seen_at.desc()).limit(200)).all()
    order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    return sorted((item_view(r, r.batch_id) for r in rows), key=lambda x: order[x["severity"]])


def counts(conn: Connection, principal: Principal) -> dict[str, int]:
    rows = conn.execute(
        select(ex.c.severity, func.count()).where(visible(principal), ex.c.status.in_(LIVE)).group_by(ex.c.severity)
    ).all()
    return {"CRITICAL": 0, "WARNING": 0, "INFO": 0} | dict(rows)


def act(conn: Connection, principal: Principal, exception_id: uuid.UUID, action: str, note: str | None) -> dict:
    if not principal.has_any(Role.REVIEWER, Role.SENDER, Role.ADMIN):
        raise forbidden()
    row = conn.execute(select(ex).where(ex.c.id == exception_id, visible(principal)).with_for_update()).one_or_none()
    if row is None:
        raise not_found()
    if row.status not in LIVE:
        raise conflict("ALREADY_CLOSED", "This exception is already closed.")
    status = {"ACKNOWLEDGE": "ACKNOWLEDGED", "RESOLVE": "RESOLVED", "DISMISS": "DISMISSED"}[action]
    values: dict[str, Any] = {"status": status, "version": ex.c.version + 1}
    if status != "ACKNOWLEDGED":
        values |= {"resolved_at": func.now(), "resolved_by": principal.membership_id, "resolution": note}
    conn.execute(update(ex).where(ex.c.id == row.id).values(**values))
    conn.execute(
        insert(ev).values(
            id=uuid.uuid4(),
            tenant_id=row.tenant_id,
            exception_id=row.id,
            action=status,
            actor_id=principal.membership_id,
            note=note,
        )
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action=f"EXCEPTION_{status}",
        object_type="exception",
        object_id=row.id,
        reason=note,
        after={"kind": row.kind},
    )
    return item_view(conn.execute(select(ex).where(ex.c.id == row.id)).one())


def history(conn: Connection, principal: Principal, exception_id: uuid.UUID) -> list[dict[str, Any]]:
    row = conn.execute(select(ex.c.id).where(ex.c.id == exception_id, visible(principal))).one_or_none()
    if row is None:
        raise not_found()
    return [
        {
            "action": e.action,
            "note": e.note,
            "actor_id": str(e.actor_id) if e.actor_id else None,
            "at": e.created_at.isoformat(),
        }
        for e in conn.execute(select(ev).where(ev.c.exception_id == exception_id).order_by(ev.c.created_at))
    ]
