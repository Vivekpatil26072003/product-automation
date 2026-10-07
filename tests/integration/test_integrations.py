"""M5 integrations end to end against imitation providers (FR13, FR15, FR22, A5; TC26-TC31, TC42).

Providers are replaced by tests/fake_providers.py through app.integrations.set_transport, so the real
adapters, job ledger, workers, encryption and API run unchanged.
"""

import uuid

import httpx
import pytest
from sqlalchemy import create_engine, select, text, update

from app import integrations
from app.core.crypto import SecretsUnavailable, decrypt
from app.db import tables as t
from app.db.engine import tenant_tx
from app.integrations.sheets import COLUMNS
from app.outbox import service as outbox
from tests.conftest import idem, sign_in
from tests.fake_providers import GUID, SPREADSHEET_ID, FakeProviders, service_account_json
from workers import dispatcher
from workers.runtime import run_one

pytestmark = pytest.mark.db
KINDS = ["integration.test", "integrations.fanout", "sheets.sync", "erp.sync", "powerbi.refresh"]


@pytest.fixture
def fake(owner_engine):
    # Jobs left by other tests (other tenants) must not run against this test's fake providers.
    with owner_engine.begin() as conn:
        conn.execute(update(t.job).where(t.job.c.kind.in_(KINDS), t.job.c.state.in_(("QUEUED", "RETRY_WAIT")))
                     .values(state="CANCELLED"))  # fmt: skip
    f = FakeProviders()
    integrations.set_transport(f.transport)
    yield f
    integrations.set_transport(None)


def drain(owner_engine=None) -> None:
    while run_one(KINDS, "test-worker"):
        pass


def due_now(owner_engine, tenant_id) -> None:
    """Make delayed/backing-off jobs of this tenant due (instead of sleeping through the backoff)."""
    with owner_engine.begin() as conn:
        conn.execute(update(t.job).where(t.job.c.tenant_id == tenant_id, t.job.c.state.in_(("QUEUED", "RETRY_WAIT")))
                     .values(next_attempt_at=text("now() - interval '1 second'")))  # fmt: skip


def connect_sheets(client, seeded) -> dict:
    sign_in(client, seeded, "dev-admin")
    r = client.post("/api/v1/integrations", headers=idem(), json={
        "provider": "google_sheets", "name": "Production sheet",
        "config": {"spreadsheet_id": SPREADSHEET_ID},
        "secret": {"service_account_json": service_account_json()},
    })  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()["data"]


def conn_row(seeded, cid):
    with tenant_tx(seeded.tenant_id) as conn:
        return conn.execute(select(t.integration_connection).where(t.integration_connection.c.id == cid)).one()


def sync_rows(seeded, cid):
    with tenant_tx(seeded.tenant_id) as conn:
        return conn.execute(select(t.record_sync).where(t.record_sync.c.connection_id == cid)).all()


# --- credentials ----------------------------------------------------------------------------------


def test_credentials_are_write_only_and_encrypted(client, seeded, fake, owner_engine):
    data = connect_sheets(client, seeded)
    assert data["state"] == "NEEDS_TEST" and data["has_secret"] is True
    assert "secret" not in data and "PRIVATE KEY" not in str(data)
    listed = client.get("/api/v1/integrations").json()
    assert "PRIVATE KEY" not in str(listed) and listed["encryption_configured"] is True

    row = conn_row(seeded, uuid.UUID(data["id"]))
    assert b"PRIVATE KEY" not in bytes(row.secret_ciphertext)
    assert "PRIVATE KEY" in decrypt(row.id, row.secret_ciphertext, row.secret_key_id)["service_account"]["private_key"]
    with pytest.raises(SecretsUnavailable):  # bound to its connection: a copied ciphertext does not decrypt
        decrypt(uuid.uuid4(), row.secret_ciphertext, row.secret_key_id)
    with owner_engine.connect() as conn:
        audit = conn.execute(text("SELECT before::text, after::text FROM audit_event WHERE object_id = :i"),
                             {"i": row.id}).all()  # fmt: skip
    assert audit and all("PRIVATE KEY" not in str(a) for a in audit)


