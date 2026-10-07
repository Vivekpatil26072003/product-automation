"""Daily production sheets: list ("SQL sheet"), one sheet with calculated columns, review edits, approval,
downloads (xlsx / pdf / csv), email, and targets."""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.api.deps import etag, idempotency_key, if_match, require
from app.auth.principal import Principal
from app.core.idempotency import run_idempotent
from app.db.engine import tenant_tx
from app.domain.enums import Role
from app.pick_registers import service as registers
from app.shift_reports import service as sheets

router = APIRouter(tags=["daily-sheets"])


class EmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
    format: Literal["xlsx", "pdf", "csv"] = "pdf"
    version: int = Field(ge=1)


class TargetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: Annotated[str, StringConstraints(max_length=60)]
    metric: Annotated[str, StringConstraints(max_length=60)]
    value: str | None = Field(default=None, max_length=30)


class TargetsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[TargetItem] = Field(min_length=1, max_length=400)


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


@router.get("/sheets", summary="Daily production sheets in scope, newest day first")
def list_sheets(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    state: Literal["DRAFT", "APPROVED"] | None = Query(default=None),
    supervisor: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=62, ge=1, le=400),
    principal: Principal = Depends(require()),
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": sheets.list_sheets(conn, principal, date_from, date_to, state, supervisor, limit)}


@router.get("/sheet-targets", summary="Targets and table parameters of the daily sheet")
def get_targets(principal: Principal = Depends(require())) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": sheets.targets_view(conn, principal)}


@router.put("/sheet-targets", summary="Change targets / table parameters (administrators)")
def put_targets(body: TargetsIn, principal: Principal = Depends(require(Role.ADMIN, mutation=True))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": sheets.set_targets(conn, principal, [i.model_dump() for i in body.items])}


@router.get("/batches/{batch_id}/sheets", summary="Daily sheets and pick registers that pages of this upload went into")
def batch_sheets(batch_id: uuid.UUID, principal: Principal = Depends(require())) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        daily = [s | {"kind": "sheet"} for s in sheets.for_batch(conn, principal, batch_id)]
        return {"data": daily + registers.for_batch(conn, principal, batch_id)}


@router.get("/sheets/{report_id}", summary="One daily sheet with shift values, totals and to-date")
def get_sheet(report_id: uuid.UUID, principal: Principal = Depends(require())) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        data = sheets.sheet_view(conn, principal, report_id)
    return JSONResponse({"data": data}, headers={"ETag": etag(data["version"])})


@router.patch("/sheets/{report_id}", summary="Save corrections / confirmations / supervisors / date (If-Match)")
def patch_sheet(
    report_id: uuid.UUID,
    payload: dict[str, Any] = Body(...),
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(Role.UPLOADER, Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    allowed = {"values", "confirm", "shifts", "notes", "report_date", "reason"}
    body = {k: v for k, v in payload.items() if k in allowed}
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"PATCH /sheets/{report_id}",
            key,
            [expected, body],
            lambda: (200, {"data": sheets.patch_sheet(conn, principal, report_id, expected, body)}),
        )
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})


@router.post("/sheets/{report_id}/approve", summary="Approve the sheet (If-Match)")
def approve(
    report_id: uuid.UUID,
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /sheets/{report_id}/approve",
            key,
            expected,
            lambda: (200, {"data": sheets.approve(conn, principal, report_id, expected)}),
        )
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})


@router.get("/sheets/{report_id}/file", summary="Download the sheet as Excel, PDF or CSV")
def sheet_file(
    report_id: uuid.UUID,
    format: Literal["xlsx", "pdf", "csv"] = Query(default="xlsx"),
    download: bool = Query(default=True),
    principal: Principal = Depends(require()),
) -> Response:
    with tenant_tx(principal.tenant_id) as conn:
        data, name, mime = sheets.file_for(conn, principal, report_id, format)
    disposition = "attachment" if download else "inline"
    return Response(
        data,
        media_type=mime,
        headers={"Content-Disposition": f'{disposition}; filename="{name}"', "Cache-Control": "no-store"},
    )


@router.post("/sheets/{report_id}/emails", status_code=202, summary="Email the sheet (sent by the worker)")
def email_sheet(
    report_id: uuid.UUID,
    body: EmailIn,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /sheets/{report_id}/emails",
            key,
            body.model_dump(),
            lambda: (
                202,
                {"data": sheets.start_email(conn, principal, report_id, body.to_email, body.format, body.version)},
            ),
        )
    return JSONResponse(out, status_code=status)
