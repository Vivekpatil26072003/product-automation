"""Master data, users and settings (FR23, TC01, TC43; idempotency and If-Match contracts)."""

import uuid

import pytest

from tests.conftest import idem, sign_in

pytestmark = pytest.mark.db


def test_viewer_sees_only_granted_departments_and_machines(client, seeded):
    sign_in(client, seeded, "dev-viewer")
    depts = client.get("/api/v1/masters/departments").json()["data"]
    assert {d["code"] for d in depts} == {"TAPELINE", "WARPING"}
    machines = client.get("/api/v1/masters/machines").json()["data"]
    assert {m["code"] for m in machines} == {"T-01", "T-02", "T-03", "T-04", "W-01", "W-02"}


def test_admin_sees_all_seven_seeded_departments(client, seeded):
    sign_in(client, seeded, "dev-admin")
    codes = [d["code"] for d in client.get("/api/v1/masters/departments").json()["data"]]
    assert codes == ["TAPELINE", "WARPING", "SULZER_FABRIC", "LAMINATION", "MULTIFILAMENT", "DISPATCH", "PURCHASE"]


def test_non_admin_cannot_write_master_data(client, seeded):
    sign_in(client, seeded, "dev-reviewer")
    r = client.post("/api/v1/masters/departments", json={"code": "QA", "name": "QA"}, headers=idem())
    assert r.status_code == 403


def test_create_is_idempotent_and_key_reuse_is_rejected(client, seeded):
    sign_in(client, seeded, "dev-admin")
    headers = idem()
    first = client.post("/api/v1/masters/departments", json={"code": "qa-lab", "name": "QA Lab"}, headers=headers)
    assert first.status_code == 201, first.text
    assert first.json()["data"]["code"] == "QA-LAB" and first.headers["ETag"] == '"1"'
    replay = client.post("/api/v1/masters/departments", json={"code": "qa-lab", "name": "QA Lab"}, headers=headers)
    assert replay.status_code == 201 and replay.json() == first.json()
    reused = client.post("/api/v1/masters/departments", json={"code": "OTHER", "name": "Other"}, headers=headers)
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    missing = client.post("/api/v1/masters/departments", json={"code": "X1", "name": "X"})
    assert missing.status_code == 400


def test_duplicate_code_conflicts(client, seeded):
    sign_in(client, seeded, "dev-admin")
    r = client.post("/api/v1/masters/departments", json={"code": "TAPELINE", "name": "Again"}, headers=idem())
    assert r.status_code == 409


def test_patch_requires_current_version(client, seeded):
    sign_in(client, seeded, "dev-admin")
    dept = str(seeded.departments["PURCHASE"])
    url = f"/api/v1/masters/departments/{dept}"
    assert client.patch(url, json={"name": "Procurement"}, headers=idem()).status_code == 428
    ok = client.patch(url, json={"name": "Procurement"}, headers={"If-Match": '"1"', **idem()})
    assert ok.status_code == 200 and ok.json()["data"]["version"] == 2 and ok.headers["ETag"] == '"2"'
    stale = client.patch(url, json={"active": False}, headers={"If-Match": '"1"', **idem()})
    assert stale.status_code == 412 and stale.json()["error"]["current_version"] == 2


def test_machine_referenced_by_records_cannot_move_department(client, seeded):
    sign_in(client, seeded, "dev-admin")
    r = client.patch(f"/api/v1/masters/machines/{seeded.machines['T-04']}",
                     json={"department_id": str(seeded.departments["WARPING"])},
                     headers={"If-Match": '"1"', **idem()})  # fmt: skip
    assert r.status_code == 409  # F1 has an approved revision on T-04 in Tapeline


def test_unit_alias_rejects_binary_float_and_conflicting_alias(client, seeded):
    sign_in(client, seeded, "dev-admin")
    r = client.post("/api/v1/masters/unit-aliases", json={"alias": "dm", "unit": "m", "factor": 0.1}, headers=idem())
    assert r.status_code == 422
    r = client.post("/api/v1/masters/unit-aliases", json={"alias": "dm", "unit": "m", "factor": "0.1"}, headers=idem())
    assert r.status_code == 201 and r.json()["data"]["factor"] == "0.1"
    r = client.post("/api/v1/masters/unit-aliases", json={"alias": "DM", "unit": "kg", "factor": "1"}, headers=idem())
    assert r.status_code == 409  # one alias cannot map to two dimensions


