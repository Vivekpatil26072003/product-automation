"""Owner reports: settings (administrators), batch processing status, batch report PDFs and their owner emails,
and the worker's "my diary pages" list."""

import base64
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from app.api.deps import etag, idempotency_key, if_match, require
from app.audit import service as audit
from app.auth.principal import Principal
from app.core.crypto import SecretsUnavailable
from app.core.errors import ApiError
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import tenant_tx
from app.domain.enums import Role
from app.mail import emailjs_api
from app.owner_reports import service as owner_reports
from app.owner_reports import settings as owner_settings
from app.storage.objects import get_storage

router = APIRouter(tags=["owner-reports"])
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=300)]


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)] | None = None
    auto_send: bool | None = None
    company_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    emailjs_service_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    emailjs_template_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    emailjs_public_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    emailjs_private_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    clear_private_key: bool = False
    max_request_kb: int | None = Field(default=None, ge=10, le=30000)


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email_owner: bool = False


class DeliveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resend: bool = False


class ReconcileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["ACCEPTED", "FAILED"]
    note: Reason


def _idem(conn, principal: Principal, route: str, key: str | None, payload: Any, effect) -> tuple[int, dict]:
    return run_idempotent(
        conn,
        tenant_id=principal.tenant_id,
        actor_id=principal.membership_id,
        route=route,
        key=key,
        payload=payload,
        effect=effect,
    )


# --- settings (administrators) ---------------------------------------------------------------


@router.get("/settings/owner-report", summary="Owner report and EmailJS settings (private key never returned)")
def get_settings(principal: Principal = Depends(require(Role.ADMIN))) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        data = owner_settings.view(conn, principal.tenant_id)
    return JSONResponse({"data": data}, headers={"ETag": etag(data["version"])})


@router.put("/settings/owner-report", summary="Change owner report and EmailJS settings (If-Match)")
def put_settings(
    body: SettingsIn,
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
) -> JSONResponse:
    values = body.model_dump(exclude_unset=True)
    with tenant_tx(principal.tenant_id) as conn:
        data = owner_settings.update(conn, principal, values, expected)
    return JSONResponse({"data": data}, headers={"ETag": etag(data["version"])})


@router.post("/settings/owner-report/test", summary="Send a test email with a small PDF to the owner now")
def test_email(principal: Principal = Depends(require(Role.ADMIN, mutation=True))) -> dict:
    """Proves the whole path (service, template, keys, variable attachment) with a real email to the owner."""
    from app.orders.pdf import _STYLES, _Text  # small one-page PDF, same fonts as the real report

    with tenant_tx(principal.tenant_id) as conn:
        row = owner_settings.load(conn, principal.tenant_id)
        if row is None or not row.owner_email:
            raise ApiError(409, "OWNER_EMAIL_MISSING", "Enter the owner's email first.")
        try:
            config = owner_settings.emailjs_config(conn, principal.tenant_id)
        except SecretsUnavailable as exc:
            raise ApiError(
                409, "SECRETS_UNAVAILABLE", "The stored private key cannot be read. Enter it again."
            ) from exc
        if config is None:
            raise ApiError(409, "EMAIL_NOT_CONFIGURED", "Complete the EmailJS settings first.")
        company = owner_settings.company_name(conn, principal.tenant_id, row)
    import io

    from reportlab.platypus import Paragraph, SimpleDocTemplate

    buf = io.BytesIO()
    tx = _Text()
    SimpleDocTemplate(buf, invariant=1, pageCompression=1).build(
        [
            Paragraph(tx(f"Test PDF from {company}"), _STYLES["title"]),
            Paragraph(tx("If this file is attached, owner reports will arrive with their PDF."), _STYLES["base"]),
        ]
    )
    data = buf.getvalue()
    params = {
        "to_email": row.owner_email,
        "subject": f"Test email - {company} diary reports",
        "message": "Hello,\n\nThis is a test from the diary automation system. The attached PDF proves that "
        "report attachments work.\n\nThank you.",
        "record_reference": "TEST",
        "customer_name": "",
        "company_name": company,
        "from_name": company,
        "reply_to": "",
        "attachment_name": "Test_Report.pdf",
        "pdf_file": f"data:application/pdf;base64,{base64.b64encode(data).decode()}",
        "email_reference": str(uuid.uuid4()),
    }
    result = emailjs_api.send(config, params)
    with tenant_tx(principal.tenant_id) as conn:
        audit.record(
            conn,
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            action="OWNER_REPORT_TEST_EMAIL",
            object_type="tenant",
            object_id=principal.tenant_id,
            after={"outcome": result.outcome, "http_status": result.status},
        )
    ok = result.outcome == "ACCEPTED"
    return {
        "data": {
            "outcome": result.outcome,
            "http_status": result.status,
            "to_email": row.owner_email,
            "message": f"EmailJS accepted the test email for {row.owner_email}." if ok else emailjs_api.explain(result),
        }
    }


