"""Reports, email and history (FR16-FR21; API operations 24-36).

Reports: Reviewer or Sender, limited to reports whose every department the caller is granted.
Email: Sender only. History: object-scoped per tab; filters never broaden scope.
"""

import base64
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, cast, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from app.api.deps import etag, idempotency_key, if_match, require
from app.api.routes.insights import FilterParams, resolve
from app.audit import service as audit
from app.auth.principal import Principal
from app.core.config import get_settings
from app.core.errors import ApiError, conflict, forbidden
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import tenant_tx
from app.domain.enums import Role
from app.ingestion.service import job_view
from app.jobs import ledger
from app.mail import emailjs
from app.mail import service as mail
from app.reports import service as reports
from app.reports.pdf import attachment_name
from app.storage.objects import get_storage

router = APIRouter(tags=["reports"])
rp = t.report
DEFAULT_TITLE = "Daily Production Report"


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: FilterParams | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    include_detail: bool = True
    allow_empty: bool = False
    supersedes: uuid.UUID | None = None


def _scoped(principal: Principal):
    return rp.c.department_ids.op("<@")(cast(list(principal.department_ids), ARRAY(UUID(as_uuid=True))))


def _latest_job(conn, kind: str, object_id: uuid.UUID) -> Any:
    j = t.job
    return conn.execute(
        select(j).where(j.c.kind == kind, j.c.object_id == object_id).order_by(j.c.generation.desc()).limit(1)
    ).one_or_none()


@router.post("/reports", status_code=202, summary="Snapshot approved records and queue the PDF (FR16)")
def create_report(
    body: ReportIn,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        params, title = body.filter, body.title
        if body.supersedes is not None:
            old = reports.load_report(conn, principal, body.supersedes)
            if params is None:
                saved = {k: v for k, v in old.filter_json.items() if k in FilterParams.model_fields}
                params = FilterParams(**saved | {"include_archived": False})
            title = title or old.title
        if params is None:
            raise ApiError(422, "VALIDATION_FAILED", "Choose the report period.")
        if params.include_archived:
            raise ApiError(422, "VALIDATION_FAILED", "Reports include approved active records only.")
        f = resolve(principal, params)
        return 202, {
            "data": reports.create_report(
                conn,
                principal,
                f,
                title=title or DEFAULT_TITLE,
                include_detail=body.include_detail,
                allow_empty=body.allow_empty,
                supersedes=body.supersedes,
            )
        }

    # One repeatable-read snapshot: items, metrics and facts come from the same database state.
    with tenant_tx(principal.tenant_id, isolation="REPEATABLE READ") as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route="POST /reports",
            key=key,
            payload=body.model_dump(mode="json"),
            effect=effect,
        )
    return JSONResponse(out, status_code=status)


@router.get("/reports", summary="Reports the caller may open, newest first")
def list_reports(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    state: Literal["QUEUED", "GENERATING", "READY", "FAILED"] | None = Query(default=None),
    outdated: bool | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=200),
    size: int = Query(default=25),
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER)),
) -> dict:
    if size not in (25, 50, 100):
        raise ApiError(400, "BAD_PAGE_SIZE", "Page size must be 25, 50 or 100.")
    q = select(rp).where(_scoped(principal))
    if date_from:
        q = q.where(rp.c.date_to >= date_from)
    if date_to:
        q = q.where(rp.c.date_from <= date_to)
    if state:
        q = q.where(rp.c.state == state)
    if outdated is not None:
        q = q.where(rp.c.outdated_at.isnot(None) if outdated else rp.c.outdated_at.is_(None))
    if cursor:
        c_at, c_id = _decode(cursor)
        q = q.where(or_(rp.c.created_at < c_at, and_(rp.c.created_at == c_at, rp.c.id < c_id)))
    with tenant_tx(principal.tenant_id) as conn:
        rows = conn.execute(q.order_by(rp.c.created_at.desc(), rp.c.id.desc()).limit(size + 1)).all()
    page = rows[:size]
    return {"data": [_list_item(x) for x in page], "next_cursor": _encode(page[-1]) if len(rows) > size else None}


