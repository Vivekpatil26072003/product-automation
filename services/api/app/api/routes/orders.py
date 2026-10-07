"""Customer orders: order drafts (review form), approval, orders with revisions, customers, per-order PDF and
email sends queued for the worker (EmailJS REST API)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.api.deps import etag, idempotency_key, if_match, require
from app.auth.principal import Principal
from app.core.errors import ApiError, Issue, validation_failed
from app.core.idempotency import run_idempotent
from app.db.engine import tenant_tx
from app.domain.enums import Role
from app.orders import customers
from app.orders import fields as of
from app.orders import service as orders

router = APIRouter(tags=["orders"])
EDITORS = (Role.UPLOADER, Role.REVIEWER)
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)]
FieldText = Annotated[str, StringConstraints(max_length=2000)] | None


class ExtraItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
    value: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["new", "update"]
    order_id: uuid.UUID | None = None


class DraftPatch(BaseModel):
    """Values as typed by the reviewer (dates YYYY-MM-DD or as written; numbers as text). null clears a field.
    confirm: fields whose value as read is correct (clears "check against the page"). decision: new / update."""

    model_config = ConfigDict(extra="forbid")
    fields: dict[str, FieldText] = Field(default_factory=dict)
    confirm: list[str] = Field(default_factory=list, max_length=len(of.FIELDS))
    extra: list[ExtraItem] | None = Field(default=None, max_length=30)
    decision: Decision | None = None


class RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, FieldText] = Field(default_factory=dict)
    extra: list[ExtraItem] | None = Field(default=None, max_length=30)
    reason: Reason


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Reason


class ManualIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_no: int = Field(default=1, ge=1, le=10_000)


class EmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
    revision: int = Field(ge=1)


class ReconcileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["ACCEPTED", "FAILED"]
    note: Reason


def _check(fields: dict[str, Any] | list[str]) -> None:
    unknown = sorted(set(fields) - set(of.FIELDS))
    if unknown:
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            "Unknown fields.",
            [Issue("UNKNOWN_FIELD", f"{n} is not an order field.", f"fields.{n}") for n in unknown],
        )


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


# --- drafts ----------------------------------------------------------------------------------


@router.get("/batches/{batch_id}/order-drafts", summary="Customer order drafts of a batch (review form)")
def list_drafts(
    batch_id: uuid.UUID, include_closed: bool = Query(default=False), principal: Principal = Depends(require(*EDITORS))
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": orders.list_drafts(conn, principal, batch_id, include_closed)}


@router.post("/uploads/{upload_id}/order-drafts", status_code=201, summary="Enter a customer order for a file")
def manual_draft(
    upload_id: uuid.UUID,
    body: ManualIn | None = None,
    principal: Principal = Depends(require(*EDITORS, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    page_no = body.page_no if body else 1
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /uploads/{upload_id}/order-drafts",
            key,
            page_no,
            lambda: (201, {"data": orders.create_manual(conn, principal, upload_id, page_no)}),
        )
    return JSONResponse(out, status_code=status)


@router.get("/order-drafts/{draft_id}", summary="One order draft with evidence and issues")
def get_draft(draft_id: uuid.UUID, principal: Principal = Depends(require(*EDITORS))) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        row, _ = orders.load_draft(conn, principal, draft_id)
        data = orders.draft_view(conn, row)
    return JSONResponse({"data": data}, headers={"ETag": etag(data["version"])})


@router.patch("/order-drafts/{draft_id}", summary="Autosave corrections, confirmations and the decision (If-Match)")
def patch_draft(
    draft_id: uuid.UUID,
    payload: dict[str, Any] = Body(...),
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(*EDITORS, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    try:
        body = DraftPatch.model_validate(payload)
    except ValidationError as exc:
        raise validation_failed(
            [Issue(e["type"].upper(), e["msg"], ".".join(map(str, e["loc"]))) for e in exc.errors()]
        ) from exc
    _check(body.fields)
    _check(body.confirm)
    decision_set = "decision" in payload
    decision = body.decision.model_dump(mode="json") if body.decision else None
    extra = [x.model_dump() for x in body.extra] if body.extra is not None else None
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"PATCH /order-drafts/{draft_id}",
            key,
            [expected, payload],
            lambda: (
                200,
                {
                    "data": orders.patch_draft(
                        conn, principal, draft_id, expected, body.fields, body.confirm, extra, decision, decision_set
                    )
                },
            ),
        )
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})


@router.post("/order-drafts/{draft_id}/approve", summary="Approve an order draft: saves the order (If-Match)")
def approve_draft(
    draft_id: uuid.UUID,
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /order-drafts/{draft_id}/approve",
            key,
            expected,
            lambda: (201, {"data": orders.approve_draft(conn, principal, draft_id, expected)}),
        )
    return JSONResponse(out, status_code=status)


@router.post("/order-drafts/{draft_id}/reject", summary="Reject an order draft with a reason")
def reject_draft(
    draft_id: uuid.UUID,
    body: ReasonIn,
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /order-drafts/{draft_id}/reject",
            key,
            body.model_dump(),
            lambda: (200, {"data": orders.reject_draft(conn, principal, draft_id, body.reason)}),
        )
    return JSONResponse(out, status_code=status)


# --- orders and customers --------------------------------------------------------------------


@router.get("/orders", summary="Saved customer orders in scope, latest change first")
def list_orders(
    q: str | None = Query(default=None, max_length=100),
    customer_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require()),
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": orders.list_orders(conn, principal, q, limit, customer_id)}


@router.get("/orders/summary", summary="Order figures for the dashboard (last 30 days)")
def order_summary(principal: Principal = Depends(require())) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": orders.summary(conn, principal, datetime.now(UTC) - timedelta(days=30))}


@router.get("/customers", summary="Customers with saved orders in scope")
def list_customers(
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=200, ge=1, le=1000),
    principal: Principal = Depends(require(*orders.READ_ROLES)),
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": customers.list_customers(conn, list(principal.department_ids), q, limit)}


@router.get("/orders/{order_id}", summary="One order: latest values, revisions and emails")
def get_order(order_id: uuid.UUID, principal: Principal = Depends(require())) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        data = orders.get_order(conn, principal, order_id)
    return JSONResponse({"data": data}, headers={"ETag": etag(data["revision"])})


@router.post("/orders/{order_id}/revisions", status_code=201, summary="Correct a saved order (If-Match revision)")
def revise(
    order_id: uuid.UUID,
    body: RevisionIn,
    expected: int = Depends(if_match),
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    _check(body.fields)
    extra = [x.model_dump() for x in body.extra] if body.extra is not None else None
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /orders/{order_id}/revisions",
            key,
            [expected, body.model_dump()],
            lambda: (
                201,
                {"data": orders.revise(conn, principal, order_id, expected, body.fields, body.reason, extra)},
            ),
        )
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["revision"])})


@router.get("/orders/{order_id}/pdf", summary="PDF of the latest (or a given) saved revision")
def order_pdf(
    order_id: uuid.UUID,
    revision: int | None = Query(default=None, ge=1),
    download: bool = Query(default=False),
    principal: Principal = Depends(require()),
) -> Response:
    with tenant_tx(principal.tenant_id) as conn:
        data, name, _ = orders.render_pdf(conn, principal, order_id, revision)
    disposition = "attachment" if download else "inline"
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{name}"', "Cache-Control": "no-store"},
    )


@router.post("/orders/{order_id}/emails", status_code=202, summary="Queue an email of the latest order PDF")
def start_email(
    order_id: uuid.UUID,
    body: EmailIn,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /orders/{order_id}/emails",
            key,
            body.model_dump(),
            lambda: (202, {"data": orders.start_email(conn, principal, order_id, body.to_email, body.revision)}),
        )
    return JSONResponse(out, status_code=status)


@router.post("/orders/{order_id}/emails/{email_id}/reconcile", summary="Record the checked result of an unknown send")
def reconcile_email(
    order_id: uuid.UUID,
    email_id: uuid.UUID,
    body: ReconcileIn,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /orders/{order_id}/emails/{email_id}/reconcile",
            key,
            body.model_dump(),
            lambda: (
                200,
                {"data": orders.reconcile_email(conn, principal, order_id, email_id, body.outcome, body.note)},
            ),
        )
    return JSONResponse(out, status_code=status)