def test_invalid_config_and_secret_are_rejected_without_echo(client, seeded, fake):
    sign_in(client, seeded, "dev-admin")
    r = client.post("/api/v1/integrations", headers=idem(), json={
        "provider": "power_bi", "name": "BI", "config": {"tenant": "x", "workspace_id": "nope", "dataset_id": GUID},
        "secret": {"client_id": GUID, "client_secret": "s3cr3t-value!", "extra": "zzz-hidden"},
    })  # fmt: skip
    assert r.status_code == 422
    assert "s3cr3t-value" not in r.text and "zzz-hidden" not in r.text
    fields = {f["field"] for f in r.json()["error"]["fields"]}
    assert "config.workspace_id" in fields and "secret.extra" in fields


def test_only_administrators_manage_integrations(client, seeded, fake):
    sign_in(client, seeded, "dev-reviewer")
    assert client.get("/api/v1/integrations").status_code == 403
    assert client.post("/api/v1/powerbi/refresh", headers=idem()).status_code == 403
    assert client.get("/api/v1/powerbi/status").json()["data"]["state"] == "NOT_CONFIGURED"


# --- Google Sheets ----------------------------------------------------------------------------------


def test_sheets_connect_sync_and_reconcile_is_idempotent(client, seeded, fake):  # TC26
    cid = connect_sheets(client, seeded)["id"]
    drain()
    row = conn_row(seeded, cid)
    assert row.state == "CONNECTED" and row.last_test_ok
    assert not fake.calls("POST", r"batchUpdate|append") or fake.sheet[0] == COLUMNS
    assert fake.sheet[0] == COLUMNS
    body = fake.sheet[1:]
    assert len(body) == len(seeded.records)
    assert {r[0] for r in body} == {str(x) for x in seeded.records}
    assert all(s.state == "SYNCED" and s.synced_revision == 1 for s in sync_rows(seeded, cid))

    records = client.get("/api/v1/records?date_from=2026-09-01&date_to=2026-09-30").json()["data"]
    assert records and all(r["sync_state"] == "SYNCED" for r in records)

    r = client.post(f"/api/v1/integrations/{cid}/reconcile", headers=idem())
    assert r.status_code == 202 and r.json()["result"]["queued_records"] == len(seeded.records)
    drain()
    assert len(fake.sheet) == 1 + len(seeded.records)  # updated in place, nothing appended twice
    assert all(s.state == "SYNCED" for s in sync_rows(seeded, cid))


def test_append_timeout_is_reconciled_without_duplicates(client, seeded, fake, owner_engine):  # TC27
    cid = connect_sheets(client, seeded)["id"]
    fake.timeout_after_append = True  # the rows land in the sheet, but the answer never arrives
    drain()
    job = next(j for j in client.get(f"/api/v1/sync-jobs?connection_id={cid}").json()["data"]
               if j["kind"] == "sheets.sync")  # fmt: skip
    assert job["state"] == "RETRY_WAIT" and job["error"]["code"] == "PROVIDER_TIMEOUT"
    due_now(owner_engine, seeded.tenant_id)
    drain()
    ids = [r[0] for r in fake.sheet[1:]]
    assert len(ids) == len(set(ids)) == len(seeded.records)
    assert all(s.state == "SYNCED" for s in sync_rows(seeded, cid))