def _list_item(row: Any) -> dict[str, Any]:
    v = reports.report_view(row)
    return {
        k: v[k]
        for k in (
            "id",
            "code",
            "version",
            "title",
            "state",
            "outdated",
            "record_count",
            "is_empty",
            "created_at",
            "ready_at",
            "filter",
        )
    } | {"metrics": row.metrics_json["metrics"]}


def _encode(row: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps([row.created_at.isoformat(), str(row.id)]).encode()).decode()


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        a, b = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(a), uuid.UUID(b)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "BAD_CURSOR", "The page cursor is invalid. Reload the list.") from exc


@router.get("/reports/{report_id}", summary="Report with metrics, summary, versions and outdated status (FR18)")
def get_report(report_id: uuid.UUID, principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        row = reports.load_report(conn, principal, report_id)
        current = reports.is_current(conn, row) if row.state != "FAILED" else None
        creator = conn.execute(select(t.membership.c.display_name).where(t.membership.c.id == row.created_by)).scalar()
        data = reports.report_view(row, current=current, creator=creator)
        job = _latest_job(conn, reports.RENDER_KIND, row.id)
        data["job"] = job_view(job) if job else None
        data["versions"] = [
            {
                "id": str(x.id),
                "version": x.version,
                "state": x.state,
                "outdated": x.outdated_at is not None,
                "created_at": x.created_at.isoformat(),
            }
            for x in conn.execute(
                select(rp).where(rp.c.series_id == row.series_id, _scoped(principal)).order_by(rp.c.version.desc())
            ).all()
        ]
        data["attachment_name"] = attachment_name(
            {"date_from": row.date_from.isoformat(), "code": data["code"], "version": row.version}
        )
        data["emails"] = mail.list_emails(conn, principal, row.id) if principal.has_any(Role.SENDER) else []
        latest = conn.execute(
            select(t.export).where(t.export.c.report_id == row.id).order_by(t.export.c.created_at.desc()).limit(1)
        ).one_or_none()
        data["excel_export"] = None if latest is None else {"id": str(latest.id), "state": latest.state}
    return {"data": data}


@router.get("/reports/{report_id}/items", summary="The snapshot records exactly as included (not live rows)")
def report_items(
    report_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER)),
) -> dict:
    ri = t.report_item
    with tenant_tx(principal.tenant_id) as conn:
        row = reports.load_report(conn, principal, report_id)
        items = conn.execute(
            select(ri).where(ri.c.report_id == row.id).order_by(ri.c.position).offset(offset).limit(limit)
        ).all()
    return {
        "data": [
            {
                "record_id": str(x.record_id),
                "revision": x.revision_number,
                "revision_id": str(x.revision_id),
                **x.fields,
            }
            for x in items
        ],
        "total": row.record_count,
    }


@router.get("/reports/{report_id}/file", summary="Short-lived link to the immutable PDF (same bytes for preview)")
def report_file(report_id: uuid.UUID, principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        row = reports.load_report(conn, principal, report_id)
        if row.state != "READY":
            raise conflict("REPORT_NOT_READY", "The PDF is not ready yet.")
        if row.file_purged_at is not None:
            raise ApiError(410, "REPORT_FILE_PURGED", "The PDF was deleted under the retention policy; the report's "
                           "figures remain. Generate a new version if a file is needed.")  # fmt: skip
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action="REPORT_DOWNLOADED",
            object_type="report",
            object_id=row.id,
            object_revision=row.version,
        )
    name = attachment_name(
        {"date_from": row.date_from.isoformat(), "code": reports.code_for(row.series_id), "version": row.version}
    )
    storage = get_storage()
    expires = datetime.now(UTC) + timedelta(seconds=get_settings().signed_url_ttl_seconds)
    return {
        "data": {
            "url": storage.presign_get(row.file_key),
            "download_url": storage.presign_get(row.file_key, name),
            "sha256": row.sha256,
            "bytes": row.bytes,
            "name": name,
            "expires_at": expires.isoformat(timespec="seconds"),
        }
    }


