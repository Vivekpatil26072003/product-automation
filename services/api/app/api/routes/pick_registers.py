"""Pick reading registers (WGS-02): list, one register with calculated totals and checks, review edits, approval,
downloads (xlsx / pdf / csv / sql) and email."""

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

router = APIRouter(tags=["pick-registers"])
Format = Literal["xlsx", "pdf", "csv", "sql"]


class EmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
    format: Format = "pdf"
    version: int = Field(ge=1)


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


@router.get("/registers", summary="Pick reading registers in scope, newest day first")
def list_registers(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    state: Literal["DRAFT", "APPROVED"] | None = Query(default=None),
    limit: int = Query(default=62, ge=1, le=400),
    principal: Principal = Depends(require()),
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": registers.list_registers(conn, principal, date_from, date_to, state, limit)}


@router.get("/registers/{register_id}", summary="One register: readings, picks, totals and checks per shift")
def get_register(register_id: uuid.UUID, principal: Principal = Depends(require())) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        data = registers.register_view(conn, principal, register_id)
    return JSONResponse({"data": data}, headers={"ETag": etag(data["version"])})


@router.patch("/registers/{register_id}", summary="Save corrections / confirmations / totals / date (If-Match)")
def patch_register(
    register_id: uuid.UUID,
    payload: dict[str, Any] = Body(...),
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(Role.UPLOADER, Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    allowed = {"values", "totals", "confirm", "notes", "register_date", "reason"}
    body = {k: v for k, v in payload.items() if k in allowed}
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"PATCH /registers/{register_id}",
            key,
            [expected, body],
            lambda: (200, {"data": registers.patch_register(conn, principal, register_id, expected, body)}),
        )
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})


@router.post("/registers/{register_id}/approve", summary="Approve the register (If-Match)")
def approve(
    register_id: uuid.UUID,
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /registers/{register_id}/approve",
            key,
            expected,
            lambda: (200, {"data": registers.approve(conn, principal, register_id, expected)}),
        )
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})


@router.get("/registers/{register_id}/file", summary="Download the register as Excel, PDF, CSV or SQL")
def register_file(
    register_id: uuid.UUID,
    format: Format = Query(default="xlsx"),
    download: bool = Query(default=True),
    principal: Principal = Depends(require()),
) -> Response:
    with tenant_tx(principal.tenant_id) as conn:
        data, name, mime = registers.file_for(conn, principal, register_id, format)
    disposition = "attachment" if download or format == "sql" else "inline"
    return Response(
        data,
        media_type=mime,
        headers={"Content-Disposition": f'{disposition}; filename="{name}"', "Cache-Control": "no-store"},
    )


@router.post("/registers/{register_id}/emails", status_code=202, summary="Email the register (sent by the worker)")
def email_register(
    register_id: uuid.UUID,
    body: EmailIn,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /registers/{register_id}/emails",
            key,
            body.model_dump(),
            lambda: (
                202,
                {"data": registers.start_email(conn, principal, register_id, body.to_email, body.format, body.version)},
            ),
        )
    return JSONResponse(out, status_code=status)
