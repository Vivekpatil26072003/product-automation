"""email.send (object = email_message); FR20.

Commit intent, then send: the intent (email_message QUEUED) was committed by the request. Here:
1. Fenced transaction: QUEUED -> SENDING and an IN_FLIGHT attempt. If the email is already SENDING, an
   earlier worker died while talking to the provider: the outcome is UNKNOWN and nothing is sent again.
2. Checks without the provider: report still current, attachment bytes match the confirmed checksum,
   connection still CONNECTED. Any failure here is FAILED (no provider call).
3. One provider call, outside any transaction.
4. Record the outcome. Only a 429 (not accepted) or a request that never left goes back to QUEUED for a
   bounded retry. A timeout or 5xx after sending is UNKNOWN and waits for a Sender to reconcile it.
"""

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import func, insert, select, update

from app.audit import service as audit
from app.core.config import get_settings
from app.core.crypto import SecretsUnavailable, decrypt
from app.db import tables as t
from app.integrations import adapter_for
from app.integrations.graph_mail import SendOutcome
from app.jobs import ledger
from app.reports import service as reports
from app.storage.objects import get_storage
from workers.registry import Outcome, handler
from workers.runtime import JobContext, RetryableError

log = logging.getLogger("workers.mail")
SERVICE = audit.Actor("service", None)
em, ea, rs = t.email_message, t.email_attempt, t.email_recipient_status


def _finish(
    ctx: JobContext,
    email: Any,
    attempt_no: int,
    state: str,
    outcome: SendOutcome | None,
    *,
    code: str | None = None,
    message: str | None = None,
) -> None:
    """state: ACCEPTED | UNKNOWN | FAILED | QUEUED (retry)."""
    attempt_outcome = {"QUEUED": "RETRY"}.get(state, state)
    with ctx.transaction() as conn:
        conn.execute(
            update(ea)
            .where(ea.c.email_id == email.id, ea.c.attempt_no == attempt_no)
            .values(
                outcome=attempt_outcome,
                finished_at=func.now(),
                http_status=outcome.http_status if outcome else None,
                provider_request_id=outcome.request_id if outcome else None,
                error_code=code,
                error_message=message,
            )
        )
        values: dict[str, Any] = {"state": state, "error_code": code, "error_message": message}
        if outcome and outcome.request_id:
            values["provider_request_id"] = outcome.request_id
        if state == "ACCEPTED":
            values |= {"accepted_at": func.now(), "finished_at": func.now()}
        elif state in ("UNKNOWN", "FAILED"):
            values["finished_at"] = func.now()
        conn.execute(update(em).where(em.c.id == email.id).values(**values))
        if state != "QUEUED":
            recipient_state = {"ACCEPTED": "ACCEPTED", "UNKNOWN": "UNKNOWN", "FAILED": "FAILED"}[state]
            conn.execute(
                update(rs)
                .where(rs.c.email_id == email.id)
                .values(state=recipient_state, observation_source="provider_response", observed_at=func.now())
            )
        audit.record(
            conn,
            tenant_id=email.tenant_id,
            actor=SERVICE,
            action=f"EMAIL_{attempt_outcome}",
            object_type="email_message",
            object_id=email.id,
            after={"attempt": attempt_no, "code": code, "http_status": outcome.http_status if outcome else None},
        )


PAUSE_RECHECK_SECONDS = 300


