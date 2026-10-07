"""M7 automation jobs (FR24, A1-A3).

automation.tick   (object = tenant)        claim due schedule occurrences, scan exceptions, send due reminders.
                                            Queued every minute per company by the worker loop (enqueue_ticks).
schedule.run      (object = schedule_run)  a small state machine, one step per job execution:
    QUEUED/WAITING -> (permissions rechecked) snapshot report -> REPORTING
    REPORTING      -> READY: draft with the schedule's recipients -> DRAFTED, or with a valid approval SENDING
    SENDING        -> ACCEPTED: SENT | FAILED: FAILED | UNKNOWN: HALTED (schedule paused, reconcile required)
  The owner's membership, Sender role and department grants are rebuilt from the database before every step;
  a revoked permission pauses the schedule and nothing is sent. One active run per schedule: a later run waits
  up to two hours, then fails visibly.
notification.email (object = notification) optional email copy of a reminder; an uncertain outcome is
  recorded, never retried blindly.
"""

import logging
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from app.audit import service as audit
from app.automation import exceptions, reminders
from app.automation import schedules as sched
from app.core.config import get_settings
from app.core.crypto import SecretsUnavailable, decrypt
from app.core.errors import ApiError
from app.db import tables as t
from app.db.engine import dispatcher_tx, tenant_tx
from app.domain.enums import Role
from app.integrations import adapter_for
from app.integrations import service as integ
from app.jobs import ledger
from app.mail import emailjs
from app.mail import service as mail
from app.ops import retention
from app.orders import service as orders
from app.records import query
from app.reports import service as reports
from workers.registry import Outcome, handler
from workers.runtime import JobContext

log = logging.getLogger("workers.automation")
SERVICE = audit.Actor("service", None)
sr, s = t.schedule_run, t.schedule
TICK = "automation.tick"
RETENTION_KIND = "retention.purge"
STEP_SECONDS = {"REPORTING": 5, "SENDING": 10, "WAITING": 300}


def enqueue_ticks() -> int:
    """Worker loop: one coalesced tick per company."""
    with dispatcher_tx() as conn:
        tenants = conn.execute(select(t.tenant.c.id)).scalars().all()
        for tenant_id in tenants:
            ledger.ensure_job(conn, tenant_id=tenant_id, kind=TICK, object_id=tenant_id, max_attempts=1)
    return len(tenants)


@handler(TICK)
def tick(ctx: JobContext) -> Outcome:
    tenant_id = ctx.claim.object_id
    result: dict[str, Any] = {"runs": 0}
    with ctx.transaction() as conn:
        now = conn.execute(select(func.now())).scalar_one()
        if sched.settings_of(conn, tenant_id)["feature_flags"]["scheduling"]:
            due = conn.execute(
                select(s).where(s.c.active, s.c.next_due_at <= now).with_for_update(skip_locked=True)
            ).all()
            for row in due:
                result["runs"] += len(sched.claim_due(conn, row, now))
        tz = conn.execute(select(t.tenant.c.timezone).where(t.tenant.c.id == tenant_id)).scalar_one()
        result["exceptions"] = exceptions.scan(conn, tenant_id, now.astimezone(ZoneInfo(tz)).date())
        result["reminders"] = reminders.evaluate(conn, tenant_id, now)
        result["emailjs_unreported"] = emailjs.sweep_stale(conn, now)
        result["order_emails_unreported"] = orders.sweep_stale(conn, now)
        if retention.due(conn, now):  # daily purge (FR25), one per company
            ledger.ensure_job(conn, tenant_id=tenant_id, kind=RETENTION_KIND, object_id=tenant_id, max_attempts=3)
    return Outcome(result=result)


@handler("retention.purge")
def retention_purge(ctx: JobContext) -> Outcome:
    return Outcome(result=retention.run(ctx.claim.tenant_id, dry_run=False))


# --- schedule runs -----------------------------------------------------------------------------------------


def _finish(conn, run: Any, state: str, *, code: str | None = None, message: str | None = None, **values: Any) -> None:
    conn.execute(
        update(sr)
        .where(sr.c.id == run.id)
        .values(state=state, error_code=code, error_message=message, finished_at=func.now(), **values)
    )
    audit.record(
        conn,
        tenant_id=run.tenant_id,
        actor=SERVICE,
        action=f"SCHEDULE_RUN_{state}",
        object_type="schedule_run",
        object_id=run.id,
        after={"code": code, **{k: str(v) for k, v in values.items()}},
    )


def _pause(conn, schedule_id: uuid.UUID, reason: str) -> None:
    conn.execute(
        update(s)
        .where(s.c.id == schedule_id)
        .values(active=False, paused_reason=reason, row_version=s.c.row_version + 1)
    )


def _again(conn, run: Any, seconds: float) -> None:
    ledger.ensure_job(conn, tenant_id=run.tenant_id, kind=sched.RUN_KIND, object_id=run.id, delay_seconds=seconds)


@handler(sched.RUN_KIND)
def run_schedule(ctx: JobContext) -> Outcome:
    with ctx.transaction() as conn:
        run = conn.execute(select(sr).where(sr.c.id == ctx.claim.object_id).with_for_update()).one()
        if run.state in sched.TERMINAL:
            return Outcome(result={"skipped": run.state})
        row = conn.execute(select(s).where(s.c.id == run.schedule_id).with_for_update()).one()
        if run.cancel_requested:
            _finish(conn, run, "CANCELLED")
            return Outcome(result={"state": "CANCELLED"})
        config = conn.execute(
            select(t.schedule_version.c.config).where(
                t.schedule_version.c.schedule_id == row.id, t.schedule_version.c.version == run.version
            )
        ).scalar_one()
        owner = sched.principal_for(conn, row.owner_id)
        scope = {uuid.UUID(x) for x in config["department_ids"]}
        problem = None
        if not sched.settings_of(conn, run.tenant_id)["feature_flags"]["scheduling"]:
            problem = ("FEATURE_DISABLED", "Scheduled reports were turned off.")
        elif owner is None or not owner.has_any(Role.SENDER) or not owner.can_access_all(scope):
            problem = ("PERMISSION_REVOKED", "The schedule owner no longer has the Sender role or every department.")
        if problem and run.state != "SENDING":  # an email already handed over is only observed, never re-sent
            _finish(conn, run, "FAILED", code=problem[0], message=problem[1])
            _pause(conn, row.id, problem[0])
            return Outcome(state="FAILED", error_code=problem[0], error_message=problem[1])
        now = conn.execute(select(func.now())).scalar_one()

        if run.state in ("QUEUED", "WAITING"):
            busy = conn.execute(
                select(sr.c.id).where(
                    sr.c.schedule_id == row.id,
                    sr.c.id != run.id,
                    sr.c.due_at < run.due_at,
                    sr.c.state.in_(("QUEUED", "WAITING", "REPORTING", "SENDING")),
                )
            ).first()
            if busy:
                if now - run.due_at > sched.OVERLAP_WAIT:
                    _finish(
                        conn,
                        run,
                        "FAILED",
                        code="OVERLAP",
                        message="An earlier run of this schedule was still "
                        "active after two hours. Review it, then use Run now.",
                    )
                    return Outcome(state="FAILED", error_code="OVERLAP", error_message="Earlier run still active.")
                conn.execute(update(sr).where(sr.c.id == run.id).values(state="WAITING"))
                _again(conn, run, STEP_SECONDS["WAITING"])
                return Outcome(result={"state": "WAITING"})

    if run.state in ("QUEUED", "WAITING"):
        return _snapshot(ctx, run, row, config, owner)
    if run.state == "REPORTING":
        return _after_report(ctx, run, row, config, owner)
    return _after_send(ctx, run, row)


def _snapshot(ctx: JobContext, run: Any, row: Any, config: dict[str, Any], owner: Any) -> Outcome:
    with tenant_tx(run.tenant_id, isolation="REPEATABLE READ") as conn:
        ledger.fence(conn, ctx.claim)
        f = query.build_filter(
            owner,
            date_from=run.period_start,
            date_to=run.period_end,
            department_ids=[uuid.UUID(x) for x in config["department_ids"]],
            units=config["units"],
        )
        try:
            out = reports.create_report(
                conn,
                owner,
                f,
                title=config["title"],
                include_detail=config["include_detail"],
                allow_empty=config["empty_policy"] == "DRAFT",
                supersedes=None,
            )
        except ApiError as exc:
            state = "SKIPPED_EMPTY" if exc.code == "EMPTY_PERIOD" else "FAILED"
            note = (
                "No approved records in the period; nothing was created or sent." if state == "SKIPPED_EMPTY" else None
            )
            _finish(
                conn,
                run,
                state,
                code=None if state == "SKIPPED_EMPTY" else exc.code,
                message=None if state == "SKIPPED_EMPTY" else exc.message,
                note=note,
            )
            return Outcome(result={"state": state})
        report = conn.execute(select(t.report).where(t.report.c.id == uuid.UUID(out["report_id"]))).one()
        conn.execute(
            update(sr)
            .where(sr.c.id == run.id)
            .values(state="REPORTING", report_id=report.id, excluded_pending=report.facts_json["excluded_pending"])
        )
        _again(conn, run, STEP_SECONDS["REPORTING"])
    return Outcome(result={"state": "REPORTING", "report_id": out["report_id"]})


