"""Service identities for providers (spec §11): a Google service account and a Microsoft Entra application.

Google: OAuth 2.0 JWT bearer grant signed with the service account key (RFC 7523).
Microsoft: client credentials grant against the configured Entra tenant (".default" scope).
Tokens live only in memory for the duration of a job.
"""

import base64
import json
import time
from typing import Any

import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.integrations.base import IntegrationError, Token, request

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def google_token(http: httpx.Client, service_account: dict[str, Any], scope: str = SHEETS_SCOPE) -> Token:
    try:
        key = RSAKey.import_key(service_account["private_key"])
        email = service_account["client_email"]
    except (KeyError, ValueError, TypeError) as exc:
        raise IntegrationError("INVALID_CREDENTIALS", "The service account key is not valid.", reconnect=True) from exc
    now = int(time.time())
    token_uri = service_account.get("token_uri") or GOOGLE_TOKEN_URL
    assertion = jwt.encode(
        {"alg": "RS256", "typ": "JWT"},
        {"iss": email, "scope": scope, "aud": token_uri, "iat": now, "exp": now + 3600},
        key,
    )
    resp = request(
        http,
        "POST",
        token_uri,
        "Google sign-in",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
    )
    body = resp.json()
    return Token(body["access_token"], time.time() + float(body.get("expires_in", 3600)))


def entra_token(http: httpx.Client, tenant: str, client_id: str, client_secret: str, scope: str) -> Token:
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    resp = request(
        http,
        "POST",
        url,
        "Microsoft sign-in",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        },
    )
    body = resp.json()
    return Token(body["access_token"], time.time() + float(body.get("expires_in", 3600)))


def token_roles(token: Token) -> list[str]:
    """Application permissions granted in an app-only Entra token (the "roles" claim).

    Read without signature verification: the token came straight from Microsoft over TLS and is used only to
    show an administrator which permissions are present, never to authorize anything in this application.
    """
    try:
        payload = token.value.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return list(claims.get("roles", []))
    except (IndexError, ValueError):
        return []
