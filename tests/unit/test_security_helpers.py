"""Security helpers that need no infrastructure (FR01, FR25)."""

import uuid

import pytest
from pydantic import ValidationError

from app.auth.oidc import safe_return_to
from app.auth.sessions import csrf_token_for, csrf_valid
from app.core.config import Settings
from app.core.logging import redact
from app.storage.objects import key_belongs_to, new_object_key


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/records?date=2026-09-27", "/records?date=2026-09-27"),
        ("/", "/"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example/x", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("/ok\r\nSet-Cookie: x", "/"),
        ("records", "/"),
    ],
)
def test_return_to_is_same_origin_only(value, expected):
    assert safe_return_to(value) == expected


def test_csrf_token_is_bound_to_session():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert csrf_valid(a, csrf_token_for(a))
    assert not csrf_valid(a, csrf_token_for(b))
    assert not csrf_valid(a, None) and not csrf_valid(a, "")


@pytest.mark.parametrize(
    "line",
    [
        "GET https://s3.local/b/k?X-Amz-Signature=abc123&X-Amz-Credential=zz",
        "Authorization: Bearer eyJhbGciOi.payload.sig",
        "client_secret=supersecretvalue",
        "password: hunter2",
    ],
)
def test_logs_never_contain_secrets_or_signed_urls(line):  # TC49 (unit part)
    out = redact(line)
    for secret in ("abc123", "eyJhbGciOi", "supersecretvalue", "hunter2"):
        assert secret not in out


def test_object_keys_are_generated_and_tenant_scoped():
    tenant = uuid.uuid4()
    key = new_object_key("quarantine", tenant)
    area, tid, name = key.split("/")
    assert (area, tid) == ("quarantine", str(tenant)) and uuid.UUID(name)
    assert key_belongs_to(key, tenant)
    assert not key_belongs_to(key, uuid.uuid4())
    assert not key_belongs_to(f"quarantine/{tenant}/../../etc/passwd", tenant)
    with pytest.raises(ValueError):
        new_object_key("public", tenant)  # type: ignore[arg-type]


def test_settings_refuse_dev_auth_outside_development():
    with pytest.raises(ValidationError):
        Settings(app_env="production", dev_auth_enabled=True, session_secret="s" * 40)


def test_settings_require_strong_secret_and_secure_cookie_in_production():
    with pytest.raises(ValidationError):
        Settings(app_env="development", session_secret="short")
    with pytest.raises(ValidationError):
        Settings(app_env="production", session_secret="s" * 40, session_cookie_secure=False)
