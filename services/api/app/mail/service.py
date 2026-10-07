"""Email drafts, confirmed send and reconciliation (FR19, FR20; API operations 29-35).

- A draft belongs to one READY report. It is plain text (no HTML is sent), with To/Cc/Bcc, subject and body
  validated on every save. Every save bumps the version and recomputes content_hash over everything the
  recipient will receive, including the attachment's checksum.
- Send requires the draft version and the content_hash the Sender confirmed. Any edit after preview changes
  the hash, so an old confirmation is refused. The report must still be READY and current.
- Commit intent, then send: the request only writes email_message (one per draft, UNIQUE intent_key) and
  queues email.send. The provider is called outside any transaction. Double clicks and replays get the same
  email ID.
- Provider acceptance is ACCEPTED, never "delivered". A timeout after the request may have reached the
  provider is UNKNOWN: no automatic retry; a Sender reconciles it with evidence from the mailbox or logs.
"""

import hashlib
import json
import re
import uuid
from typing import Any

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import Connection, func, insert, select, update

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.company import DEFAULTS
from app.core.config import get_settings
from app.core.errors import ApiError, Issue, conflict, forbidden, not_found, precondition_failed, validation_failed
from app.db import tables as t
from app.domain.enums import Role
from app.integrations import service as integ
from app.jobs import ledger
from app.reports import service as reports
from app.reports.facts import email_summary, fmt_date, unverified_quantities
from app.reports.pdf import attachment_name

SEND_KIND = "email.send"
MAX_RECIPIENTS, MAX_SUBJECT, MAX_BODY = 50, 200, 20_000
# Release-1 cap is 10 MiB; Microsoft Graph sendMail accepts at most ~3 MB of inline attachment per request.
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024
HTML_TAG = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>")
d, em = t.email_draft, t.email_message
KINDS = ("TO", "CC", "BCC")


def channel() -> str:
    """ "graph" (server sends through Microsoft 365) or "emailjs" (the Sender's browser sends through EmailJS)."""
    return get_settings().email_provider


def _require_sender(principal: Principal) -> None:
    if not principal.has_any(Role.SENDER):
        raise forbidden("Only Senders can prepare and send email.")


# --- validation -----------------------------------------------------------------------------------


def normalize_recipients(to: list[Any], cc: list[Any], bcc: list[Any]) -> tuple[list[dict[str, str]], list[Issue]]:
    issues: list[Issue] = []
    out: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    for kind, values in zip(KINDS, (to, cc, bcc), strict=True):
        field = kind.lower()
        for i, raw in enumerate(values):
            address = raw.get("address", "") if isinstance(raw, dict) else str(raw)
            name = (raw.get("name") or "") if isinstance(raw, dict) else ""
            address, name = address.strip(), name.strip()
            if any(ch in address + name for ch in "\r\n") or len(name) > 120:
                issues.append(Issue("INVALID_RECIPIENT", "Line breaks are not allowed in recipients.", f"{field}[{i}]"))
                continue
            try:
                norm = validate_email(address, check_deliverability=False).normalized
            except EmailNotValidError:
                issues.append(
                    Issue("INVALID_RECIPIENT", f"{address or '(empty)'} is not a valid email address.", f"{field}[{i}]")
                )
                continue
            key = norm.lower()
            if key in seen:
                issues.append(
                    Issue("DUPLICATE_RECIPIENT", f"{norm} is already in {seen[key].lower()}.", f"{field}[{i}]")
                )
                continue
            seen[key] = kind
            out.append({"kind": kind, "address": norm, "name": name})
    if len(out) > MAX_RECIPIENTS:
        issues.append(
            Issue("TOO_MANY_RECIPIENTS", f"Use at most {MAX_RECIPIENTS} recipients in To, Cc and Bcc combined.", "to")
        )
    return out, issues


