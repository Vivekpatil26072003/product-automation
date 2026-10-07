"""Company SSO via OpenID Connect authorization code flow with PKCE and nonce (FR01, API ops 2–3).

No public registration: a verified identity signs in only if an administrator has mapped its subject
to an active membership.
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from app.core.config import Settings
from app.core.errors import ApiError

_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]
_CACHE_SECONDS = 3600


def safe_return_to(value: str | None) -> str:
    """Only same-origin absolute paths. Anything else falls back to "/" (no open redirect)."""
    if not value or len(value) > 2000 or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(c) < 32 for c in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class _Cached:
    value: dict[str, Any]
    at: float


class OidcClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        if not (settings.oidc_issuer and settings.oidc_client_id):
            raise ApiError(503, "SSO_NOT_CONFIGURED", "Company sign-in is not configured.")
        self.issuer = settings.oidc_issuer.rstrip("/")
        self.client_id = settings.oidc_client_id
        self.client_secret = settings.oidc_client_secret
        self.scopes = settings.oidc_scopes
        self.redirect_uri = settings.public_base_url.rstrip("/") + "/api/v1/auth/callback"
        self._http = http or httpx.Client(timeout=10)

    _metadata: dict[str, _Cached] = {}
    _jwks: dict[str, _Cached] = {}

    def _get_json(self, url: str, cache: dict[str, _Cached]) -> dict[str, Any]:
        hit = cache.get(url)
        if hit and time.monotonic() - hit.at < _CACHE_SECONDS:
            return hit.value
        try:
            resp = self._http.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ApiError(503, "SSO_UNAVAILABLE", "Company sign-in is temporarily unavailable.") from exc
        cache[url] = _Cached(resp.json(), time.monotonic())
        return cache[url].value

    def metadata(self) -> dict[str, Any]:
        meta = self._get_json(f"{self.issuer}/.well-known/openid-configuration", self._metadata)
        if meta.get("issuer", "").rstrip("/") != self.issuer:
            raise ApiError(503, "SSO_MISCONFIGURED", "Identity provider issuer does not match configuration.")
        return meta

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.metadata()['authorization_endpoint']}?{query}"

    def exchange_code(self, *, code: str, code_verifier: str) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        }
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        try:
            resp = self._http.post(self.metadata()["token_endpoint"], data=data, auth=auth)
        except httpx.HTTPError as exc:
            raise ApiError(503, "SSO_UNAVAILABLE", "Company sign-in is temporarily unavailable.") from exc
        if resp.status_code != 200:
            # Provider bodies are not echoed back: they can contain codes or diagnostic secrets.
            raise ApiError(400, "SSO_EXCHANGE_FAILED", "Sign-in could not be completed. Try again.")
        return resp.json()

    def validate_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        keys = KeySet.import_key_set(self._get_json(self.metadata()["jwks_uri"], self._jwks))
        try:
            token = jwt.decode(id_token, keys, algorithms=_ALGORITHMS)
            jwt.JWTClaimsRegistry(
                leeway=60,
                iss={"essential": True, "value": self.metadata()["issuer"]},
                aud={"essential": True, "value": self.client_id},
                sub={"essential": True},
                exp={"essential": True},
                nonce={"essential": True, "value": nonce},
            ).validate(token.claims)
        except (JoseError, ValueError) as exc:
            raise ApiError(400, "SSO_TOKEN_INVALID", "Sign-in could not be verified. Try again.") from exc
        return dict(token.claims)