@handler("email.send")
def send_email(ctx: JobContext) -> Outcome:
    if get_settings().sends_paused:
        with ctx.transaction() as conn:
            ledger.ensure_job(conn, tenant_id=ctx.claim.tenant_id, kind="email.send", object_id=ctx.claim.object_id,
                              delay_seconds=PAUSE_RECHECK_SECONDS)  # fmt: skip
        return Outcome(result={"paused": True})
    with ctx.transaction() as conn:
        email = conn.execute(select(em).where(em.c.id == ctx.claim.object_id).with_for_update()).one()
        attempt_no = (
            conn.execute(select(func.max(ea.c.attempt_no)).where(ea.c.email_id == email.id)).scalar() or 0
        ) + 1
        if email.state == "SENDING":
            # A previous attempt may have reached the provider before the worker stopped: never resend blindly.
            conn.execute(
                update(ea)
                .where(ea.c.email_id == email.id, ea.c.outcome == "IN_FLIGHT")
                .values(
                    outcome="UNKNOWN",
                    finished_at=func.now(),
                    error_code="WORKER_INTERRUPTED",
                    error_message="The worker stopped during the provider call.",
                )
            )
            conn.execute(
                update(em)
                .where(em.c.id == email.id)
                .values(
                    state="UNKNOWN",
                    error_code="WORKER_INTERRUPTED",
                    finished_at=func.now(),
                    error_message="Sending was interrupted; the provider may have accepted it.",
                )
            )
            conn.execute(
                update(rs)
                .where(rs.c.email_id == email.id)
                .values(state="UNKNOWN", observation_source="worker_recovery", observed_at=func.now())
            )
            return Outcome(state="FAILED", error_code="UNKNOWN_OUTCOME", error_message="Reconcile before resending.")
        if email.state != "QUEUED":
            return Outcome(result={"skipped": email.state})
        conn.execute(update(em).where(em.c.id == email.id).values(state="SENDING"))
        conn.execute(
            insert(ea).values(id=uuid.uuid4(), tenant_id=email.tenant_id, email_id=email.id, attempt_no=attempt_no)
        )
        report = conn.execute(select(t.report).where(t.report.c.id == email.report_id)).one()
        connection = conn.execute(
            select(t.integration_connection).where(t.integration_connection.c.id == email.connection_id)
        ).one()
        current = report.state == "READY" and reports.is_current(conn, report)
        if not current:
            reports.mark_outdated(conn, report, "RECORDS_CHANGED")
        draft = conn.execute(select(t.email_draft).where(t.email_draft.c.id == email.draft_id)).one()

    def fail(code: str, message: str) -> Outcome:
        _finish(ctx, email, attempt_no, "FAILED", None, code=code, message=message)
        return Outcome(state="FAILED", error_code=code, error_message=message)

    if not current:
        return fail("REPORT_OUTDATED", "The report changed after confirmation. Generate a new version.")
    if connection.state != "CONNECTED":
        return fail("EMAIL_NOT_CONNECTED", "Email sending is not connected.")
    data = get_storage().get_bytes(report.file_key, 20_000_000)
    if hashlib.sha256(data).hexdigest() != email.attachment_sha256:
        return fail("ATTACHMENT_MISMATCH", "The stored PDF does not match the confirmed attachment.")
    try:
        secret = decrypt(connection.id, connection.secret_ciphertext, connection.secret_key_id)
    except (SecretsUnavailable, TypeError):
        return fail("SECRETS_UNAVAILABLE", "Stored email credentials cannot be read.")

    outcome = adapter_for("ms_graph_mail", connection.config, secret).send(
        subject=email.subject,
        body=draft.body,
        recipients=email.recipients,
        attachment_name=email.attachment_name,
        attachment=data,
    )
    if outcome.kind == "ACCEPTED":
        _finish(ctx, email, attempt_no, "ACCEPTED", outcome)
        return Outcome(result={"state": "ACCEPTED", "provider_id": outcome.request_id})
    if outcome.kind in ("RETRY", "NOT_SENT"):
        if ctx.claim.attempt_no >= 5:
            return fail(outcome.code or "PROVIDER_BUSY", "The provider did not accept the message after 5 attempts.")
        _finish(ctx, email, attempt_no, "QUEUED", outcome, code=outcome.code, message="Not accepted yet; retrying.")
        raise RetryableError(outcome.code or "PROVIDER_BUSY", "Email provider busy; retrying.", outcome.retry_after)
    if outcome.kind == "UNKNOWN":
        _finish(
            ctx,
            email,
            attempt_no,
            "UNKNOWN",
            outcome,
            code=outcome.code,
            message="No answer after sending; the provider may have accepted it.",
        )
        return Outcome(state="FAILED", error_code="UNKNOWN_OUTCOME", error_message="Reconcile before resending.")
    if outcome.kind == "AUTH":
        with ctx.transaction() as conn:
            conn.execute(
                update(t.integration_connection)
                .where(t.integration_connection.c.id == connection.id)
                .values(
                    state="RECONNECT_REQUIRED",
                    last_error_code=outcome.code,
                    last_error_message="Microsoft 365 refused the credentials while sending.",
                )
            )
        return fail("AUTH_FAILED", "Microsoft 365 refused the credentials. An administrator must reconnect.")
    return fail(outcome.code or "PROVIDER_REJECTED", f"Microsoft 365 rejected the message ({outcome.http_status}).")
