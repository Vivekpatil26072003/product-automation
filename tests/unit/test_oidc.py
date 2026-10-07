"""ID token validation against a local signing key and a mocked identity provider (FR01, TC42)."""

import time

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from app.auth.oidc import OidcClient, pkce_pair
from app.core.config import get_settings
from app.core.errors import ApiError

ISSUER = "https://idp.test.invalid/tenant"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"})
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"})


def _idp(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/.well-known/openid-configuration"):
        return httpx.Response(200, json={
            "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks"})  # fmt: skip
    if request.url.path.endswith("/jwks"):
        return httpx.Response(200, json=KeySet([KEY]).as_dict(private=False))
    return httpx.Response(404)


@pytest.fixture
def client():
    OidcClient._metadata.clear()
    OidcClient._jwks.clear()
    return OidcClient(get_settings(), http=httpx.Client(transport=httpx.MockTransport(_idp)))


def _token(key=KEY, **overrides) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "production-automation-test", "sub": "user-123", "nonce": "n-1",
              "iat": now, "exp": now + 300} | overrides  # fmt: skip
    return jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)


def test_valid_token(client):
    assert client.validate_id_token(_token(), nonce="n-1")["sub"] == "user-123"


@pytest.mark.parametrize(
    "overrides",
    [{"nonce": "replayed"}, {"aud": "another-app"}, {"iss": "https://evil.invalid"}, {"exp": int(time.time()) - 3600}],
)
def test_rejected_tokens(client, overrides):
    with pytest.raises(ApiError) as exc:
        client.validate_id_token(_token(**overrides), nonce="n-1")
    assert exc.value.code == "SSO_TOKEN_INVALID"


def test_wrong_signing_key_rejected(client):
    with pytest.raises(ApiError):
        client.validate_id_token(_token(key=OTHER_KEY), nonce="n-1")


def test_authorization_url_uses_pkce_and_state(client):
    verifier, challenge = pkce_pair()
    url = httpx.URL(client.authorization_url(state="s", nonce="n", code_challenge=challenge))
    assert url.params["code_challenge_method"] == "S256"
    assert url.params["state"] == "s" and url.params["nonce"] == "n"
    assert url.params["redirect_uri"].endswith("/api/v1/auth/callback")
    assert verifier not in str(url)