def _after_report(ctx: JobContext, run: Any, row: Any, config: dict[str, Any], owner: Any) -> Outcome:
    with ctx.transaction() as conn:
        report = conn.execute(select(t.report).where(t.report.c.id == run.report_id)).one()
        if report.state == "FAILED":
            _finish(conn, run, "FAILED", code="REPORT_FAILED", message="The report PDF could not be created.")
            return Outcome(state="FAILED", error_code="REPORT_FAILED", error_message="PDF failed.")
        if report.state != "READY":
            _again(conn, run, STEP_SECONDS["REPORTING"])
            return Outcome(result={"state": "REPORTING"})
        try:
            draft = mail.create_draft(conn, owner, report.id)
            changes = {
                k: [r["address"] for r in config["recipients"] if r["kind"] == k.upper()] for k in ("to", "cc", "bcc")
            }
            if config.get("subject"):
                changes["subject"] = config["subject"]
            draft = mail.update_draft(conn, owner, draft.id, draft.version, changes)
        except ApiError as exc:
            _finish(conn, run, "FAILED", code=exc.code, message=exc.message)
            return Outcome(state="FAILED", error_code=exc.code, error_message=exc.message)
        allowed, why = sched.auto_send_allowed(conn, row)
        if run.mode == "AUTO_SEND" and row.version != run.version:
            allowed, why = False, "POLICY_CHANGED"
        if run.mode == "AUTO_SEND" and report.is_empty:
            allowed, why = False, "EMPTY_REPORT"  # an empty period never produces an automatic email
        if run.mode != "AUTO_SEND" or not allowed:
            note = None if run.mode != "AUTO_SEND" else f"Prepared as a draft only: {why}."
            _finish(conn, run, "DRAFTED", draft_id=draft.id, note=note)
            return Outcome(result={"state": "DRAFTED"})
        try:
            _, out = mail.send(
                conn, owner, draft.id, version=draft.version, confirmed_hash=draft.content_hash, if_match=draft.version
            )
        except ApiError as exc:
            _finish(conn, run, "DRAFTED", draft_id=draft.id, note=f"Prepared as a draft only: {exc.message}")
            return Outcome(result={"state": "DRAFTED", "blocked": exc.code})
        conn.execute(
            update(sr)
            .where(sr.c.id == run.id)
            .values(state="SENDING", draft_id=draft.id, email_id=uuid.UUID(out["data"]["email_id"]))
        )
        _again(conn, run, STEP_SECONDS["SENDING"])
    return Outcome(result={"state": "SENDING"})


def _after_send(ctx: JobContext, run: Any, row: Any) -> Outcome:
    with ctx.transaction() as conn:
        state = conn.execute(select(t.email_message.c.state).where(t.email_message.c.id == run.email_id)).scalar_one()
        if state == "ACCEPTED":
            _finish(conn, run, "SENT")
        elif state == "FAILED":
            _finish(conn, run, "FAILED", code="EMAIL_FAILED", message="The email was not sent.")
        elif state == "UNKNOWN":
            _finish(
                conn,
                run,
                "HALTED",
                code="EMAIL_UNKNOWN",
                message="The email outcome is unknown. Automation is paused until it is reconciled.",
            )
            _pause(conn, row.id, "UNKNOWN_SEND")
        else:
            _again(conn, run, STEP_SECONDS["SENDING"])
            return Outcome(result={"state": "SENDING"})
    return Outcome(result={"email": state})


# --- reminder email ------------------------------------------------------------------------------------------


@handler(reminders.EMAIL_KIND)
def email_notification(ctx: JobContext) -> Outcome:
    if get_settings().sends_paused:
        with ctx.transaction() as conn:
            ledger.ensure_job(conn, tenant_id=ctx.claim.tenant_id, kind=reminders.EMAIL_KIND,
                              object_id=ctx.claim.object_id, delay_seconds=300)  # fmt: skip
        return Outcome(result={"paused": True})
    with ctx.transaction() as conn:
        note = conn.execute(select(t.notification).where(t.notification.c.id == ctx.claim.object_id)).one()
        if note.email_state != "QUEUED":
            return Outcome(result={"skipped": note.email_state})
        member = conn.execute(select(t.membership).where(t.membership.c.id == note.recipient_id)).one()
        conn_row = integ.live(conn, "ms_graph_mail")
        # Mark before calling out: a crash after this point is reported as UNKNOWN, never re-sent.
        conn.execute(update(t.notification).where(t.notification.c.id == note.id).values(email_state="UNKNOWN"))
    if conn_row is None or conn_row.state != "CONNECTED" or not member.active or not member.email:
        return _note_email(ctx, note.id, "SKIPPED", "Email not available for this recipient.")
    try:
        secret = decrypt(conn_row.id, conn_row.secret_ciphertext, conn_row.secret_key_id)
    except (SecretsUnavailable, TypeError):
        return _note_email(ctx, note.id, "FAILED", "Stored email credentials cannot be read.")
    outcome = adapter_for("ms_graph_mail", conn_row.config, secret).send(
        subject=note.title, body=note.body, recipients=[{"kind": "TO", "address": member.email, "name": ""}]
    )
    state = {"ACCEPTED": "ACCEPTED", "UNKNOWN": "UNKNOWN"}.get(outcome.kind, "FAILED")
    return _note_email(ctx, note.id, state, None if state == "ACCEPTED" else outcome.code)


def _note_email(ctx: JobContext, note_id: uuid.UUID, state: str, error: str | None) -> Outcome:
    with ctx.transaction() as conn:
        conn.execute(
            update(t.notification).where(t.notification.c.id == note_id).values(email_state=state, email_error=error)
        )
    return Outcome(result={"email": state})
