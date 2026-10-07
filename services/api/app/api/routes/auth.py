"""API operations 1–3: GET /session, GET /auth/login, GET /auth/callback, POST /auth/logout.

POST /auth/dev-login exists only when DEV_AUTH_ENABLED and APP_ENV is development/test. It still
requires an existing active membership; it is not registration.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, insert, select, update

from app.api.deps import csrf_checked, current_principal
from app.audit import service as audit
from app.auth import sessions
from app.auth.oidc import OidcClient, pkce_pair, safe_return_to
from app.auth.principal import Principal
from app.core.config import get_settings
from app.core.errors import ApiError, not_found
from app.db import tables as t
from app.db.engine import app_engine, auth_tx, set_tenant, tenant_tx

router = APIRouter()
_STATE_TTL = timedelta(minutes=10)


def _set_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.session_cookie_name, token, max_age=s.session_ttl_hours * 3600,
        httponly=True, secure=s.session_cookie_secure, samesite="lax", path="/",
    )  # fmt: skip


def _start_session(conn: Connection, subject: str, method: str) -> tuple[str | None, str]:
    """Auth-scoped: find exactly one active membership for the subject and open a session.

    Returns (session token, "OK"), or (None, reason) when sign-in must be refused.
    """
    rows = conn.execute(
        select(t.membership.c.id, t.membership.c.tenant_id).where(
            t.membership.c.subject == subject, t.membership.c.active
        )
    ).all()
    if not rows:
        audit.security_event(conn, "SIGN_IN_REFUSED", f"no active membership method={method}")
        return None, "NO_MEMBERSHIP"
    if len(rows) > 1:
        # Release 1 is single-company; multi-company needs its own isolation review (spec §2).
        audit.security_event(conn, "SIGN_IN_REFUSED", "multiple tenant memberships")
        return None, "MULTI_TENANT_UNSUPPORTED"
    membership_id, tenant_id = rows[0]
    session_id, token = sessions.create_session(conn, tenant_id=tenant_id, membership_id=membership_id, method=method)
    set_tenant(conn, tenant_id)
    audit.record(conn, tenant_id=tenant_id, actor=audit.Actor("user", membership_id), action="SESSION_STARTED",
                 object_type="session", object_id=session_id, after={"method": method})  # fmt: skip
    return token, "OK"


def _session_body(p: Principal) -> dict:
    with tenant_tx(p.tenant_id) as conn:
        depts = conn.execute(
            select(t.department.c.id, t.department.c.code, t.department.c.name)
            .where(t.department.c.id.in_(p.department_ids))
            .order_by(t.department.c.sort_order, t.department.c.name)
        ).all()
    return {
        "data": {
            "user_id": str(p.membership_id),
            "tenant_id": str(p.tenant_id),
            "subject": p.subject,
            "display_name": p.display_name,
            "email": p.email,
            "roles": sorted(r.value for r in p.roles),
            "department_ids": [str(d.id) for d in depts],
            "departments": [{"id": str(d.id), "code": d.code, "name": d.name} for d in depts],
            "timezone": p.timezone,
            "auth_method": p.auth_method,
            "csrf_token": sessions.csrf_token_for(p.session_id),
        }
    }


@router.get("/session", summary="Current session, roles, department grants and CSRF token")
def get_session(principal: Principal = Depends(current_principal)) -> dict:
    return _session_body(principal)


@router.get("/auth/login", status_code=302, summary="Start company SSO (OIDC + PKCE)")
def login(return_to: str | None = Query(default=None, max_length=2000)) -> RedirectResponse:
    client = OidcClient(get_settings())
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    with app_engine().begin() as conn:
        conn.execute(
            insert(t.oidc_login_state).values(
                state_hash=hashlib.sha256(state.encode()).digest(), nonce=nonce, code_verifier=verifier,
                return_to=safe_return_to(return_to), expires_at=datetime.now(UTC) + _STATE_TTL,
            )
        )  # fmt: skip
    return RedirectResponse(client.authorization_url(state=state, nonce=nonce, code_challenge=challenge), 302)


@router.get("/auth/callback", status_code=302, summary="Complete company SSO")
def callback(
    code: str | None = Query(default=None, max_length=4000),
    state: str | None = Query(default=None, max_length=400),
    error: str | None = Query(default=None, max_length=200),
) -> RedirectResponse:
    client = OidcClient(get_settings())
    if not state:
        raise ApiError(400, "INVALID_STATE", "Sign-in link is invalid or expired. Start again.")
    with app_engine().begin() as conn:
        # One-time use: the UPDATE both validates and consumes the state atomically.
        row = conn.execute(
            update(t.oidc_login_state)
            .where(
                t.oidc_login_state.c.state_hash == hashlib.sha256(state.encode()).digest(),
                t.oidc_login_state.c.consumed_at.is_(None),
                t.oidc_login_state.c.expires_at > datetime.now(UTC),
            )
            .values(consumed_at=datetime.now(UTC))
            .returning(t.oidc_login_state.c.nonce, t.oidc_login_state.c.code_verifier, t.oidc_login_state.c.return_to)
        ).one_or_none()
        if row is None:
            audit.security_event(conn, "SSO_STATE_REJECTED")
    if row is None:
        raise ApiError(400, "INVALID_STATE", "Sign-in link is invalid or expired. Start again.")
    if error or not code:
        return RedirectResponse("/login?error=sso_cancelled", 302)

    tokens = client.exchange_code(code=code, code_verifier=row.code_verifier)
    if "id_token" not in tokens:
        raise ApiError(400, "SSO_TOKEN_INVALID", "Sign-in could not be verified. Try again.")
    claims = client.validate_id_token(tokens["id_token"], nonce=row.nonce)

    with auth_tx() as conn:
        token, reason = _start_session(conn, str(claims["sub"]), "oidc")
    if token is None:
        return RedirectResponse(f"/403?reason={reason.lower()}", 302)
    response = RedirectResponse(row.return_to, 302)
    _set_cookie(response, token)
    return response


class DevLogin(BaseModel):
    subject: str = Field(min_length=1, max_length=255)


@router.post("/auth/dev-login", summary="Development-only sign-in for seeded memberships", include_in_schema=False)
def dev_login(body: DevLogin, response: Response) -> dict:
    if not get_settings().dev_auth_active:
        raise not_found()
    with auth_tx() as conn:
        token, reason = _start_session(conn, body.subject, "dev")
    if token is None:
        raise ApiError(403, reason, "This identity has no active membership.")
    _set_cookie(response, token)
    principal = sessions.resolve(token)
    assert principal is not None
    return _session_body(principal)


@router.post("/auth/logout", status_code=204, summary="End the session")
def logout(response: Response, principal: Principal = Depends(csrf_checked)) -> Response:
    sessions.revoke(principal.session_id)
    with tenant_tx(principal.tenant_id) as conn:
        audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="SESSION_ENDED",
                     object_type="session", object_id=principal.session_id)  # fmt: skip
    response.status_code = 204
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return response
