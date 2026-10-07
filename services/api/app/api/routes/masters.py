"""API operations 54–55: GET/POST /masters/{kind}, PATCH /masters/{kind}/{id}."""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from app.api.deps import current_principal, etag, idempotency_key, if_match, require
from app.auth.principal import Principal
from app.core.errors import Issue, validation_failed
from app.core.idempotency import run_idempotent
from app.db.engine import tenant_tx
from app.domain.enums import Role
from app.masters import service
from app.masters.schemas import CREATE_MODELS, PATCH_MODELS

router = APIRouter(prefix="/masters", tags=["masters"])
Kind = Literal["departments", "machines", "operators", "unit-aliases", "aliases"]
PatchKind = Literal["departments", "machines", "operators", "unit-aliases"]


def _parse(model, payload: dict[str, Any]):
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise validation_failed(
            [
                Issue(code=e["type"].upper(), message=e["msg"], field=".".join(map(str, e["loc"])) or None)
                for e in exc.errors()
            ]  # fmt: skip
        ) from exc


def _respond(status: int, body: dict) -> JSONResponse:
    headers = {"ETag": etag(body["data"]["version"])} if "version" in body.get("data", {}) else None
    return JSONResponse(body, status_code=status, headers=headers)


@router.get("/{kind}")
def list_masters(
    kind: Kind, active: bool | None = Query(default=None), principal: Principal = Depends(current_principal)
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        rows = service.list_masters(conn, principal, kind, active)
    return {"data": rows, "next_cursor": None, "total": len(rows)}


@router.post("/{kind}", status_code=201)
def create_master(
    kind: Kind,
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    body = _parse(CREATE_MODELS[kind], payload)
    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id, route=f"POST /masters/{kind}",
            key=key, payload=payload,
            effect=lambda: (201, {"data": service.create_master(conn, principal, kind, body)}),
        )  # fmt: skip
    return _respond(status, out)


@router.patch("/{kind}/{obj_id}")
def patch_master(
    kind: PatchKind,
    obj_id: uuid.UUID,
    payload: dict[str, Any] = Body(...),
    expected_version: int = Depends(if_match),
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    body = _parse(PATCH_MODELS[kind], payload)
    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id,
            route=f"PATCH /masters/{kind}/{obj_id}", key=key, payload=[expected_version, payload],
            effect=lambda: (200, {"data": service.patch_master(conn, principal, kind, obj_id, expected_version, body)}),
        )  # fmt: skip
    return _respond(status, out)


@router.delete("/aliases/{obj_id}", status_code=204)
def delete_alias(
    obj_id: uuid.UUID,
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> Response:
    with tenant_tx(principal.tenant_id) as conn:

        def effect() -> tuple[int, dict]:
            service.delete_alias(conn, principal, obj_id)
            return 204, {}

        run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id,
            route=f"DELETE /masters/aliases/{obj_id}", key=key, payload=None, effect=effect,
        )  # fmt: skip
    return Response(status_code=204)
