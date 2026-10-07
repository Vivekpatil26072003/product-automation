"""Server-side sessions. The browser holds an opaque random token in a Secure HttpOnly SameSite cookie;
the database stores only its SHA-256. The CSRF token is an HMAC of the session ID, so it needs no
storage and is useless without the session cookie.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, and_, insert, select, update

from app.auth.principal import Principal
from app.core.config import get_settings
from app.db import tables as t
from app.db.engine import auth_tx, set_tenant
from app.domain.enums import Role

_TOUCH_INTERVAL = timedelta(minutes=5)


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def csrf_token_for(session_id: uuid.UUID) -> str:
    key = get_settings().session_secret.encode()
    return hmac.new(key, f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()


def csrf_valid(session_id: uuid.UUID, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(csrf_token_for(session_id), presented)


def create_session(
    conn: Connection, *, tenant_id: uuid.UUID, membership_id: uuid.UUID, method: str
) -> tuple[uuid.UUID, str]:
    """Must run in an auth-scoped transaction. Returns (session_id, raw cookie token)."""
    token = secrets.token_urlsafe(32)
    session_id = uuid.uuid4()
    conn.execute(
        insert(t.auth_session).values(
            id=session_id,
            tenant_id=tenant_id,
            membership_id=membership_id,
            token_hash=_hash(token),
            auth_method=method,
            expires_at=datetime.now(UTC) + timedelta(hours=get_settings().session_ttl_hours),
        )
    )
    return session_id, token


def resolve(token: str | None) -> Principal | None:
    if not token or len(token) > 200:
        return None
    now = datetime.now(UTC)
    s, m, tn = t.auth_session, t.membership, t.tenant
    with auth_tx() as conn:
        row = conn.execute(
            select(
                s.c.id.label("session_id"), s.c.last_seen_at, s.c.auth_method, m.c.id.label("membership_id"),
                m.c.tenant_id, m.c.subject, m.c.display_name, m.c.email, m.c.roles, tn.c.timezone,
            )
            .select_from(s.join(m, and_(m.c.id == s.c.membership_id, m.c.tenant_id == s.c.tenant_id)))
            .join(tn, tn.c.id == s.c.tenant_id)
            .where(s.c.token_hash == _hash(token), s.c.revoked_at.is_(None), s.c.expires_at > now, m.c.active)
        ).one_or_none()  # fmt: skip
        if row is None:
            return None
        if now - row.last_seen_at > _TOUCH_INTERVAL:
            conn.execute(update(s).where(s.c.id == row.session_id).values(last_seen_at=now))

        set_tenant(conn, row.tenant_id)
        departments = conn.execute(
            select(t.membership_department.c.department_id)
            .join(t.department, t.department.c.id == t.membership_department.c.department_id)
            .where(t.membership_department.c.membership_id == row.membership_id, t.department.c.active)
        ).scalars()
        return Principal(
            membership_id=row.membership_id,
            tenant_id=row.tenant_id,
            session_id=row.session_id,
            subject=row.subject,
            display_name=row.display_name,
            email=row.email,
            roles=frozenset(Role(r) for r in row.roles),
            department_ids=frozenset(departments),
            timezone=row.timezone,
            auth_method=row.auth_method,
        )


def revoke(session_id: uuid.UUID) -> None:
    with auth_tx() as conn:
        conn.execute(
            update(t.auth_session)
            .where(t.auth_session.c.id == session_id, t.auth_session.c.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
