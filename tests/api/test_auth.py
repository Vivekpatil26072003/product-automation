"""Sessions and authorization (FR01, TC01, TC02)."""

import pytest

from app.core.config import get_settings
from tests.conftest import idem, sign_in

pytestmark = pytest.mark.db


def test_no_session_is_401_with_error_envelope(client):
    r = client.get("/api/v1/session")
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "UNAUTHENTICATED" and err["request_id"]
    assert r.headers["X-Request-ID"] == err["request_id"]


def test_dev_login_requires_existing_membership(client, seeded):
    r = client.post("/api/v1/auth/dev-login", json={"subject": "nobody-registered"})
    assert r.status_code == 403  # no public registration


def test_session_reports_server_side_roles_and_grants(client, seeded):
    body = sign_in(client, seeded, "dev-viewer")
    assert body["roles"] == ["VIEWER"]
    assert {d["code"] for d in body["departments"]} == {"TAPELINE", "WARPING"}
    assert body["tenant_id"] == str(seeded.tenant_id)
    cookie = client.cookies.jar
    assert any(c.name == get_settings().session_cookie_name for c in cookie)


def test_mutation_without_csrf_is_rejected(client, seeded):
    sign_in(client, seeded, "dev-admin")
    del client.headers["X-CSRF-Token"]
    r = client.post("/api/v1/masters/departments", json={"code": "QA", "name": "QA"}, headers=idem())
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"


def test_logout_revokes_session(client, seeded):
    sign_in(client, seeded, "dev-viewer")
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/session").status_code == 401


def test_removed_grant_and_role_take_effect_on_next_request(client, seeded):  # TC02
    viewer = sign_in(client, seeded, "dev-viewer")
    viewer_cookies = dict(client.cookies)
    viewer_csrf = viewer["csrf_token"]

    admin = sign_in(client, seeded, "dev-admin")
    uid = str(seeded.users["dev-viewer"])
    current = client.get("/api/v1/users?size=100").json()["data"]
    version = next(u["version"] for u in current if u["id"] == uid)
    r = client.patch(f"/api/v1/users/{uid}", json={"department_ids": [str(seeded.departments["WARPING"])]},
                     headers={"If-Match": f'"{version}"', **idem()})  # fmt: skip
    assert r.status_code == 200, r.text
    assert admin  # admin session unaffected

    client.cookies.clear()
    client.cookies.update(viewer_cookies)
    client.headers["X-CSRF-Token"] = viewer_csrf
    body = client.get("/api/v1/session").json()["data"]
    assert [d["code"] for d in body["departments"]] == ["WARPING"]

    sign_in(client, seeded, "dev-admin")
    r = client.patch(f"/api/v1/users/{uid}", json={"active": False}, headers={"If-Match": f'"{version + 1}"', **idem()})
    assert r.status_code == 200
    client.cookies.clear()
    client.cookies.update(viewer_cookies)
    assert client.get("/api/v1/session").status_code == 401


def test_dev_login_unavailable_when_disabled(client, seeded, monkeypatch):
    monkeypatch.setattr(get_settings(), "dev_auth_enabled", False)
    r = client.post("/api/v1/auth/dev-login", json={"subject": seeded.subject_prefix + "dev-admin"})
    assert r.status_code == 404


def test_sso_callback_rejects_unknown_state(client, seeded):
    r = client.get("/api/v1/auth/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_STATE"
