"""Owner report and order email jobs (EmailJS REST API from the worker; no browser needed).

batch_report.render (object = batch_report): read the saved orders / records, render the PDF, store it, READY;
                     then, when the report was requested for the owner, queue its email.
batch_report.email  (object = report_delivery) and order.email (object = order_email):
    QUEUED -> SENDING (committed before the request) -> ACCEPTED / FAILED / UNKNOWN.
    A job that finds its email already SENDING (the worker stopped mid-request) marks it UNKNOWN instead of
    sending again. "Could not connect" and 429 leave it QUEUED and retry with backoff; after the last attempt it
    is FAILED and can be retried by a person. Nothing is ever marked sent unless EmailJS answered 200.
"""

import hashlib
import logging
from typing import Any

from sqlalchemy import func, select, update

from app.audit import service as audit
from app.core.crypto import SecretsUnavailable
from app.db import tables as t
from app.mail import emailjs_api
from app.orders import service as orders
from app.owner_reports import pdf as report_pdf
from app.owner_reports import service as owner_reports
from app.owner_reports import settings as owner_settings
from app.pick_registers import service as registers
from app.shift_reports import service as sheets
from app.storage.objects import get_storage, object_key_for
from workers.registry import Outcome, handler
from workers.runtime import JobContext, RetryableError

log = logging.getLogger("workers.owner_reports")
PDF = "application/pdf"
MAX_ATTEMPTS = 5
SERVICE = audit.Actor("service", None)


@handler(owner_reports.RENDER_KIND)
def render_report(ctx: JobContext) -> Outcome:
    br = t.batch_report
    with ctx.transaction() as conn:
        row = conn.execute(select(br).where(br.c.id == ctx.claim.object_id)).one()
        if row.state == "READY":
            return Outcome(result={"skipped": "READY"})
        conn.execute(
            update(br).where(br.c.id == row.id).values(state="GENERATING", error_code=None, error_message=None)
        )
        c = owner_reports.content(conn, row)
    try:
        data = report_pdf.render(c)
    except Exception as exc:  # noqa: BLE001 - any renderer fault: FAILED, nothing to send
        log.exception("batch report render failed report=%s", row.id)
        with ctx.transaction() as conn:
            conn.execute(
                update(br)
                .where(br.c.id == row.id)
                .values(
                    state="FAILED",
                    error_code="RENDER_FAILED",
                    error_message="The PDF could not be created. Create the report again from the batch screen.",
                )
            )
            owner_reports.notify(
                conn,
                row,
                "Diary report could not be created",
                f"The PDF for batch {c['batch_ref']} failed. Create it again from the batch screen.",
                f"{row.id}:render",
            )
        return Outcome(state="FAILED", error_code="RENDER_FAILED", error_message=type(exc).__name__)
    key = object_key_for("reports", row.tenant_id, row.id, ".pdf")
    get_storage().put_bytes(key, data, PDF)
    with ctx.transaction() as conn:
        conn.execute(
            update(br)
            .where(br.c.id == row.id)
            .values(
                state="READY",
                file_key=key,
                file_name=report_pdf.file_name(c["batch_ref"], row.version),
                sha256=hashlib.sha256(data).hexdigest(),
                bytes=len(data),
                ready_at=func.now(),
                summary=owner_reports.summary_json(c),
                revision_ids=c["revision_ids"],
            )
        )
        ready = conn.execute(select(br).where(br.c.id == row.id)).one()
        queued = None
        if ready.email_owner:
            cfg = owner_settings.load(conn, ready.tenant_id)
            if cfg is not None and cfg.owner_email:
                try:
                    queued = owner_reports.queue_delivery(conn, ready, cfg.owner_email, ready.trigger, ready.created_by)
                except Exception:  # noqa: BLE001 - already being sent: nothing to add
                    log.info("report %s already has a delivery in flight", ready.id)
    return Outcome(result={"bytes": len(data), "email_queued": bool(queued)})


def _audit(conn, table, row: Any, action: str, after: dict[str, Any]) -> None:
    """Audit every send step of an email (status, HTTP code, reason; never keys or the PDF itself)."""
    order_email = table.name == "order_email"
    types = {"order_email": "customer_order", "sheet_email": "shift_report", "register_email": "pick_register"}
    object_type = types.get(table.name, "batch_report")
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=SERVICE,
        action=action,
        object_type=object_type,
        object_id=row.order_id if order_email else getattr(row, "register_id", None) or row.report_id,
        object_revision=row.revision_number if order_email else None,
        after={"email_id": str(row.id), **after},
    )


def _finish(conn, table, row: Any, outcome: str, status: int | None, code: str | None, message: str | None) -> None:
    conn.execute(
        update(table)
        .where(table.c.id == row.id)
        .values(state=outcome, http_status=status, error_code=code, error_message=message, finished_at=func.now())
    )
    _audit(conn, table, row, f"EMAIL_SEND_{outcome}", {"http_status": status, "code": code, "reason": message})


