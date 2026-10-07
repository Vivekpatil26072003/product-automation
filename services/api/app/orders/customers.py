"""Customers: every saved order is linked to one customer row.

Matching, strongest first: the same customer number; the same mobile number; the same name (ignoring case, spaces
and punctuation) when neither side has a different mobile. A match fills in a mobile or customer number the
customer row did not have yet; it never overwrites one, so two different people are never merged silently.
"""

import uuid
from typing import Any

from sqlalchemy import Connection, and_, func, insert, or_, select, update

from app.db import tables as t
from app.extraction.normalize import compact_key

c = t.customer


def link(conn: Connection, tenant_id: uuid.UUID, values: dict[str, Any]) -> uuid.UUID:
    name = (values.get("customer_name") or "").strip()
    mobile, number = values.get("mobile"), values.get("customer_number")
    key = compact_key(name)
    row = None
    if number:
        row = conn.execute(select(c).where(c.c.customer_number == number).limit(1)).first()
    if row is None and mobile:
        row = conn.execute(select(c).where(c.c.mobile == mobile).order_by(c.c.created_at).limit(1)).first()
    if row is None and key:
        q = select(c).where(c.c.name_key == key)
        if mobile:
            q = q.where(c.c.mobile.is_(None))
        row = conn.execute(q.order_by(c.c.created_at).limit(1)).first()
    if row is None:
        customer_id = uuid.uuid4()
        conn.execute(
            insert(c).values(
                id=customer_id, tenant_id=tenant_id, name=name, name_key=key, mobile=mobile, customer_number=number
            )
        )
        return customer_id
    fill = {}
    if mobile and not row.mobile:
        fill["mobile"] = mobile
    if number and not row.customer_number:
        fill["customer_number"] = number
    if fill:
        conn.execute(update(c).where(c.c.id == row.id).values(**fill, version=c.c.version + 1))
    return row.id


def list_customers(conn: Connection, department_ids: list[uuid.UUID], q: str | None, limit: int) -> list[dict]:
    o, ov = t.customer_order, t.order_revision
    stmt = (
        select(
            c.c.id,
            c.c.name,
            c.c.mobile,
            c.c.customer_number,
            func.count(o.c.id).label("orders"),
            func.coalesce(func.sum(ov.c.total), 0).label("total"),
            func.max(ov.c.order_date).label("last_order_date"),
        )
        .join(o, and_(o.c.customer_id == c.c.id, o.c.state == "ACTIVE", o.c.department_id.in_(department_ids)))
        .join(ov, ov.c.id == o.c.current_revision_id)
        .group_by(c.c.id)
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(c.c.name.ilike(like), c.c.mobile.ilike(like), c.c.customer_number.ilike(like)))
    rows = conn.execute(stmt.order_by(func.max(ov.c.order_date).desc().nulls_last(), c.c.name).limit(limit)).all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "mobile": r.mobile,
            "customer_number": r.customer_number,
            "orders": r.orders,
            "total": format(r.total, "f"),
            "last_order_date": r.last_order_date.isoformat() if r.last_order_date else None,
        }
        for r in rows
    ]
