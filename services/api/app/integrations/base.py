"""Shared adapter contract for downstream providers (FR22, spec §11).

Adapters translate provider responses into IntegrationError with a stable code and one of three
consequences: retry later (transient), stop until an administrator reconnects (reconnect), or stop writing
to this destination until someone repairs it (conflict). Provider bodies are never stored or shown: they can
contain tokens, codes or data from the destination.
"""

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings


class IntegrationError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        transient: bool = False,
        reconnect: bool = False,
        conflict: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.code, self.message = code, message
        self.transient, self.reconnect, self.conflict, self.retry_after = transient, reconnect, conflict, retry_after


def check(resp: httpx.Response, what: str) -> httpx.Response:
    """Map an HTTP answer to the contract. 2xx passes through."""
    if resp.status_code < 300:
        return resp
    if resp.status_code in (401, 403):
        raise IntegrationError(
            "AUTH_FAILED", f"{what}: the provider rejected the credentials or permissions.", reconnect=True
        )
    if resp.status_code == 404:
        raise IntegrationError("DESTINATION_NOT_FOUND", f"{what}: the destination no longer exists.", reconnect=True)
    if resp.status_code == 429 or resp.status_code >= 500:
        retry_after = float(resp.headers.get("Retry-After", "0") or 0)
        raise IntegrationError(
            "PROVIDER_UNAVAILABLE",
            f"{what}: the provider is busy or unavailable.",
            transient=True,
            retry_after=retry_after,
        )
    raise IntegrationError("PROVIDER_REJECTED", f"{what}: the provider refused the request ({resp.status_code}).")


def client(http: httpx.Client | None = None) -> httpx.Client:
    return http or httpx.Client(timeout=get_settings().integration_timeout_seconds)


def request(http: httpx.Client, method: str, url: str, what: str, **kwargs: Any) -> httpx.Response:
    try:
        return check(http.request(method, url, **kwargs), what)
    except httpx.TimeoutException as exc:
        # The request may or may not have been applied: callers must reconcile before repeating a write.
        raise IntegrationError("PROVIDER_TIMEOUT", f"{what}: no answer from the provider.", transient=True) from exc
    except httpx.HTTPError as exc:
        raise IntegrationError(
            "PROVIDER_UNREACHABLE", f"{what}: the provider could not be reached.", transient=True
        ) from exc


@dataclass
class Token:
    value: str
    expires_at: float

    @property
    def fresh(self) -> bool:
        return time.time() < self.expires_at - 60
