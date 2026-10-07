"""Shared fixtures.

Unit tests need nothing running. Tests marked `db` need the PostgreSQL from infra/local and are
skipped (not passed) when it is unreachable; the skip reason is printed by `-ra`.
"""

import os
import uuid

# Test configuration must be in place before any app module reads settings.
os.environ.update(
    APP_ENV="test",
    DATABASE_URL=os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://prod_app:prod_app_dev@localhost:5433/production_test?connect_timeout=3",
    ),
    DATABASE_OWNER_URL=os.environ.get(
        "TEST_DATABASE_OWNER_URL",
        "postgresql+psycopg://prod_owner:prod_owner_dev@localhost:5433/production_test?connect_timeout=3",
    ),
    SESSION_SECRET="test-session-secret-" + "x" * 32,
    SESSION_COOKIE_SECURE="false",
    DEV_AUTH_ENABLED="true",
    STORAGE_BUCKET="prodauto-test",
    OIDC_ISSUER="https://idp.test.invalid/tenant",
    OIDC_CLIENT_ID="production-automation-test",
    INTEGRATION_KEYS="test1:" + "A" * 43 + "=",
    EMAIL_PROVIDER="graph",  # tests choose the channel explicitly; never inherit it from a developer .env
    # A developer .env may hold a real EmailJS account: tests never use it (no real email can be sent).
    EMAILJS_SERVICE_ID="",
    EMAILJS_TEMPLATE_ID="",
    EMAILJS_PUBLIC_KEY="",
    EMAILJS_PRIVATE_KEY="",
    # Nor a real OCR / AI account (e.g. a Gemini key): tests that need a reader give it a recorded stand-in.
    OCR_PROVIDER="none",
    AI_PROVIDER="none",
    GEMINI_API_KEY="",
)

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(scope="session")
def owner_engine():
    engine = create_engine(os.environ["DATABASE_OWNER_URL"], future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("PostgreSQL test database unreachable (start infra/local with docker compose)")
    from app.cli import migrate

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    migrate(os.environ["DATABASE_OWNER_URL"])
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _db_marker_requires_database(request):
    """Every `db` test goes through owner_engine, so an unreachable database skips instead of hanging."""
    if request.node.get_closest_marker("db"):
        request.getfixturevalue("owner_engine")


@pytest.fixture
def seeded(owner_engine):
    """A fresh tenant per test with unique sign-in subjects (dev-admin, dev-reviewer, ...)."""
    from app.seed.demo import seed_tenant

    prefix = f"{uuid.uuid4().hex[:8]}-"
    with owner_engine.begin() as conn:
        res = seed_tenant(conn, f"Test tenant {prefix}", subject_prefix=prefix)
    res.subject_prefix = prefix  # type: ignore[attr-defined]
    return res


@pytest.fixture
def client(owner_engine):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def sign_in(client, seeded, who: str) -> dict:
    """Dev sign-in as a seeded role; installs the CSRF header on the client. Returns the session body."""
    client.cookies.clear()
    r = client.post("/api/v1/auth/dev-login", json={"subject": seeded.subject_prefix + who})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    client.headers["X-CSRF-Token"] = body["csrf_token"]
    return body


def idem() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}