def validate_content(subject: str, body: str) -> list[Issue]:
    issues = []
    if "\r" in subject or "\n" in subject:
        issues.append(Issue("INVALID_SUBJECT", "The subject cannot contain line breaks.", "subject"))
    if not 1 <= len(subject.strip()) <= MAX_SUBJECT:
        issues.append(Issue("INVALID_SUBJECT", f"Enter a subject of 1 to {MAX_SUBJECT} characters.", "subject"))
    if len(body) > MAX_BODY:
        issues.append(Issue("BODY_TOO_LONG", f"The message can have at most {MAX_BODY:,} characters.", "body"))
    if HTML_TAG.search(body):
        issues.append(Issue("HTML_NOT_ALLOWED", "Remove HTML tags; the email is sent as plain text.", "body"))
    return issues


def content_hash(report: Any, recipients: list[dict[str, str]], subject: str, body: str) -> str:
    """Everything the recipients will receive, including the exact attachment bytes (by checksum)."""
    doc = {
        "report_id": str(report.id),
        "report_version": report.version,
        "attachment_sha256": report.sha256,
        "recipients": recipients,
        "subject": subject,
        "body": body,
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# --- drafts -----------------------------------------------------------------------------------------


def _ready_current_report(conn: Connection, principal: Principal, report_id: uuid.UUID, lock: bool = False) -> Any:
    report = reports.load_report(conn, principal, report_id, lock=lock)
    if report.state != "READY":
        raise conflict("REPORT_NOT_READY", "The report is not ready yet.")
    if report.file_purged_at is not None:
        raise conflict("REPORT_FILE_PURGED", "The PDF was deleted under the retention policy. Generate a new version.")
    if not reports.is_current(conn, report):
        reports.mark_outdated(conn, report, "RECORDS_CHANGED")
        raise conflict("REPORT_OUTDATED", "Records in this report changed. Generate a new version before sending.")
    return report


def default_body(report: Any, correction_of: Any | None = None) -> str:
    lines = ["Dear Team,", ""]
    if correction_of is not None:
        lines += [
            f"This corrects the report sent on {fmt_date(correction_of.created_at.date())} "
            f"({reports.code_for(correction_of.report_series)} version {correction_of.report_version}). "
            "Please use the attached version instead.",
            "",
        ]
    lines += [
        email_summary(report.facts_json),
        "",
        "Please find the detailed report attached.",
        "",
        "Regards,",
        "Production Team",
    ]
    return "\n".join(lines)


def default_subject(report: Any) -> str:
    return f"{report.title} – {report.facts_json['period']['label']}"[:MAX_SUBJECT]


def _insert_draft(
    conn: Connection,
    principal: Principal,
    report: Any,
    *,
    recipients: list[dict[str, str]],
    subject: str,
    body: str,
    **extra: Any,
) -> Any:
    draft_id = uuid.uuid4()
    conn.execute(
        insert(d).values(
            id=draft_id,
            tenant_id=principal.tenant_id,
            report_id=report.id,
            subject=subject,
            body=body,
            recipients=recipients,
            content_hash=content_hash(report, recipients, subject, body),
            created_by=principal.membership_id,
            **extra,
        )
    )
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="EMAIL_DRAFT_CREATED",
        object_type="email_draft",
        object_id=draft_id,
        after={
            "report_id": str(report.id),
            "report_version": report.version,
            **{k: str(v) for k, v in extra.items() if v is not None},
        },
    )
    return conn.execute(select(d).where(d.c.id == draft_id)).one()


def create_draft(
    conn: Connection, principal: Principal, report_id: uuid.UUID, correction_of_email_id: uuid.UUID | None = None
) -> Any:
    _require_sender(principal)
    report = _ready_current_report(conn, principal, report_id)
    correction = None
    if correction_of_email_id is not None:
        correction = _email_with_report(conn, principal, correction_of_email_id)
        if correction.state != "ACCEPTED":
            raise conflict("NOT_ACCEPTED", "Only an email the provider accepted can be corrected.")
        if correction.report_series != report.series_id or correction.report_version >= report.version:
            raise conflict("NOT_A_CORRECTION", "A correction must use a newer version of the same report.")
    body = default_body(report, correction)
    return _insert_draft(
        conn,
        principal,
        report,
        recipients=[],
        subject=default_subject(report),
        body=body,
        correction_of_email_id=correction_of_email_id,
    )