@router.post("/reports/{report_id}/retry", status_code=202, summary="Render the same frozen snapshot again")
def retry_report(
    report_id: uuid.UUID,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        row = reports.load_report(conn, principal, report_id, lock=True)
        if row.state != "FAILED":
            raise conflict("NOT_FAILED", "Only a failed report can be retried. Use Regenerate for new data.")
        conn.execute(update(rp).where(rp.c.id == row.id).values(state="QUEUED", error_code=None, error_message=None))
        job_id = ledger.ensure_job(
            conn,
            tenant_id=row.tenant_id,
            kind=reports.RENDER_KIND,
            object_id=row.id,
            created_by=principal.membership_id,
        )
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action="REPORT_RETRIED",
            object_type="report",
            object_id=row.id,
            object_revision=row.version,
        )
        return 202, {"data": {"report_id": str(row.id), "job_id": str(job_id), "state": "QUEUED"}}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=f"POST /reports/{report_id}/retry",
            key=key,
            payload=None,
            effect=effect,
        )
    return JSONResponse(out, status_code=status)


# --- email drafts (U7) ------------------------------------------------------------------------------------


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: uuid.UUID
    correction_of_email_id: uuid.UUID | None = None


class Recipient(BaseModel):
    model_config = ConfigDict(extra="ignore")
    address: str = Field(max_length=320)
    name: str | None = Field(default=None, max_length=200)


class DraftPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: list[Recipient | str] | None = Field(default=None, max_length=100)
    cc: list[Recipient | str] | None = Field(default=None, max_length=100)
    bcc: list[Recipient | str] | None = Field(default=None, max_length=100)
    subject: str | None = Field(default=None, max_length=1000)
    body: str | None = Field(default=None, max_length=40_000)


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    confirmed_hash: str = Field(min_length=64, max_length=64)
    confirmation: bool


def _draft_response(status: int, out: dict) -> JSONResponse:
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})


@router.post("/email-drafts", status_code=201, summary="Draft an email for a READY, current report (FR19)")
def create_draft(
    body: DraftIn,
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        draft = mail.create_draft(conn, principal, body.report_id, body.correction_of_email_id)
        _, report = mail.load_draft(conn, principal, draft.id)
        return 201, {"data": mail.draft_view(conn, draft, report)}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route="POST /email-drafts",
            key=key,
            payload=body.model_dump(mode="json"),
            effect=effect,
        )
    return _draft_response(status, out)


@router.get("/email-drafts/{draft_id}", summary="Draft with confirmation details and what blocks sending")
def get_draft(draft_id: uuid.UUID, principal: Principal = Depends(require(Role.SENDER))) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        draft, report = mail.load_draft(conn, principal, draft_id)
        data = mail.draft_view(conn, draft, report)
    return _draft_response(200, {"data": data})


@router.patch("/email-drafts/{draft_id}", summary="Save recipients, subject or body; invalidates any confirmation")
def patch_draft(
    draft_id: uuid.UUID,
    body: DraftPatch,
    expected_version: int = Depends(if_match),
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    changes = body.model_dump(mode="json", exclude_unset=True)

    def effect() -> tuple[int, dict]:
        draft = mail.update_draft(conn, principal, draft_id, expected_version, changes)
        _, report = mail.load_draft(conn, principal, draft.id)
        return 200, {"data": mail.draft_view(conn, draft, report)}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=f"PATCH /email-drafts/{draft_id}",
            key=key,
            payload=[expected_version, changes],
            effect=effect,
        )
    return _draft_response(status, out)


