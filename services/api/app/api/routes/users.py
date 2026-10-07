"""API operation 56: GET/POST /users, PATCH /users/{id}.

Memberships map an existing identity-provider subject to roles and department grants. There is no
password handling. The last active administrator cannot be deactivated or demoted (TC43).
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from sqlalchemy import Connection, and_, any_, delete, func, insert, literal, or_, select, update

from app.api.deps import etag, idempotency_key, if_match, require
from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, not_found, precondition_failed
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import tenant_tx
from app.domain.enums import Role

router = APIRouter(prefix="/users", tags=["users"])
m = t.membership
_is_admin = literal(Role.ADMIN.value) == any_(m.c.roles)

Subject = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class UserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: Subject
    email: EmailStr | None = None
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    roles: list[Role] = Field(min_length=1)
    department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    active: bool = True


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr | None = None
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    roles: list[Role] | None = Field(default=None, min_length=1)
    department_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    active: bool | None = None


def _load(conn: Connection, user_id: uuid.UUID, lock: bool = False) -> dict[str, Any] | None:
    q = select(m).where(m.c.id == user_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None:
        return None
    depts = conn.execute(
        select(t.membership_department.c.department_id).where(t.membership_department.c.membership_id == user_id)
    ).scalars()
    return _out(row, sorted(str(d) for d in depts))


def _out(row: Any, department_ids: list[str]) -> dict[str, Any]:
    return {
        "id": str(row.id), "subject": row.subject, "email": row.email, "display_name": row.display_name,
        "roles": sorted(row.roles), "department_ids": department_ids, "active": row.active,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(), "version": row.version,
    }  # fmt: skip


def _set_departments(conn: Connection, tenant_id: uuid.UUID, user_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    conn.execute(delete(t.membership_department).where(t.membership_department.c.membership_id == user_id))
    unique = sorted(set(ids))
    if unique:
        found = conn.execute(select(func.count()).where(t.department.c.id.in_(unique))).scalar_one()
        if found != len(unique):
            raise ApiError(422, "INVALID_REFERENCE", "One or more departments do not exist.")
        conn.execute(
            insert(t.membership_department),
            [{"tenant_id": tenant_id, "membership_id": user_id, "department_id": d} for d in unique],
        )


def _encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created, rid = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(created), uuid.UUID(rid)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "BAD_CURSOR", "The page cursor is invalid. Reload the list.") from exc


@router.get("")
def list_users(
    cursor: str | None = Query(default=None, max_length=200),
    size: int = Query(default=25),
    principal: Principal = Depends(require(Role.ADMIN)),
) -> dict:
    if size not in (25, 50, 100):
        raise ApiError(400, "BAD_PAGE_SIZE", "Page size must be 25, 50 or 100.")
    q = select(m).order_by(m.c.created_at, m.c.id).limit(size + 1)
    if cursor:
        c_at, c_id = _decode_cursor(cursor)
        q = q.where(or_(m.c.created_at > c_at, and_(m.c.created_at == c_at, m.c.id > c_id)))
    with tenant_tx(principal.tenant_id) as conn:
        rows = conn.execute(q).all()
        total = conn.execute(select(func.count()).select_from(m)).scalar_one()
        page = rows[:size]
        grants: dict[uuid.UUID, list[str]] = {r.id: [] for r in page}
        for mid, did in conn.execute(
            select(t.membership_department.c.membership_id, t.membership_department.c.department_id).where(
                t.membership_department.c.membership_id.in_(list(grants))
            )
        ):
            grants[mid].append(str(did))
    next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > size else None
    return {"data": [_out(r, sorted(grants[r.id])) for r in page], "next_cursor": next_cursor, "total": total}


def _assert_admin_remains(conn: Connection) -> None:
    # Lock every active admin row so concurrent demotions serialize on the same set.
    conn.execute(select(m.c.id).where(m.c.active, _is_admin).with_for_update())


def _active_admins(conn: Connection) -> int:
    return conn.execute(select(func.count()).where(m.c.active, _is_admin)).scalar_one()


@router.post("", status_code=201)
def create_user(
    body: UserIn,
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        user_id = uuid.uuid4()
        conn.execute(
            insert(m).values(
                id=user_id, tenant_id=principal.tenant_id, subject=body.subject, email=body.email,
                display_name=body.display_name, roles=sorted({r.value for r in body.roles}), active=body.active,
            )
        )  # fmt: skip
        _set_departments(conn, principal.tenant_id, user_id, body.department_ids)
        data = _load(conn, user_id)
        audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="MEMBERSHIP_CREATED",
                     object_type="membership", object_id=user_id, after=data)  # fmt: skip
        return 201, {"data": data}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id, route="POST /users",
            key=key, payload=body.model_dump(mode="json"), effect=effect,
        )  # fmt: skip
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})


@router.patch("/{user_id}")
def patch_user(
    user_id: uuid.UUID,
    body: UserPatch,
    expected_version: int = Depends(if_match),
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    changes = body.model_dump(exclude_unset=True)

    def effect() -> tuple[int, dict]:
        _assert_admin_remains(conn)
        before = _load(conn, user_id, lock=True)
        if before is None:
            raise not_found()
        if before["version"] != expected_version:
            raise precondition_failed(before["version"])
        values: dict[str, Any] = {k: v for k, v in changes.items() if k in ("email", "display_name", "active")}
        if "roles" in changes:
            values["roles"] = sorted({Role(r).value for r in changes["roles"]})
        conn.execute(update(m).where(m.c.id == user_id).values(**values, version=m.c.version + 1))
        if "department_ids" in changes:
            _set_departments(conn, principal.tenant_id, user_id, changes["department_ids"])
        if _active_admins(conn) < 1:
            raise ApiError(409, "LAST_ADMIN", "The last active administrator cannot be deactivated or demoted.")
        after = _load(conn, user_id)
        audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="MEMBERSHIP_UPDATED",
                     object_type="membership", object_id=user_id, before=before, after=after)  # fmt: skip
        return 200, {"data": after}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id, route=f"PATCH /users/{user_id}",
            key=key, payload=[expected_version, body.model_dump(mode="json", exclude_unset=True)], effect=effect,
        )  # fmt: skip
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})
