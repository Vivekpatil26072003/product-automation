"""Request dependencies: authentication, role checks, CSRF, If-Match and Idempotency-Key."""

import re
from collections.abc import Callable

from fastapi import Depends, Header, Request

from app.auth import sessions
from app.auth.principal import Principal
from app.core.config import get_settings
from app.core.errors import ApiError, forbidden, unauthenticated
from app.domain.enums import Role


def current_principal(request: Request) -> Principal:
    principal = sessions.resolve(request.cookies.get(get_settings().session_cookie_name))
    if principal is None:
        raise unauthenticated()
    return principal


def csrf_checked(
    principal: Principal = Depends(current_principal),
    x_csrf_token: str | None = Header(default=None),
) -> Principal:
    if not sessions.csrf_valid(principal.session_id, x_csrf_token):
        raise ApiError(403, "CSRF_FAILED", "Your session token is missing or stale. Reload the page.")
    return principal


def require(*roles: Role, mutation: bool = False) -> Callable[..., Principal]:
    """Server-side role gate. Hidden buttons are never a permission control."""
    base = csrf_checked if mutation else current_principal

    def dependency(principal: Principal = Depends(base)) -> Principal:
        if roles and not principal.has_any(*roles):
            raise forbidden()
        return principal

    return dependency


_ETAG = re.compile(r'^(?:W/)?"?(\d{1,9})"?$')


def if_match(if_match: str | None = Header(default=None, alias="If-Match")) -> int:
    if not if_match:
        raise ApiError(428, "PRECONDITION_REQUIRED", "Send If-Match with the version you are editing.")
    m = _ETAG.match(if_match.strip())
    if not m:
        raise ApiError(400, "BAD_IF_MATCH", "If-Match must be the item's ETag.")
    return int(m[1])


def idempotency_key(key: str | None = Header(default=None, alias="Idempotency-Key")) -> str | None:
    return key


def etag(version: int) -> str:
    return f'"{version}"'