def load_draft(conn: Connection, principal: Principal, draft_id: uuid.UUID, lock: bool = False) -> tuple[Any, Any]:
    _require_sender(principal)
    q = select(d).where(d.c.id == draft_id)
    draft = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if draft is None:
        raise not_found()
    report = reports.load_report(conn, principal, draft.report_id)  # scope follows the report
    return draft, report


def update_draft(
    conn: Connection, principal: Principal, draft_id: uuid.UUID, expected_version: int, changes: dict[str, Any]
) -> Any:
    draft, report = load_draft(conn, principal, draft_id, lock=True)
    if draft.state != "DRAFT":
        raise conflict("DRAFT_LOCKED", "This draft was confirmed for sending and can no longer change.")
    if draft.version != expected_version:
        raise precondition_failed(draft.version)
    current = {k: [r for r in draft.recipients if r["kind"] == k.upper()] for k in ("to", "cc", "bcc")}
    lists = {k: changes.get(k, current[k]) for k in ("to", "cc", "bcc")}
    recipients, issues = normalize_recipients(lists["to"], lists["cc"], lists["bcc"])
    subject, body = changes.get("subject", draft.subject), changes.get("body", draft.body)
    issues += validate_content(subject, body)
    if issues:
        raise validation_failed(issues)  # nothing saved: a failed save blocks confirmation
    subject = subject.strip()
    conn.execute(
        update(d)
        .where(d.c.id == draft.id)
        .values(
            recipients=recipients,
            subject=subject,
            body=body,
            version=d.c.version + 1,
            content_hash=content_hash(report, recipients, subject, body),
        )
    )
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="EMAIL_DRAFT_UPDATED",
        object_type="email_draft",
        object_id=draft.id,
        object_revision=draft.version + 1,
        after={
            "recipient_count": len(recipients),
            "subject_changed": subject != draft.subject,
            "body_changed": body != draft.body,
        },
    )
    return conn.execute(select(d).where(d.c.id == draft.id)).one()


def internal_domains(conn: Connection, tenant_id: uuid.UUID) -> set[str]:
    """Company domains (settings.internal_email_domains plus the sender mailbox's domain); others are external."""
    settings = conn.execute(select(t.tenant.c.settings).where(t.tenant.c.id == tenant_id)).scalar_one() or {}
    domains = {x.lower() for x in settings.get("internal_email_domains", DEFAULTS["internal_email_domains"])}
    mail = integ.live(conn, "ms_graph_mail")
    if mail is not None:
        domains.add(mail.config["sender_mailbox"].rsplit("@", 1)[1].lower())
    return domains


def draft_view(conn: Connection, draft: Any, report: Any) -> dict[str, Any]:
    current = report.state == "READY" and reports.is_current(conn, report)
    domains = internal_domains(conn, draft.tenant_id)
    external = sorted({r["address"].rsplit("@", 1)[1].lower() for r in draft.recipients} - domains)
    mail = integ.live(conn, "ms_graph_mail")
    blocking: list[dict[str, str]] = []

    def block(code: str, message: str) -> None:
        blocking.append({"code": code, "message": message})

    if draft.state != "DRAFT":
        block("DRAFT_LOCKED", "This draft was already confirmed for sending.")
    if report.state != "READY":
        block("REPORT_NOT_READY", "The report is not ready.")
    elif not current:
        block("REPORT_OUTDATED", "Records in this report changed. Generate a new version and a new draft.")
    if not any(r["kind"] == "TO" for r in draft.recipients):
        block("NO_TO", "Add at least one To recipient.")
    if channel() == "graph":
        if mail is None or mail.state != "CONNECTED":
            block("EMAIL_NOT_CONNECTED", "Email sending is not connected. An administrator must connect Microsoft 365.")
        if (report.bytes or 0) > MAX_ATTACHMENT_BYTES:
            block("ATTACHMENT_TOO_LARGE", "The PDF is larger than the provider accepts for one message.")
    if unverified := unverified_quantities(draft.body, report.facts_json):
        block(
            "UNVERIFIED_NUMBERS",
            "These figures do not match the report: " + ", ".join(unverified) + ". Correct or remove them.",
        )
    return {
        "id": str(draft.id),
        "report_id": str(report.id),
        "state": draft.state,
        "version": draft.version,
        "content_hash": draft.content_hash,
        "subject": draft.subject,
        "body": draft.body,
        "to": [r for r in draft.recipients if r["kind"] == "TO"],
        "cc": [r for r in draft.recipients if r["kind"] == "CC"],
        "bcc": [r for r in draft.recipients if r["kind"] == "BCC"],
        "report": {
            "id": str(report.id),
            "code": reports.code_for(report.series_id),
            "version": report.version,
            "title": report.title,
            "state": report.state,
            "current": current,
        },
        "attachment": {
            "name": attachment_name(
                {
                    "date_from": report.date_from.isoformat(),
                    "code": reports.code_for(report.series_id),
                    "version": report.version,
                }
            ),
            "bytes": report.bytes,
            "sha256": report.sha256,
        },
        "sender_mailbox": mail.config["sender_mailbox"] if mail is not None and channel() == "graph" else None,
        "channel": channel(),
        "external_domains": external,
        "sendable": not blocking,
        "blocking": blocking,
        "resend_of_email_id": str(draft.resend_of_email_id) if draft.resend_of_email_id else None,
        "correction_of_email_id": str(draft.correction_of_email_id) if draft.correction_of_email_id else None,
        "email_id": _email_for_draft(conn, draft.id),
        "updated_at": draft.updated_at.isoformat(),
    }


