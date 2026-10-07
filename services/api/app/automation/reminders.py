"""Pending-entry reminders and escalation (addenda A2, A3).

On a working day, once the company's submission cutoff has passed, departments expected to submit that still
have no entry are MISSING (control tower). Reminders follow company settings `reminders`:
  FIRST        first_after_minutes after the cutoff   -> the department's uploaders
  SECOND       second_after_minutes                   -> the department's uploaders
  ESCALATION   escalate_after_minutes                 -> reviewers (supervisors) granted the department
Only the latest due stage is sent (a scheduler that was down does not fire every stage at once), each
(day, department, stage, recipient) at most once, and a department that submits stops receiving them.
Notifications are in-app; email is optional and uses the connected mailbox. Every notification is audited.
"""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.expression import any_

from app.audit import service as audit
from app.automation.exceptions import system_principal
from app.core.company import DEFAULTS
from app.db import tables as t
from app.domain.enums import Role
from app.integrations import service as integ
from app.jobs import ledger

STAGES = (
    ("ESCALATION", "escalate_after_minutes"),
    ("SECOND", "second_after_minutes"),
    ("FIRST", "first_after_minutes"),
)
KIND = {"FIRST": "REMINDER_FIRST", "SECOND": "REMINDER_SECOND", "ESCALATION": "ESCALATION"}
EMAIL_KIND = "notification.email"


def _members(conn: Connection, department_id: uuid.UUID, role: Role) -> list[tuple[uuid.UUID, str | None]]:
    m, md = t.membership, t.membership_department
    return [
        (x.id, x.email)
        for x in conn.execute(
            select(m.c.id, m.c.email)
            .join(md, md.c.membership_id == m.c.id)
            .where(m.c.active, md.c.department_id == department_id, literal(role.value) == any_(m.c.roles))
        ).all()
    ]


def evaluate(conn: Connection, tenant_id: uuid.UUID, now: datetime) -> int:
    """Create due reminders for today. Returns the number of new notifications."""
    from app.control.service import control_tower

    stored = conn.execute(select(t.tenant.c.settings).where(t.tenant.c.id == tenant_id)).scalar_one() or {}
    cfg = {**DEFAULTS["reminders"], **(stored.get("reminders") or {})}
    if not cfg["enabled"]:
        return 0
    tower = control_tower(conn, system_principal(conn, tenant_id), None)
    if not tower["working_day"] or not tower["past_cutoff"]:
        return 0
    tz = ZoneInfo(tower["timezone"])
    day = date.fromisoformat(tower["date"])
    hh, mm = (int(x) for x in tower["cutoff_local_time"].split(":"))
    cutoff = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
    minutes = (now - cutoff).total_seconds() / 60
    stage = next((s for s, key in STAGES if minutes >= int(cfg[key])), None)
    if stage is None:
        return 0
    mail_ok = (
        bool(cfg["email"]) and (mail := integ.live(conn, "ms_graph_mail")) is not None and mail.state == "CONNECTED"
    )
    created = 0
    for d in (x for x in tower["departments"] if x["status"] == "MISSING"):
        dept_id = uuid.UUID(d["department_id"])
        role = Role.REVIEWER if stage == "ESCALATION" else Role.UPLOADER
        recipients = _members(conn, dept_id, role) or _members(conn, dept_id, Role.REVIEWER)
        title = (
            f"Escalation: {d['name']} has not submitted for {day.isoformat()}"
            if stage == "ESCALATION"
            else f"Reminder: {d['name']} production entry for {day.isoformat()} is missing"
        )
        body = (
            f"No production entry for {d['name']} on {day.isoformat()} has been received, and the "
            f"{tower['cutoff_local_time']} cutoff has passed. Upload the note or tell your supervisor if the "
            "department did not work."
        )
        for member_id, email in recipients:
            note_id = conn.execute(
                pg_insert(t.notification)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    recipient_id=member_id,
                    kind=KIND[stage],
                    dedupe_key=f"missing:{day.isoformat()}:{dept_id}:{stage}:{member_id}",
                    title=title,
                    body=body,
                    link="/uploads/new",
                    department_id=dept_id,
                    day=day,
                    email_state="QUEUED" if mail_ok and email else "NOT_REQUESTED",
                )
                .on_conflict_do_nothing()
                .returning(t.notification.c.id)
            ).scalar_one_or_none()
            if note_id is None:
                continue
            created += 1
            audit.record(
                conn,
                tenant_id=tenant_id,
                actor=audit.Actor("service", None),
                action=f"NOTIFICATION_{stage}",
                object_type="notification",
                object_id=note_id,
                after={
                    "department_id": str(dept_id),
                    "day": day.isoformat(),
                    "recipient": str(member_id),
                    "email": bool(mail_ok and email),
                },
            )
            if mail_ok and email:
                ledger.create_job(conn, tenant_id=tenant_id, kind=EMAIL_KIND, object_id=note_id, max_attempts=3)
    return created