# --- batches ---------------------------------------------------------------------------------


@router.get("/batches/{batch_id}/pipeline", summary="Processing status of a batch, step by step")
def batch_pipeline(batch_id: uuid.UUID, principal: Principal = Depends(require())) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": owner_reports.pipeline(conn, principal, batch_id)}


@router.get("/diary/batches", summary="My recent diary uploads with their status (worker screen)")
def my_batches(limit: int = Query(default=10, ge=1, le=50), principal: Principal = Depends(require())) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        b = t.batch
        rows = conn.execute(
            select(b.c.id, b.c.created_at, b.c.file_count)
            .where(b.c.owner_id == principal.membership_id)
            .order_by(b.c.created_at.desc())
            .limit(limit)
        ).all()
        out = []
        for r in rows:
            p = owner_reports.pipeline(conn, principal, r.id)
            out.append({"id": str(r.id), "created_at": r.created_at.isoformat(), "files": r.file_count, **p})
    return {"data": out}


@router.get("/batches/{batch_id}/reports", summary="Owner reports of a batch with their email deliveries")
def list_reports(batch_id: uuid.UUID, principal: Principal = Depends(require())) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": owner_reports.list_reports(conn, principal, batch_id)}


@router.post("/batches/{batch_id}/reports", status_code=201, summary="Create the batch report PDF (and email it)")
def create_report(
    batch_id: uuid.UUID,
    body: ReportIn,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /batches/{batch_id}/reports",
            key,
            body.model_dump(),
            lambda: (201, {"data": owner_reports.create_manual(conn, principal, batch_id, body.email_owner)}),
        )
    return JSONResponse(out, status_code=status)


@router.get("/batch-reports/{report_id}/pdf", summary="The batch report PDF")
def report_pdf(
    report_id: uuid.UUID, download: bool = Query(default=False), principal: Principal = Depends(require())
) -> Response:
    with tenant_tx(principal.tenant_id) as conn:
        report = owner_reports.report_file(conn, principal, report_id)
    data = get_storage().get_bytes(report.file_key, 20_000_000)
    disposition = "attachment" if download else "inline"
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{report.file_name}"', "Cache-Control": "no-store"},
    )


@router.post("/batch-reports/{report_id}/deliveries", status_code=202, summary="Email the report to the owner")
def deliver(
    report_id: uuid.UUID,
    body: DeliveryIn,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /batch-reports/{report_id}/deliveries",
            key,
            body.model_dump(),
            lambda: (202, {"data": owner_reports.request_delivery(conn, principal, report_id, body.resend)}),
        )
    return JSONResponse(out, status_code=status)


@router.post("/report-deliveries/{delivery_id}/reconcile", summary="Record the checked result of an unknown send")
def reconcile(
    delivery_id: uuid.UUID,
    body: ReconcileIn,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /report-deliveries/{delivery_id}/reconcile",
            key,
            body.model_dump(),
            lambda: (
                200,
                {"data": owner_reports.reconcile_delivery(conn, principal, delivery_id, body.outcome, body.note)},
            ),
        )
    return JSONResponse(out, status_code=status)


@router.get("/batches/{batch_id}/diary-data", summary="Orders read from a batch as one table (saved and to review)")
def diary_data(batch_id: uuid.UUID, principal: Principal = Depends(require())) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": owner_reports.diary_data(conn, principal, batch_id)}


@router.get("/batches/{batch_id}/diary-data/pdf", summary="The diary data table of a batch as a PDF")
def diary_pdf(batch_id: uuid.UUID, principal: Principal = Depends(require())) -> Response:
    with tenant_tx(principal.tenant_id) as conn:
        data, name = owner_reports.diary_pdf(conn, principal, batch_id)
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )
