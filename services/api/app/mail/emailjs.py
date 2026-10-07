"""EmailJS channel (EMAIL_PROVIDER=emailjs): the Sender's browser sends, the server stays the record of truth.

Flow for one confirmed draft (the intent is created by app.mail.service.send, exactly as for Microsoft Graph):
1. claim()   QUEUED -> SENDING, once. The report is re-checked (READY and current) at this moment, and the
             EmailJS template variables are built from the saved draft and the saved report snapshot. A second
             claim (double click, second tab, replay) is refused, so the browser can send at most once.
2. browser   emailjs.send(service, template, variables) with the public key (@emailjs/browser).
3. record_result()  SENDING -> ACCEPTED (EmailJS answered 200), FAILED (EmailJS refused) or UNKNOWN (no answer).
   EmailJS accepting a message is not delivery evidence, same as Graph.
A claimed send whose browser never reported back becomes UNKNOWN after STALE_MINUTES (sweep_stale), so it is
reconciled instead of being forgotten or sent twice.

Template variables are always strings: never null/undefined; missing optional values become "" or "N/A".
"""

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, and_, func, insert, select, update

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import conflict
from app.db import tables as t
from app.mail import service as mail
from app.reports import service as reports
from app.reports.facts import STATUS_LABEL, fmt_number

STALE_MINUTES = 15
em, ea, rs = t.email_message, t.email_attempt, t.email_recipient_status
# Every variable the project's EmailJS template may use (docs/runbooks/emailjs.md). Order is documentation only.
VARIABLES = (
    "to_email",
    "cc_email",
    "bcc_email",
    "reply_to",
    "from_name",
    "subject",
    "message",
    "report_title",
    "report_code",
    "report_version",
    "period",
    "date_from",
    "date_to",
    "timezone",
    "record_count",
    "department_count",
    "excluded_pending",
    "unit",
    "production_total",
    "target_total",
    "achievement_pct",
    "variance",
    "unit_summary",
    "department_rows",
    "status_summary",
    "stop_total_minutes",
    "summary",
    "generated_at",
    "email_reference",
)


def _s(value: Any, default: str = "") -> str:
    """Template-safe text: never None/undefined."""
    return default if value is None else str(value)


def template_params(conn: Connection, email: Any, draft: Any, report: Any, sender: Any) -> dict[str, str]:
    facts, metrics = report.facts_json, report.metrics_json
    units = metrics.get("metrics", [])
    names = dict(conn.execute(select(t.department.c.id, t.department.c.name)).all())

    def pct(v: str | None) -> str:
        return "N/A" if v is None else f"{v}%"

    unit_lines = [
        f"{u['unit']}: production {fmt_number(u['production_qty'])} {u['unit']} against target "
        f"{fmt_number(u['target_qty'])} {u['unit']}; achievement {pct(u['achievement_pct'])}; "
        f"variance {fmt_number(u['variance'], signed=True)} {u['unit']}; {u['record_count']} records"
        for u in units
    ]
    dept_lines = [
        f"{x.get('department_name') or names.get(x['department_id'], 'Department')} ({x['unit']}): "
        f"{fmt_number(x['production_qty'])} of {fmt_number(x['target_qty'])} {x['unit']}, {pct(x['achievement_pct'])}"
        for x in metrics.get("departments", [])
    ]
    counts = metrics.get("status_counts", {})
    single = units[0] if len(units) == 1 else None
    multi = "see unit summary" if len(units) > 1 else "N/A"
    tz = ZoneInfo(report.timezone)
    recipients = {k: [r["address"] for r in draft.recipients if r["kind"] == k] for k in ("TO", "CC", "BCC")}
    return {
        "to_email": ", ".join(recipients["TO"]),
        "cc_email": ", ".join(recipients["CC"]),
        "bcc_email": ", ".join(recipients["BCC"]),
        "reply_to": _s(sender.email),
        "from_name": _s(sender.display_name, "Production Team"),
        "subject": _s(draft.subject),
        "message": _s(draft.body),
        "report_title": _s(report.title),
        "report_code": reports.code_for(report.series_id),
        "report_version": str(report.version),
        "period": _s(facts.get("period", {}).get("label")),
        "date_from": report.date_from.isoformat(),
        "date_to": report.date_to.isoformat(),
        "timezone": _s(report.timezone),
        "record_count": str(report.record_count),
        "department_count": _s(facts.get("department_count"), "0"),
        "excluded_pending": _s(facts.get("excluded_pending"), "0"),
        "unit": single["unit"] if single else multi,
        "production_total": fmt_number(single["production_qty"]) if single else multi,
        "target_total": fmt_number(single["target_qty"]) if single else multi,
        "achievement_pct": pct(single["achievement_pct"]) if single else multi,
        "variance": fmt_number(single["variance"], signed=True) if single else multi,
        "unit_summary": "\n".join(unit_lines) or "No approved records in this period.",
        "department_rows": "\n".join(dept_lines) or "No approved records in this period.",
        "status_summary": " · ".join(f"{STATUS_LABEL.get(s, s).capitalize()} {counts.get(s, 0)}" for s in STATUS_LABEL),
        "stop_total_minutes": _s(metrics.get("stop_total_minutes"), "0"),
        "summary": " ".join(s["text"] for s in reports.summary_of(report)) or "Summary not available.",
        "generated_at": report.created_at.astimezone(tz).strftime("%d %b %Y %H:%M"),
        "email_reference": str(email.id),
    }