def _email_for_draft(conn: Connection, draft_id: uuid.UUID) -> str | None:
    x = conn.execute(select(em.c.id).where(em.c.draft_id == draft_id)).scalar_one_or_none()
    return str(x) if x else None


# --- send ----------------------------------------------------------------------------------------------


def send(
    conn: Connection, principal: Principal, draft_id: uuid.UUID, *, version: int, confirmed_hash: str, if_match: int
) -> tuple[int, dict[str, Any]]:
    draft, report = load_draft(conn, principal, draft_id, lock=True)
    existing = conn.execute(select(em).where(em.c.draft_id == draft.id)).one_or_none()
    if existing is not None:  # double click with another key, or a replay: same email, no second send
        if existing.content_hash != confirmed_hash:
            raise conflict("DRAFT_LOCKED", "This draft was already sent with different content.")
        return 202, {"data": {"email_id": str(existing.id), "state": existing.state, "channel": existing.channel}}
    if draft.version != if_match:
        raise precondition_failed(draft.version)
    if draft.version != version or draft.content_hash != confirmed_hash:
        raise conflict("STALE_CONFIRMATION", "The draft changed after you previewed it. Review the confirmation again.")
    report = _ready_current_report(conn, principal, draft.report_id, lock=True)
    view = draft_view(conn, draft, report)
    if view["blocking"]:
        b = view["blocking"][0]
        raise ApiError(409 if b["code"] != "NO_TO" else 422, b["code"], b["message"])
    unknown = conn.execute(
        select(em.c.id)
        .join(t.report, t.report.c.id == em.c.report_id)
        .where(t.report.c.series_id == report.series_id, em.c.state == "UNKNOWN")
        .limit(1)
    ).first()
    if unknown:
        raise conflict(
            "RECONCILE_REQUIRED",
            "An earlier email for this report has an unknown outcome. Reconcile it before sending again.",
        )
    via = channel()
    mail = integ.live(conn, "ms_graph_mail") if via == "graph" else None
    email_id = uuid.uuid4()
    conn.execute(
        insert(em).values(
            id=email_id,
            tenant_id=principal.tenant_id,
            draft_id=draft.id,
            draft_version=draft.version,
            content_hash=draft.content_hash,
            report_id=report.id,
            intent_key=f"draft:{draft.id}:v{draft.version}:{draft.content_hash[:16]}",
            channel=via,
            connection_id=mail.id if mail else None,
            sender_mailbox=mail.config["sender_mailbox"] if mail else None,
            subject=draft.subject,
            recipients=draft.recipients,
            attachment_name=view["attachment"]["name"] if via == "graph" else None,
            attachment_sha256=report.sha256,  # identity of the report snapshot the email describes
            attachment_bytes=report.bytes if via == "graph" else None,
            created_by=principal.membership_id,
        )
    )
    conn.execute(
        insert(t.email_recipient_status),
        [
            {
                "id": uuid.uuid4(),
                "tenant_id": principal.tenant_id,
                "email_id": email_id,
                "kind": r["kind"],
                "address": r["address"],
            }
            for r in draft.recipients
        ],
    )
    conn.execute(update(d).where(d.c.id == draft.id).values(state="QUEUED"))
    if via == "graph":  # EmailJS: the browser claims and sends it (app.mail.emailjs)
        ledger.create_job(
            conn, tenant_id=principal.tenant_id, kind=SEND_KIND, object_id=email_id, created_by=principal.membership_id
        )
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="EMAIL_SEND_CONFIRMED",
        object_type="email_message",
        object_id=email_id,
        after={
            "draft_id": str(draft.id),
            "draft_version": draft.version,
            "content_hash": draft.content_hash,
            "report_id": str(report.id),
            "report_version": report.version,
            "recipients": len(draft.recipients),
            "external_domains": view["external_domains"],
            "channel": via,
        },
    )
    return 202, {"data": {"email_id": str(email_id), "state": "QUEUED", "channel": via}}