@router.post("/email-drafts/{draft_id}/send", status_code=202, summary="Confirm and send once (FR20)")
def send_draft(
    draft_id: uuid.UUID,
    body: SendIn,
    expected_version: int = Depends(if_match),
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    if not body.confirmation:
        raise ApiError(422, "CONFIRMATION_REQUIRED", "Confirm the recipients, report version and attachment first.")
    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=f"POST /email-drafts/{draft_id}/send",
            key=key,
            payload=body.model_dump(),
            effect=lambda: mail.send(
                conn,
                principal,
                draft_id,
                version=body.version,
                confirmed_hash=body.confirmed_hash,
                if_match=expected_version,
            ),
        )
    return JSONResponse(out, status_code=status)


# --- emails -------------------------------------------------------------------------------------------------


class ReconcileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["ACCEPTED", "NOT_ACCEPTED"]
    evidence_ref: str = Field(min_length=3, max_length=300)
    reason: str = Field(min_length=3, max_length=500)


class ResendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)


@router.get("/emails", summary="Emails for reports in the caller's scope")
def list_emails(
    report_id: uuid.UUID | None = Query(default=None), principal: Principal = Depends(require(Role.SENDER))
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": mail.list_emails(conn, principal, report_id)}


@router.get("/emails/{email_id}", summary="Send state, per-recipient observations and attempts")
def get_email(email_id: uuid.UUID, principal: Principal = Depends(require(Role.SENDER))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": mail.get_email(conn, principal, email_id)}


@router.post("/emails/{email_id}/reconcile", summary="Resolve an UNKNOWN outcome with evidence")
def reconcile_email(
    email_id: uuid.UUID,
    body: ReconcileIn,
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=f"POST /emails/{email_id}/reconcile",
            key=key,
            payload=body.model_dump(),
            effect=lambda: (
                200,
                {"data": mail.reconcile(conn, principal, email_id, body.outcome, body.evidence_ref, body.reason)},
            ),
        )
    return JSONResponse(out, status_code=status)


# --- EmailJS channel (browser sends; EMAIL_PROVIDER=emailjs) ------------------------------------------------


class ClientResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["ACCEPTED", "FAILED", "UNKNOWN"]
    provider_status: int | None = Field(default=None, ge=0, le=999)
    provider_text: str | None = Field(default=None, max_length=300)


@router.post("/emails/{email_id}/client-send/claim", summary="EmailJS: claim the send once and get its variables")
def claim_client_send(
    email_id: uuid.UUID,
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    if not key or len(key) > 200:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header (1–200 characters).")
    # Not replayable by key on purpose: the state change QUEUED -> SENDING happens at most once, so a repeated
    # request can never make the browser send a second time.
    with tenant_tx(principal.tenant_id) as conn:
        data = emailjs.claim(conn, principal, email_id)
    if "error" in data:
        err = data["error"]
        return JSONResponse({"error": {"code": err["code"], "message": err["message"], "fields": []}}, status_code=409)
    return JSONResponse({"data": data})


@router.post("/emails/{email_id}/client-send/result", summary="EmailJS: record what EmailJS answered")
def client_send_result(
    email_id: uuid.UUID,
    body: ClientResultIn,
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id,
            route=f"POST /emails/{email_id}/client-send/result", key=key, payload=body.model_dump(),
            effect=lambda: (200, {"data": emailjs.record_result(conn, principal, email_id, body.outcome,
                                                                 body.provider_status, body.provider_text)}),
        )  # fmt: skip
    return JSONResponse(out, status_code=status)


@router.post("/emails/{email_id}/resend-draft", status_code=201, summary="Explicit new draft to send again")
def resend_draft(
    email_id: uuid.UUID,
    body: ResendIn,
    principal: Principal = Depends(require(Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        draft = mail.resend_draft(conn, principal, email_id, body.reason)
        _, report = mail.load_draft(conn, principal, draft.id)
        return 201, {"data": mail.draft_view(conn, draft, report)}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route=f"POST /emails/{email_id}/resend-draft",
            key=key,
            payload=body.model_dump(),
            effect=effect,
        )
    return _draft_response(status, out)


# --- history (U8) --------------------------------------------------------------------------------------------

HISTORY_ROLES = {
    "uploads": (Role.UPLOADER, Role.REVIEWER),
    "reports": (Role.REVIEWER, Role.SENDER),
    "emails": (Role.SENDER,),
    "sync": (Role.ADMIN,),
    "orders": (Role.REVIEWER, Role.SENDER, Role.VIEWER),
    "order_emails": (Role.REVIEWER, Role.SENDER),
    "owner_reports": (Role.REVIEWER, Role.SENDER, Role.ADMIN),
}
ORDER_EMAIL_TEXT = {
    "QUEUED": "Waiting to be sent",
    "SENDING": "Sending through EmailJS",
    "ACCEPTED": "Accepted by EmailJS (delivery not confirmed)",
    "FAILED": "Not sent",
    "UNKNOWN": "Unknown: check the EmailJS history",
}
SYNC_KINDS = ("sheets.sync", "erp.sync", "powerbi.refresh", "integration.test")


@router.get("/history", summary="Role-scoped history of uploads, reports, emails, sync and orders (FR21)")
def history(
    kind: Literal["uploads", "reports", "emails", "sync", "orders", "order_emails", "owner_reports"] = Query(),
    state: str | None = Query(default=None, max_length=30),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=200),
    size: int = Query(default=25),
    principal: Principal = Depends(require()),
) -> dict:
    if not principal.has_any(*HISTORY_ROLES[kind]):
        raise forbidden()
    if size not in (25, 50, 100):
        raise ApiError(400, "BAD_PAGE_SIZE", "Page size must be 25, 50 or 100.")
    j = t.job
    if kind == "uploads":
        u, b = t.upload, t.batch
        visible = b.c.department_id.in_(list(principal.department_ids))
        if not principal.has_any(Role.REVIEWER):
            visible = and_(visible, b.c.owner_id == principal.membership_id)
        q = (
            select(j, u.c.display_name, u.c.batch_id)
            .join(u, u.c.id == j.c.object_id)
            .join(b, b.c.id == u.c.batch_id)
            .where(j.c.kind.like("upload.%"), visible)
        )
        at, oid = j.c.created_at, j.c.id
    elif kind == "sync":
        q = select(j).where(j.c.kind.in_(SYNC_KINDS))
        at, oid = j.c.created_at, j.c.id
    elif kind == "reports":
        q = select(rp).where(_scoped(principal))
        at, oid = rp.c.created_at, rp.c.id
    elif kind == "orders":  # every save of an order: approval (revision 1) and each correction
        co, ov = t.customer_order, t.order_revision
        q = (
            select(ov, co.c.order_ref, co.c.id.label("order_key"))
            .join(co, co.c.id == ov.c.order_id)
            .where(co.c.department_id.in_(list(principal.department_ids)))
        )
        at, oid = ov.c.created_at, ov.c.id
    elif kind == "order_emails":
        co, oe = t.customer_order, t.order_email
        q = (
            select(oe, co.c.order_ref)
            .join(co, co.c.id == oe.c.order_id)
            .where(co.c.department_id.in_(list(principal.department_ids)))
        )
        at, oid = oe.c.created_at, oe.c.id
    elif kind == "owner_reports":  # every email of a batch report to the owner
        rd, bre, bt = t.report_delivery, t.batch_report, t.batch
        q = (
            select(rd, bre.c.batch_id, bre.c.version.label("report_version"), bre.c.file_name, bre.c.summary)
            .join(bre, bre.c.id == rd.c.report_id)
            .join(bt, bt.c.id == bre.c.batch_id)
            .where(bt.c.department_id.in_(list(principal.department_ids)))
        )
        at, oid = rd.c.created_at, rd.c.id
    else:
        em = t.email_message
        q = (
            select(em, rp.c.series_id, rp.c.version.label("report_version"))
            .join(rp, rp.c.id == em.c.report_id)
            .where(_scoped(principal))
        )
        at, oid = em.c.created_at, em.c.id
    state_col = {
        "reports": rp.c.state,
        "emails": t.email_message.c.state,
        "order_emails": t.order_email.c.state,
        "orders": t.customer_order.c.state,
        "owner_reports": t.report_delivery.c.state,
    }.get(kind, j.c.state)
    if state:
        q = q.where(state_col == state)
    if date_from:
        q = q.where(at >= datetime.combine(date_from, datetime.min.time(), UTC))
    if date_to:
        q = q.where(at < datetime.combine(date_to + timedelta(days=1), datetime.min.time(), UTC))
    if cursor:
        c_at, c_id = _decode(cursor)
        q = q.where(or_(at < c_at, and_(at == c_at, oid < c_id)))
    with tenant_tx(principal.tenant_id) as conn:
        rows = conn.execute(q.order_by(at.desc(), oid.desc()).limit(size + 1)).all()
    page = rows[:size]
    return {
        "data": [_history_row(kind, x) for x in page],
        "next_cursor": _encode(page[-1]) if len(rows) > size else None,
    }


def _history_row(kind: str, x: Any) -> dict[str, Any]:
    base = {"kind": kind, "at": x.created_at.isoformat()}
    if kind in ("uploads", "sync"):
        err = {"code": x.error_code, "message": x.error_message} if x.error_code else None
        summary = f"{x.kind} · {x.display_name}" if kind == "uploads" else x.kind
        return base | {
            "object_id": str(x.object_id),
            "job_id": str(x.id),
            "state": x.state,
            "summary": summary,
            "attempts": x.attempts,
            "max_attempts": x.max_attempts,
            "last_error": err,
            "next_retry_at": x.next_attempt_at.isoformat() if x.state == "RETRY_WAIT" else None,
            "retryable": bool(x.retryable) and x.state in ("FAILED", "PARTIAL"),
            "detail_url": f"/batches/{x.batch_id}" if kind == "uploads" else "/settings/integrations",
        }
    if kind == "orders":
        return base | {
            "object_id": str(x.order_key),
            "state": "SAVED" if x.number == 1 else "CORRECTED",
            "summary": f"{x.order_ref} · {x.customer_name} · revision {x.number}"
            + (f" · {x.reason}" if x.reason else ""),
            "last_error": None,
            "detail_url": f"/orders/{x.order_key}",
        }
    if kind == "owner_reports":
        s = x.summary or {}
        return base | {
            "object_id": str(x.batch_id),
            "state": x.state,
            "state_text": ORDER_EMAIL_TEXT[x.state],
            "summary": f"{x.file_name or 'Report'} · {s.get('orders', 0)} order(s) · to {x.to_email}"
            + (" · sent again" if x.trigger == "RESEND" else " · automatic" if x.trigger == "AUTO" else ""),
            "last_error": {"code": x.error_code, "message": x.error_message} if x.error_code else None,
            "detail_url": f"/batches/{x.batch_id}",
        }
    if kind == "order_emails":
        return base | {
            "object_id": str(x.order_id),
            "state": x.state,
            "state_text": ORDER_EMAIL_TEXT[x.state],
            "summary": f"{x.order_ref} r{x.revision_number} · {x.attachment_name} · to {x.to_email}",
            "last_error": {"code": x.error_code, "message": x.error_message} if x.error_code else None,
            "detail_url": f"/orders/{x.order_id}",
        }
    if kind == "reports":
        return base | {
            "object_id": str(x.id),
            "state": x.state,
            "outdated": x.outdated_at is not None,
            "summary": f"{x.title} · {reports.code_for(x.series_id)} v{x.version} · {x.record_count} records",
            "last_error": {"code": x.error_code, "message": x.error_message} if x.error_code else None,
            "detail_url": f"/reports/{x.id}",
        }
    return base | {
        "object_id": str(x.id),
        "state": x.state,
        "state_text": mail.STATE_TEXT[x.state],
        "summary": f"{x.subject} · {reports.code_for(x.series_id)} v{x.report_version} · "
        f"{len(x.recipients)} recipient{'s' if len(x.recipients) != 1 else ''}",
        "last_error": {"code": x.error_code, "message": x.error_message} if x.error_code else None,
        "detail_url": f"/emails/{x.id}",
    }