def _load(conn: Connection, principal: Principal, email_id: uuid.UUID) -> Any:
    row = mail._email_with_report(conn, principal, email_id, lock=True)
    if row.channel != "emailjs":
        raise conflict("NOT_EMAILJS", "This email is sent by the server, not the browser.")
    return row


def claim(conn: Connection, principal: Principal, email_id: uuid.UUID) -> dict[str, Any]:
    row = _load(conn, principal, email_id)
    if row.state != "QUEUED":
        raise conflict("ALREADY_CLAIMED", "This email was already handed to EmailJS; it will not be sent again.")
    draft = conn.execute(select(t.email_draft).where(t.email_draft.c.id == row.draft_id)).one()
    report = conn.execute(select(t.report).where(t.report.c.id == row.report_id)).one()
    if report.state != "READY" or not reports.is_current(conn, report):  # latest saved data, checked now
        if report.state == "READY":
            reports.mark_outdated(conn, report, "RECORDS_CHANGED")
        conn.execute(
            update(em)
            .where(em.c.id == row.id)
            .values(
                state="FAILED",
                error_code="REPORT_OUTDATED",
                finished_at=func.now(),
                error_message="Records changed after confirmation. Generate a new version and a new draft.",
            )
        )
        conn.execute(
            update(rs)
            .where(rs.c.email_id == row.id)
            .values(state="FAILED", observed_at=func.now(), observation_source="provider_response")
        )
        # Returned, not raised: the FAILED state must be committed, and nothing is handed to EmailJS.
        return {
            "email_id": str(row.id),
            "error": {
                "code": "REPORT_OUTDATED",
                "message": "Records changed after confirmation. Generate a new version and a new draft.",
            },
        }
    sender = conn.execute(select(t.membership).where(t.membership.c.id == principal.membership_id)).one()
    params = template_params(conn, row, draft, report, sender)
    conn.execute(update(em).where(em.c.id == row.id).values(state="SENDING"))
    conn.execute(insert(ea).values(id=uuid.uuid4(), tenant_id=row.tenant_id, email_id=row.id, attempt_no=1))
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="EMAIL_CLIENT_SEND_CLAIMED",
        object_type="email_message",
        object_id=row.id,
        after={"channel": "emailjs"},
    )
    return {"email_id": str(row.id), "template_params": params}


def record_result(
    conn: Connection,
    principal: Principal,
    email_id: uuid.UUID,
    outcome: str,
    provider_status: int | None,
    provider_text: str | None,
) -> dict[str, Any]:
    row = _load(conn, principal, email_id)
    if row.state != "SENDING":
        raise conflict("NOT_SENDING", "No EmailJS send is in progress for this email.")
    text = (provider_text or "")[:300] or None
    code = {"ACCEPTED": None, "FAILED": "EMAILJS_REJECTED", "UNKNOWN": "EMAILJS_NO_ANSWER"}[outcome]
    conn.execute(
        update(ea)
        .where(ea.c.email_id == row.id, ea.c.outcome == "IN_FLIGHT")
        .values(
            outcome=outcome, http_status=provider_status, error_code=code, error_message=text, finished_at=func.now()
        )
    )
    values: dict[str, Any] = {"state": outcome, "error_code": code, "error_message": text, "finished_at": func.now()}
    if outcome == "ACCEPTED":
        values["accepted_at"] = func.now()
    conn.execute(update(em).where(em.c.id == row.id).values(**values))
    conn.execute(
        update(rs)
        .where(rs.c.email_id == row.id)
        .values(state=outcome, observation_source="provider_response", observed_at=func.now())
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action=f"EMAIL_{outcome}",
        object_type="email_message",
        object_id=row.id,
        after={"channel": "emailjs", "http_status": provider_status},
    )
    return mail.get_email(conn, principal, row.id)


def sweep_stale(conn: Connection, now: datetime) -> int:
    """Browser sends that claimed but never reported back become UNKNOWN (reconcile, never resend blindly)."""
    cutoff = now - timedelta(minutes=STALE_MINUTES)
    stale = select(ea.c.email_id).where(ea.c.outcome == "IN_FLIGHT", ea.c.started_at < cutoff)
    ids = (
        conn.execute(
            select(em.c.id).where(and_(em.c.channel == "emailjs", em.c.state == "SENDING", em.c.id.in_(stale)))
        )
        .scalars()
        .all()
    )
    for email_id in ids:
        conn.execute(
            update(ea)
            .where(ea.c.email_id == email_id, ea.c.outcome == "IN_FLIGHT")
            .values(
                outcome="UNKNOWN",
                error_code="EMAILJS_NO_REPORT",
                finished_at=func.now(),
                error_message="The browser did not report the EmailJS result.",
            )
        )
        conn.execute(
            update(em)
            .where(em.c.id == email_id)
            .values(
                state="UNKNOWN",
                error_code="EMAILJS_NO_REPORT",
                finished_at=func.now(),
                error_message="The browser did not report whether EmailJS accepted it. Check the EmailJS history.",
            )
        )
        conn.execute(
            update(rs)
            .where(rs.c.email_id == email_id)
            .values(state="UNKNOWN", observation_source="worker_recovery", observed_at=func.now())
        )
    return len(ids)
