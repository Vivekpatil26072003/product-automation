"""Read-only database logins for Power BI (FR15). Run by operations as the database owner.

The login can SELECT only approved_production_v and production_watermark_v, and those views show only
the companies mapped to that login in bi_access. It cannot read any table directly.
"""

import re
import uuid

from psycopg import sql
from sqlalchemy import Connection, text

ROLE_NAME = re.compile(r"^bi_[a-z0-9_]{2,40}$")
VIEWS = ("approved_production_v", "production_watermark_v")


def grant(conn: Connection, role: str, password: str | None, tenant_id: uuid.UUID) -> bool:
    """Create the login if needed (then `password` is required) and map it to the company.

    Returns True when the login was created.
    """
    if not ROLE_NAME.match(role):
        raise ValueError("role names must look like bi_<company>: lowercase letters, digits and _")
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar() is not None
    cur = conn.connection.dbapi_connection.cursor()  # type: ignore[union-attr]
    if not exists:
        if not password or len(password) < 16:
            raise ValueError("a new login needs a password of at least 16 characters")
        cur.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(role), sql.Literal(password))
        )
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
    for view in VIEWS:
        cur.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(view), sql.Identifier(role)))
    conn.execute(
        text("INSERT INTO bi_access (role_name, tenant_id) VALUES (:r, :t) ON CONFLICT DO NOTHING"),
        {"r": role, "t": tenant_id},
    )
    return not exists


def revoke(conn: Connection, role: str, tenant_id: uuid.UUID) -> None:
    conn.execute(text("DELETE FROM bi_access WHERE role_name = :r AND tenant_id = :t"), {"r": role, "t": tenant_id})