# --- emails --------------------------------------------------------------------------------------------


def _email_with_report(conn: Connection, principal: Principal, email_id: uuid.UUID, lock: bool = False) -> Any:
    _require_sender(principal)
    q = (
        select(
            em,
            t.report.c.series_id.label("report_series"),
            t.report.c.version.label("report_version"),
            t.report.c.department_ids.label("report_departments"),
        )
        .join(t.report, t.report.c.id == em.c.report_id)
        .where(em.c.id == email_id)
    )
    row = conn.execute(q.with_for_update(of=em) if lock else q).one_or_none()
    if row is None or not principal.can_access_all(set(row.report_departments)):
        raise not_found()
    return row


STATE_TEXT = {
    "QUEUED": "Queued for sending",
    "SENDING": "Sending",
    "ACCEPTED": "Accepted by the email provider. Delivery to each mailbox is not confirmed.",
    "UNKNOWN": "Outcome unknown: the provider may or may not have accepted it. Check Sent Items before any resend.",
    "FAILED": "Not sent",
}


def email_view(conn: Connection, row: Any) -> dict[str, Any]:
    recipients = conn.execute(
        select(t.email_recipient_status)
        .where(t.email_recipient_status.c.email_id == row.id)
        .order_by(t.email_recipient_status.c.kind, t.email_recipient_status.c.address)
    ).all()
    attempts = conn.execute(
        select(t.email_attempt).where(t.email_attempt.c.email_id == row.id).order_by(t.email_attempt.c.attempt_no)
    ).all()
    return {
        "id": str(row.id),
        "state": row.state,
        "state_text": STATE_TEXT[row.state],
        "draft_id": str(row.draft_id),
        "report_id": str(row.report_id),
        "report_code": reports.code_for(row.report_series),
        "report_version": row.report_version,
        "subject": row.subject,
        "sender_mailbox": row.sender_mailbox,
        "channel": row.channel,
        "provider_id": row.provider_request_id,
        "attachment": {"name": row.attachment_name, "bytes": row.attachment_bytes, "sha256": row.attachment_sha256},
        "recipients": [
            {
                "kind": r.kind,
                "address": r.address,
                "state": r.state,
                "observation_source": r.observation_source,
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
            }
            for r in recipients
        ],
        "attempts": [
            {
                "attempt_no": a.attempt_no,
                "outcome": a.outcome,
                "http_status": a.http_status,
                "error_code": a.error_code,
                "error_message": a.error_message,
                "started_at": a.started_at.isoformat(),
                "finished_at": a.finished_at.isoformat() if a.finished_at else None,
            }
            for a in attempts
        ],
        "error": {"code": row.error_code, "message": row.error_message} if row.error_code else None,
        "created_at": row.created_at.isoformat(),
        "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
        "reconciliation": None
        if row.reconciled_at is None
        else {
            "outcome": row.reconcile_outcome,
            "evidence_ref": row.reconcile_evidence,
            "reason": row.reconcile_reason,
            "at": row.reconciled_at.isoformat(),
        },
    }