def test_other_tenant_ids_are_not_found(client, seeded, owner_engine):  # TC01
    from app.seed.demo import seed_tenant

    with owner_engine.begin() as conn:
        other = seed_tenant(conn, f"Other {uuid.uuid4().hex[:6]}", subject_prefix=uuid.uuid4().hex[:6] + "-")
    sign_in(client, seeded, "dev-admin")
    r = client.patch(f"/api/v1/masters/departments/{other.departments['TAPELINE']}", json={"name": "Hijack"},
                     headers={"If-Match": '"1"', **idem()})  # fmt: skip
    assert r.status_code == 404
    r = client.post(
        "/api/v1/users",
        json={
            "subject": "x-" + uuid.uuid4().hex,
            "roles": ["VIEWER"],
            "department_ids": [str(other.departments["TAPELINE"])],
        },
        headers=idem(),
    )
    assert r.status_code == 422  # cannot grant another tenant's department


def test_last_active_admin_cannot_be_removed(client, seeded):  # TC43
    sign_in(client, seeded, "dev-admin")
    uid = str(seeded.users["dev-admin"])
    r = client.patch(f"/api/v1/users/{uid}", json={"active": False}, headers={"If-Match": '"1"', **idem()})
    assert r.status_code == 409 and r.json()["error"]["code"] == "LAST_ADMIN"
    r = client.patch(f"/api/v1/users/{uid}", json={"roles": ["VIEWER"]}, headers={"If-Match": '"1"', **idem()})
    assert r.status_code == 409


def test_users_pagination_is_stable(client, seeded):
    sign_in(client, seeded, "dev-admin")
    for i in range(26):
        assert client.post("/api/v1/users", json={"subject": f"{seeded.subject_prefix}bulk-{i}", "roles": ["VIEWER"]},
                           headers=idem()).status_code == 201  # fmt: skip
    first = client.get("/api/v1/users?size=25").json()
    second = client.get(f"/api/v1/users?size=25&cursor={first['next_cursor']}").json()
    ids = [u["id"] for u in first["data"] + second["data"]]
    assert len(ids) == len(set(ids)) == first["total"] == 31 and second["next_cursor"] is None
    assert client.get("/api/v1/users?size=30").status_code == 400
    assert client.get("/api/v1/users?cursor=garbage").status_code == 400


def test_settings_validation_and_versioning(client, seeded):
    sign_in(client, seeded, "dev-admin")
    view = client.get("/api/v1/settings")
    assert view.json()["data"]["timezone"] == "Asia/Kolkata"
    bad = client.patch("/api/v1/settings", json={"timezone": "Mars/Olympus"}, headers={"If-Match": '"1"', **idem()})
    assert bad.status_code == 422
    ok = client.patch("/api/v1/settings", json={"submission_cutoff_local_time": "17:30", "daily_spend_limit": "500.00"},
                      headers={"If-Match": '"1"', **idem()})  # fmt: skip
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert data["submission_cutoff_local_time"] == "17:30" and data["version"] == 2
    sign_in(client, seeded, "dev-reviewer")
    assert client.get("/api/v1/settings").status_code == 403


def test_every_mutation_is_audited(client, seeded, owner_engine):
    from sqlalchemy import select

    from app.db import tables as t

    sign_in(client, seeded, "dev-admin")
    r = client.post("/api/v1/masters/operators", json={"name": "Anita"}, headers=idem())
    oid = uuid.UUID(r.json()["data"]["id"])
    with owner_engine.connect() as conn:
        rows = conn.execute(select(t.audit_event).where(t.audit_event.c.object_id == oid)).all()
    assert len(rows) == 1 and rows[0].action == "MASTER_CREATED" and rows[0].actor_id == seeded.users["dev-admin"]
