"""Master data (FR23, A6). Referenced masters are deactivated, never deleted. Department names and
codes are configurable data; nothing in the application hard-codes the seeded departments.
"""

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, Table, insert, or_, select, update

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, not_found, precondition_failed
from app.db import tables as t
from app.domain.enums import Role
from app.domain.units import normalize_alias
from app.masters.schemas import AliasIn

TABLES: dict[str, Table] = {
    "departments": t.department,
    "machines": t.machine,
    "operators": t.operator,
    "unit-aliases": t.unit_alias,
    "aliases": t.master_alias,
}
_ORDER = {
    "departments": (t.department.c.sort_order, t.department.c.name),
    "machines": (t.machine.c.code,),
    "operators": (t.operator.c.name,),
    "unit-aliases": (t.unit_alias.c.unit, t.unit_alias.c.alias_key),
    "aliases": (t.master_alias.c.kind, t.master_alias.c.alias_key),
}
_ALIAS_TARGET = {"department": t.department, "machine": t.machine, "operator": t.operator}
_HIDDEN = {"tenant_id", "alias_key"}


def serialize(row: Any) -> dict[str, Any]:
    out = {}
    for k, v in row._mapping.items():
        if k in _HIDDEN:
            continue
        if isinstance(v, uuid.UUID):
            v = str(v)
        elif isinstance(v, Decimal):
            v = format(v.normalize(), "f")
        elif hasattr(v, "isoformat"):
            v = v.isoformat()
        out[k] = v
    return out


def _table(kind: str) -> Table:
    if kind not in TABLES:
        raise not_found()
    return TABLES[kind]


def list_masters(conn: Connection, principal: Principal, kind: str, active: bool | None) -> list[dict]:
    table = _table(kind)
    q = select(table).order_by(*_ORDER[kind])
    if active is not None and "active" in table.c:
        q = q.where(table.c.active == active)

    # Administrators manage all company master data; everyone else sees only granted departments.
    if not principal.has_any(Role.ADMIN):
        grants = list(principal.department_ids)
        if kind == "departments":
            q = q.where(table.c.id.in_(grants))
        elif kind == "machines":
            q = q.where(table.c.department_id.in_(grants))
        elif kind == "operators":
            q = q.where(or_(table.c.department_id.is_(None), table.c.department_id.in_(grants)))
        elif kind == "aliases":
            granted_machines = select(t.machine.c.id).where(t.machine.c.department_id.in_(grants))
            q = q.where(
                or_(
                    table.c.department_id.in_(grants),
                    table.c.machine_id.in_(granted_machines),
                    table.c.operator_id.is_not(None),
                )
            )
    return [serialize(r) for r in conn.execute(q)]


def create_master(conn: Connection, principal: Principal, kind: str, body: BaseModel) -> dict:
    table = _table(kind)
    values: dict[str, Any] = {"id": uuid.uuid4(), "tenant_id": principal.tenant_id}
    if isinstance(body, AliasIn):
        target = _ALIAS_TARGET[body.kind]
        exists = conn.execute(select(target.c.id).where(target.c.id == body.target_id)).first()
        if not exists:
            raise ApiError(422, "INVALID_REFERENCE", f"The {body.kind} does not exist.")
        values |= {"alias": body.alias, "alias_key": normalize_alias(body.alias), f"{body.kind}_id": body.target_id}
    else:
        values |= body.model_dump()
        if kind == "unit-aliases":
            values["alias_key"] = normalize_alias(body.alias)
    row = conn.execute(insert(table).values(**values).returning(table)).one()
    data = serialize(row)
    audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="MASTER_CREATED",
                 object_type=kind, object_id=row.id, after=data)  # fmt: skip
    return data


def patch_master(
    conn: Connection, principal: Principal, kind: str, obj_id: uuid.UUID, expected_version: int, body: BaseModel
) -> dict:
    table = _table(kind)
    if kind == "aliases":
        raise ApiError(405, "METHOD_NOT_ALLOWED", "Aliases are replaced, not edited. Delete and re-create.")
    changes = body.model_dump(exclude_unset=True)
    before = conn.execute(select(table).where(table.c.id == obj_id).with_for_update()).one_or_none()
    if before is None:
        raise not_found()
    if before.version != expected_version:
        raise precondition_failed(before.version)
    if not changes:
        return serialize(before)
    row = conn.execute(
        update(table).where(table.c.id == obj_id).values(**changes, version=table.c.version + 1).returning(table)
    ).one()
    data = serialize(row)
    audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="MASTER_UPDATED",
                 object_type=kind, object_id=obj_id, before=serialize(before), after=data)  # fmt: skip
    return data


def delete_alias(conn: Connection, principal: Principal, obj_id: uuid.UUID) -> None:
    table = t.master_alias
    before = conn.execute(select(table).where(table.c.id == obj_id)).one_or_none()
    if before is None:
        raise not_found()
    conn.execute(table.delete().where(table.c.id == obj_id))
    audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="ALIAS_DELETED",
                 object_type="aliases", object_id=obj_id, before=serialize(before))  # fmt: skip