def get_email(conn: Connection, principal: Principal, email_id: uuid.UUID) -> dict[str, Any]:
    return email_view(conn, _email_with_report(conn, principal, email_id))


def reconcile(
    conn: Connection, principal: Principal, email_id: uuid.UUID, outcome: str, evidence_ref: str, reason: str
) -> dict[str, Any]:
    row = _email_with_report(conn, principal, email_id, lock=True)
    if row.state != "UNKNOWN":
        raise conflict("NOT_UNKNOWN", "Only an email with an unknown outcome can be reconciled.")
    new_state = "ACCEPTED" if outcome == "ACCEPTED" else "FAILED"
    values: dict[str, Any] = {
        "state": new_state,
        "reconciled_by": principal.membership_id,
        "reconciled_at": func.now(),
        "reconcile_outcome": outcome,
        "reconcile_evidence": evidence_ref,
        "reconcile_reason": reason,
        "finished_at": func.now(),
    }
    if new_state == "ACCEPTED":
        values["accepted_at"] = func.now()
    else:
        values |= {"error_code": "NOT_ACCEPTED", "error_message": "Reconciled: the provider did not accept it."}
    conn.execute(update(em).where(em.c.id == row.id).values(**values))
    rs = t.email_recipient_status
    conn.execute(
        update(rs)
        .where(rs.c.email_id == row.id)
        .values(
            state="ACCEPTED" if new_state == "ACCEPTED" else "FAILED",
            observation_source="reconciliation",
            observed_at=func.now(),
        )
    )
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="EMAIL_RECONCILED",
        object_type="email_message",
        object_id=row.id,
        reason=reason,
        before={"state": "UNKNOWN"},
        after={"state": new_state, "evidence_ref": evidence_ref},
    )
    return email_view(conn, _email_with_report(conn, principal, row.id))


def resend_draft(conn: Connection, principal: Principal, email_id: uuid.UUID, reason: str) -> Any:
    row = _email_with_report(conn, principal, email_id)
    if row.state == "UNKNOWN":
        raise conflict("RECONCILE_REQUIRED", "Reconcile the unknown outcome before preparing a resend.")
    if row.state in ("QUEUED", "SENDING"):
        raise conflict("EMAIL_IN_PROGRESS", "This email is still being sent.")
    try:
        report = _ready_current_report(conn, principal, row.report_id)
    except ApiError as exc:
        if exc.code == "REPORT_OUTDATED":
            raise conflict(
                "REGENERATE_REQUIRED",
                "The report is outdated. Generate a new version, then prepare a correction email from it.",
            ) from exc
        raise
    draft = conn.execute(select(d).where(d.c.id == row.draft_id)).one()
    return _insert_draft(
        conn,
        principal,
        report,
        recipients=draft.recipients,
        subject=draft.subject,
        body=draft.body,
        resend_of_email_id=row.id,
        resend_reason=reason,
    )


def list_emails(conn: Connection, principal: Principal, report_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    _require_sender(principal)
    q = (
        select(
            em.c.id,
            em.c.state,
            em.c.subject,
            em.c.created_at,
            em.c.report_id,
            t.report.c.series_id,
            t.report.c.version,
            t.report.c.department_ids,
            func.jsonb_array_length(em.c.recipients).label("n"),
        )
        .join(t.report, t.report.c.id == em.c.report_id)
        .order_by(em.c.created_at.desc())
        .limit(100)
    )
    if report_id is not None:
        q = q.where(em.c.report_id == report_id)
    return [
        {
            "id": str(x.id),
            "state": x.state,
            "state_text": STATE_TEXT[x.state],
            "subject": x.subject,
            "report_id": str(x.report_id),
            "report_code": reports.code_for(x.series_id),
            "report_version": x.version,
            "recipient_count": x.n,
            "created_at": x.created_at.isoformat(),
        }
        for x in conn.execute(q).all()
        if principal.can_access_all(set(x.department_ids))
    ]