def test_rate_limit_backs_off_and_retries(client, seeded, fake, owner_engine):
    cid = connect_sheets(client, seeded)["id"]
    drain()  # connect + first sync
    fake.fail_once("POST", r"batchUpdate", httpx.Response(429, headers={"Retry-After": "7"}))
    client.post(f"/api/v1/integrations/{cid}/reconcile", headers=idem())
    drain()
    jobs = client.get(f"/api/v1/sync-jobs?connection_id={cid}").json()["data"]
    assert jobs[0]["state"] == "RETRY_WAIT" and jobs[0]["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert all(s.state == "PENDING" and s.attempts == 1 for s in sync_rows(seeded, cid))
    due_now(owner_engine, seeded.tenant_id)
    drain()
    assert all(s.state == "SYNCED" for s in sync_rows(seeded, cid))


def test_changed_header_stops_sync_as_conflict(client, seeded, fake):  # TC28
    fake.tabs["Production_Data"] = [["record_id", "rev", "something else"]]
    cid = connect_sheets(client, seeded)["id"]
    drain()
    row = conn_row(seeded, cid)
    assert row.state == "TEST_FAILED" and row.last_error_code == "SCHEMA_MISMATCH"
    assert not fake.calls("POST", r"batchUpdate|append")
    assert client.post(f"/api/v1/integrations/{cid}/reconcile", headers=idem()).status_code == 409


def test_duplicate_key_in_sheet_is_a_conflict_and_nothing_is_written(client, seeded, fake):  # TC28
    cid = connect_sheets(client, seeded)["id"]
    drain()
    dup = list(fake.sheet[1])
    fake.sheet.append(dup)
    writes = len(fake.calls("POST", r"batchUpdate|append"))
    client.post(f"/api/v1/integrations/{cid}/reconcile", headers=idem())
    drain()
    row = conn_row(seeded, cid)
    assert row.state == "CONFLICT" and row.last_error_code == "DUPLICATE_KEY"
    assert len(fake.calls("POST", r"batchUpdate|append")) == writes
    view = client.get("/api/v1/control-tower").json()["data"]["integrations"]["google_sheets"]
    assert view["state"] == "CONFLICT"


def test_newer_revision_in_sheet_is_never_overwritten(client, seeded, fake):
    cid = connect_sheets(client, seeded)["id"]
    drain()
    fake.sheet[1][1] = 9  # someone (or another system) holds a newer revision
    fake.sheet[1][4] = "kept"
    client.post(f"/api/v1/integrations/{cid}/reconcile", headers=idem())
    drain()
    assert fake.sheet[1][1] == 9 and fake.sheet[1][4] == "kept"
    states = {str(s.record_id): s for s in sync_rows(seeded, cid)}
    assert states[str(fake.sheet[1][0])].state == "CONFLICT"
    assert states[str(fake.sheet[1][0])].error_code == "DESTINATION_NEWER"


def test_formula_text_is_written_raw(client, seeded, fake, owner_engine):
    with owner_engine.begin() as conn:
        rev_id = conn.execute(select(t.production_record.c.current_revision_id)
                              .where(t.production_record.c.id == seeded.records[0])).scalar_one()  # fmt: skip
        conn.execute(text("ALTER TABLE record_revision DISABLE TRIGGER USER"))
        conn.execute(update(t.record_revision).where(t.record_revision.c.id == rev_id).values(remarks="=SUM(A1)"))
        conn.execute(text("ALTER TABLE record_revision ENABLE TRIGGER USER"))
    connect_sheets(client, seeded)
    drain()
    row = next(r for r in fake.sheet if r[0] == str(seeded.records[0]))
    assert row[COLUMNS.index("remarks")] == "=SUM(A1)"
    assert all("RAW" in str(r.url) or b'"RAW"' in r.content for r in fake.calls("POST", r"batchUpdate|append"))


def test_record_change_fans_out_once_per_destination(client, seeded, fake, owner_engine):
    connect_sheets(client, seeded)["id"]
    drain()
    rid = seeded.records[0]
    with tenant_tx(seeded.tenant_id) as conn:  # a duplicate event for the same record coalesces
        outbox.enqueue(conn, tenant_id=seeded.tenant_id, event_type="production_record.changed",
                       event_key=f"test:{rid}:{uuid.uuid4()}", payload={"record_id": str(rid)})  # fmt: skip
    sign_in(client, seeded, "dev-reviewer")
    r = client.post(f"/api/v1/records/{rid}/archive", headers=idem(), json={"reason": "Entered twice"})
    assert r.status_code == 200, r.text
    while dispatcher.dispatch_batch():
        pass
    with tenant_tx(seeded.tenant_id) as conn:
        fanouts = conn.execute(select(t.job).where(t.job.c.kind == "integrations.fanout", t.job.c.object_id == rid,
                                                   t.job.c.state == "QUEUED")).all()  # fmt: skip
    assert len(fanouts) == 1
    drain()
    row = next(r for r in fake.sheet if r[0] == str(rid))
    assert row[COLUMNS.index("record_state")] == "ARCHIVED"
    assert len(fake.sheet) == 1 + len(seeded.records)


def test_disconnect_erases_credentials_and_stops_calls(client, seeded, fake, owner_engine):  # TC42
    data = connect_sheets(client, seeded)
    cid = data["id"]
    before = len(fake.requests)
    r = client.delete(f"/api/v1/integrations/{cid}", headers={**idem(), "If-Match": f'"{data["version"]}"'})
    assert r.status_code == 200 and r.json()["data"]["state"] == "DISCONNECTED"
    row = conn_row(seeded, cid)
    assert row.secret_ciphertext is None and row.secret_key_id is None
    with tenant_tx(seeded.tenant_id) as conn:
        states = conn.execute(select(t.job.c.state).where(t.job.c.object_id == row.id)).scalars().all()
    assert states and set(states) == {"CANCELLED"}
    drain()
    assert len(fake.requests) == before  # nothing is sent after disconnecting
    listed = client.get("/api/v1/integrations").json()
    assert listed["data"] == [] and listed["disconnected"][0]["id"] == cid
    assert connect_sheets(client, seeded)["state"] == "NEEDS_TEST"  # a new connection can be made


def test_revoked_credentials_require_reconnect(client, seeded, fake, owner_engine):
    cid = connect_sheets(client, seeded)["id"]
    drain()
    fake.fail_once("GET", r"/values/", httpx.Response(403))
    client.post(f"/api/v1/integrations/{cid}/reconcile", headers=idem())
    drain()
    assert conn_row(seeded, cid).state == "RECONNECT_REQUIRED"


def test_edit_requires_current_version_and_retests(client, seeded, fake):
    data = connect_sheets(client, seeded)
    drain()
    cid = data["id"]
    current = client.get(f"/api/v1/integrations/{cid}")
    r = client.patch(f"/api/v1/integrations/{cid}", headers={**idem(), "If-Match": '"99"'}, json={"name": "x"})
    assert r.status_code == 412
    r = client.patch(f"/api/v1/integrations/{cid}", headers={**idem(), "If-Match": current.headers["ETag"]},
                     json={"config": {"tab": "Other"}})  # fmt: skip
    assert r.status_code == 200 and r.json()["data"]["state"] == "NEEDS_TEST"
    drain()
    row = conn_row(seeded, cid)
    assert row.state == "TEST_FAILED" and row.last_error_code == "TAB_NOT_FOUND"


# --- Power BI ------------------------------------------------------------------------------------------


def connect_powerbi(client, seeded, **config) -> str:
    sign_in(client, seeded, "dev-admin")
    r = client.post("/api/v1/integrations", headers=idem(), json={
        "provider": "power_bi", "name": "Power BI",
        "config": {"tenant": GUID, "workspace_id": GUID, "dataset_id": GUID, **config},
        "secret": {"client_id": GUID, "client_secret": "client-secret-value"},
    })  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def test_powerbi_refresh_freshness_and_coalescing(client, seeded, fake, owner_engine):  # TC30, TC31
    connect_powerbi(client, seeded)
    drain()  # test passes -> first refresh requested
    assert len(fake.refreshes) == 1
    status = client.get("/api/v1/powerbi/status").json()["data"]
    assert status["state"] == "REFRESHING" and status["stale"] is True

    fake.refresh_status = "Completed"
    due_now(owner_engine, seeded.tenant_id)
    drain()
    status = client.get("/api/v1/powerbi/status").json()["data"]
    assert status["state"] == "FRESH" and status["refreshed_data_version"] == status["data_version"]

    with owner_engine.begin() as conn:  # new approved data arrives
        conn.execute(update(t.tenant).where(t.tenant.c.id == seeded.tenant_id)
                     .values(data_version=t.tenant.c.data_version + 1))  # fmt: skip
    assert client.get("/api/v1/powerbi/status").json()["data"]["state"] == "STALE"
    for _ in range(3):  # repeated requests inside the minimum interval coalesce into one deferred job
        assert client.post("/api/v1/powerbi/refresh", headers=idem()).status_code == 202
        drain()
    assert len(fake.refreshes) == 1
    with tenant_tx(seeded.tenant_id) as conn:
        waiting = conn.execute(select(t.job).where(t.job.c.kind == "powerbi.refresh", t.job.c.state == "QUEUED")).all()
    assert len(waiting) == 1
    dash = client.get("/api/v1/dashboard").json()["data"]["power_bi"]
    assert dash["state"] == "STALE"


def test_powerbi_failed_refresh_is_reported(client, seeded, fake, owner_engine):
    connect_powerbi(client, seeded)
    drain()
    fake.refresh_status = "Failed"
    due_now(owner_engine, seeded.tenant_id)
    drain()
    status = client.get("/api/v1/powerbi/status").json()["data"]
    assert status["state"] == "FAILED" and status["last_request"]["error_code"] == "REFRESH_FAILED"


def test_bi_login_sees_only_its_company(owner_engine, seeded):
    from app.integrations import bi_access
    from app.seed.demo import seed_tenant

    with owner_engine.begin() as conn:
        other = seed_tenant(conn, f"Other {uuid.uuid4().hex[:6]}", subject_prefix=uuid.uuid4().hex[:8] + "-")
        role = f"bi_t{uuid.uuid4().hex[:8]}"
        assert bi_access.grant(conn, role, "a-long-test-password-123", seeded.tenant_id)
    url = owner_engine.url.set(username=role, password="a-long-test-password-123")
    bi = create_engine(url)
    try:
        with bi.connect() as conn:
            tenants = set(conn.execute(text("SELECT DISTINCT tenant_id FROM approved_production_v")).scalars())
            assert tenants == {seeded.tenant_id} and other.tenant_id not in tenants
            assert conn.execute(text("SELECT count(*) FROM approved_production_v")).scalar() == len(seeded.records)
            assert conn.execute(text("SELECT tenant_id FROM production_watermark_v")).scalars().all() == [
                seeded.tenant_id]  # fmt: skip
        with bi.connect() as conn, pytest.raises(Exception, match="permission denied"):
            conn.execute(text("SELECT * FROM production_record"))
    finally:
        bi.dispose()
        with owner_engine.begin() as conn:
            conn.execute(text("DELETE FROM bi_access WHERE role_name = :r"), {"r": role})
            conn.execute(text(f'REVOKE ALL ON approved_production_v, production_watermark_v FROM "{role}"'))
            conn.execute(text(f'REVOKE USAGE ON SCHEMA public FROM "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))


# --- Microsoft Graph mail ---------------------------------------------------------------------------------


def test_graph_mail_test_checks_mail_send_without_sending(client, seeded, fake):
    sign_in(client, seeded, "dev-admin")
    fake.graph_roles = ["User.Read.All"]
    r = client.post("/api/v1/integrations", headers=idem(), json={
        "provider": "ms_graph_mail", "name": "Mail",
        "config": {"tenant": "contoso.onmicrosoft.com", "sender_mailbox": "reports@contoso.example"},
        "secret": {"client_id": GUID, "client_secret": "client-secret-value"},
    })  # fmt: skip
    cid = r.json()["data"]["id"]
    drain()
    assert conn_row(seeded, cid).last_error_code == "PERMISSION_MISSING"
    fake.graph_roles = ["Mail.Send"]
    client.post(f"/api/v1/integrations/{cid}/test", headers=idem())
    drain()
    assert conn_row(seeded, cid).state == "CONNECTED"
    assert not [x for x in fake.requests if x.url.host == "graph.microsoft.com"]  # nothing sent


# --- ERP ------------------------------------------------------------------------------------------------------


def test_erp_mock_requires_feature_flag_and_logs_attempts(client, seeded, fake):
    sign_in(client, seeded, "dev-admin")
    body = {"provider": "erp", "name": "ERP (mock)", "config": {"adapter": "mock"}}
    assert client.post("/api/v1/integrations", headers=idem(), json=body).status_code == 409
    s = client.get("/api/v1/settings")
    flags = s.json()["data"]["feature_flags"] | {"erp_integration": True}
    assert client.patch("/api/v1/settings", headers={**idem(), "If-Match": s.headers["ETag"]},
                        json={"feature_flags": flags}).status_code == 200  # fmt: skip
    bad = client.post(
        "/api/v1/integrations",
        headers=idem(),
        json=body | {"config": {"adapter": "mock", "direction": "BIDIRECTIONAL"}},
    )
    cid = bad.json()["data"]["id"]
    drain()
    assert conn_row(seeded, cid).last_error_code == "DIRECTION_NOT_SUPPORTED"
    etag = client.get(f"/api/v1/integrations/{cid}").headers["ETag"]
    client.patch(f"/api/v1/integrations/{cid}", headers={**idem(), "If-Match": etag},
                 json={"config": {"direction": "OUTBOUND"}})  # fmt: skip
    drain()
    view = client.get(f"/api/v1/integrations/{cid}").json()["data"]
    assert view["state"] == "CONNECTED" and view["mock"] is True
    assert view["sync"]["SYNCED"] == len(seeded.records)
    with tenant_tx(seeded.tenant_id) as conn:
        attempts = conn.execute(select(t.erp_sync_attempt).where(t.erp_sync_attempt.c.connection_id == cid)).all()
    assert len(attempts) == len(seeded.records)
    assert all(a.outcome == "SUCCEEDED" and a.mapping_version == 1 and a.external_ref.startswith("MOCK-")
               for a in attempts)  # fmt: skip