def _send(ctx: JobContext, table, row: Any, load_params) -> Outcome:
    """Shared send step for report deliveries and order emails."""
    if row.state in ("ACCEPTED", "FAILED", "UNKNOWN"):
        return Outcome(result={"skipped": row.state})
    with ctx.transaction() as conn:
        if row.state == "SENDING":  # a previous attempt stopped mid-request: it may have been sent
            _finish(
                conn,
                table,
                row,
                "UNKNOWN",
                None,
                "INTERRUPTED",
                "Sending was interrupted; it may have been sent. Check EmailJS -> Email History.",
            )
            return Outcome(state="FAILED", error_code="INTERRUPTED")
        try:
            config = owner_settings.emailjs_config(conn, row.tenant_id)
        except SecretsUnavailable:
            config = None
        if config is None:
            _finish(
                conn,
                table,
                row,
                "FAILED",
                None,
                "EMAIL_NOT_CONFIGURED",
                "Email is not set up: set EMAILJS_SERVICE_ID, EMAILJS_TEMPLATE_ID, EMAILJS_PUBLIC_KEY and "
                "EMAILJS_PRIVATE_KEY in the server .env (or complete Settings -> Owner report & email), then retry.",
            )
            return Outcome(state="FAILED", error_code="EMAIL_NOT_CONFIGURED")
        try:
            params, data = load_params(conn)
        except Exception as exc:  # noqa: BLE001 - the attachment cannot be produced: do not send
            log.exception("email params failed id=%s", row.id)
            _finish(conn, table, row, "FAILED", None, "ATTACHMENT_UNAVAILABLE", f"The PDF is not available ({exc}).")
            return Outcome(state="FAILED", error_code="ATTACHMENT_UNAVAILABLE")
        size = emailjs_api.request_bytes(config, params)
        if size > config.max_request_kb * 1000:
            _finish(
                conn,
                table,
                row,
                "FAILED",
                None,
                "ATTACHMENT_TOO_LARGE",
                f"The email with its PDF is {size // 1000} KB; the EmailJS plan allows {config.max_request_kb} KB "
                "per request. Raise the limit in settings if your plan allows more.",
            )
            return Outcome(state="FAILED", error_code="ATTACHMENT_TOO_LARGE")
        conn.execute(update(table).where(table.c.id == row.id).values(state="SENDING"))
        _audit(conn, table, row, "EMAIL_SEND_STARTED", {"attempt": ctx.claim.attempt_no, "request_bytes": size})
    result = emailjs_api.send(config, params)
    with ctx.transaction() as conn:
        if result.outcome == "RETRY":
            if ctx.claim.attempt_no >= MAX_ATTEMPTS:
                _finish(
                    conn,
                    table,
                    row,
                    "FAILED",
                    result.status,
                    "EMAILJS_UNAVAILABLE",
                    f"{result.text} Not sent after {MAX_ATTEMPTS} attempts; retry later.",
                )
                return Outcome(state="FAILED", error_code="EMAILJS_UNAVAILABLE")
            conn.execute(update(table).where(table.c.id == row.id).values(state="QUEUED"))
        elif result.outcome == "ACCEPTED":
            _finish(conn, table, row, "ACCEPTED", result.status, None, None)
        elif result.outcome == "FAILED":
            _finish(conn, table, row, "FAILED", result.status, "EMAILJS_REJECTED", emailjs_api.explain(result))
        else:
            _finish(
                conn,
                table,
                row,
                "UNKNOWN",
                result.status,
                "EMAILJS_NO_ANSWER",
                f"{result.text} It may have been sent: check EmailJS -> Email History before sending again.",
            )
    if result.outcome == "RETRY":
        raise RetryableError("EMAILJS_UNAVAILABLE", result.text, 30.0)
    return Outcome(
        state="SUCCEEDED" if result.outcome == "ACCEPTED" else "FAILED",
        result={"outcome": result.outcome},
        error_code=None if result.outcome == "ACCEPTED" else f"EMAILJS_{result.outcome}",
    )


@handler(owner_reports.EMAIL_KIND)
def email_report(ctx: JobContext) -> Outcome:
    rd, br = t.report_delivery, t.batch_report
    with ctx.transaction() as conn:
        d = conn.execute(select(rd).where(rd.c.id == ctx.claim.object_id)).one()
        report = conn.execute(select(br).where(br.c.id == d.report_id)).one()

    def params(conn) -> tuple[dict[str, str], bytes]:
        data = get_storage().get_bytes(report.file_key, 20_000_000)
        if hashlib.sha256(data).hexdigest() != report.sha256:
            raise RuntimeError("stored PDF does not match its checksum")
        return owner_reports.email_params(conn, report, d, data), data

    out = _send(ctx, rd, d, params)
    with ctx.transaction() as conn:
        final = conn.execute(select(rd).where(rd.c.id == d.id)).one()
        batch_ref = f"B-{str(report.batch_id)[:8].upper()}"
        if final.state == "ACCEPTED":
            owner_reports.notify(
                conn,
                report,
                f"Diary report {batch_ref} emailed to the owner",
                f"Sent to {final.to_email} with {report.file_name}.",
                f"{d.id}:sent",
            )
        elif final.state in ("FAILED", "UNKNOWN"):
            owner_reports.notify(
                conn,
                report,
                f"Diary report {batch_ref} was not emailed"
                if final.state == "FAILED"
                else f"Diary report {batch_ref}: email result unknown",
                final.error_message or "",
                f"{d.id}:{final.state}",
            )
    return out


@handler(orders.EMAIL_KIND)
def email_order(ctx: JobContext) -> Outcome:
    oe = t.order_email
    with ctx.transaction() as conn:
        e = conn.execute(select(oe).where(oe.c.id == ctx.claim.object_id)).one()
    return _send(ctx, oe, e, lambda conn: orders.email_params(conn, e))


@handler(sheets.EMAIL_KIND)
def email_sheet(ctx: JobContext) -> Outcome:
    se = t.sheet_email
    with ctx.transaction() as conn:
        e = conn.execute(select(se).where(se.c.id == ctx.claim.object_id)).one()
    return _send(ctx, se, e, lambda conn: sheets.email_params(conn, e))


@handler(registers.EMAIL_KIND)
def email_register(ctx: JobContext) -> Outcome:
    rt = t.register_email
    with ctx.transaction() as conn:
        e = conn.execute(select(rt).where(rt.c.id == ctx.claim.object_id)).one()
    return _send(ctx, rt, e, lambda conn: registers.email_params(conn, e))
