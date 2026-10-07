"""M8 hardening on the real stack: retention (FR25, TC48), no-leak logging (TC49), send pause and restore
reconciliation (FR29), monitoring, ROI (A10), and end-to-end consistency across every output (FR30, TC56/TC57).
"""

import hashlib
import io
import logging
import uuid

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import select, text, update

from app.core.config import get_settings
from app.db import tables as t
from app.db.engine import tenant_tx
from app.ops import restore, retention
from app.storage.objects import get_storage, object_key_for
from tests import filegen
from tests.api.test_insights import add_record
from tests.conftest import idem, sign_in
from tests.fake_providers import SPREADSHEET_ID, FakeProviders, service_account_json
from tests.integration.test_ingestion_pipeline import complete_all, start_batch
from tests.integration.test_reports_email import F1, connect_mail, new_report
from workers import dispatcher
from workers.runtime import run_one

pytestmark = [pytest.mark.db, pytest.mark.infra]
KINDS = [
    "upload.scan",
    "upload.parse",
    "upload.extract",
    "report.render",
    "reports.invalidate",
    "export.render",
    "email.send",
    "integration.test",
    "integrations.fanout",
    "sheets.sync",
    "notification.email",
]


@pytest.fixture
def fake(owner_engine):
    from app import integrations

    with owner_engine.begin() as conn:
        conn.execute(
            update(t.job)
            .where(t.job.c.kind.in_(KINDS), t.job.c.state.in_(("QUEUED", "RETRY_WAIT")))
            .values(state="CANCELLED")
        )
    f = FakeProviders()
    integrations.set_transport(f.transport)
    yield f
    integrations.set_transport(None)


def drain(owner_engine=None, tenant_id=None) -> None:
    for _ in range(6):
        dispatcher.dispatch_batch()
        while run_one(KINDS, "test-worker"):
            pass
        if owner_engine is None:
            return
        with owner_engine.begin() as conn:
            later = conn.execute(
                update(t.job)
                .where(
                    t.job.c.tenant_id == tenant_id,
                    t.job.c.kind.in_(KINDS),
                    t.job.c.state.in_(("QUEUED", "RETRY_WAIT")),
                    t.job.c.next_attempt_at > text("now()"),
                )
                .values(next_attempt_at=text("now()"))
                .returning(t.job.c.id)
            ).all()
        if not later:
            return


def age(owner_engine, table, row_id, days: int) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table.name} DISABLE TRIGGER USER"))
        conn.execute(
            update(table).where(table.c.id == row_id).values(created_at=text(f"now() - interval '{days} days'"))
        )
        conn.execute(text(f"ALTER TABLE {table.name} ENABLE TRIGGER USER"))


def ready_upload(client, seeded) -> dict:
    sign_in(client, seeded, "dev-reviewer")
    files = {"note.pdf": filegen.pdf(text_pages=1)}
    body = start_batch(client, seeded, files)
    complete_all(client, body, files)
    drain()
    return body


# --- retention (FR25, TC48) ---------------------------------------------------------------------------------


def test_retention_purges_eligible_bytes_respects_holds_and_keeps_records(client, seeded, fake, owner_engine):
    body = ready_upload(client, seeded)
    upload_id = uuid.UUID(body["uploads"][0]["id"])
    original = object_key_for("originals", seeded.tenant_id, upload_id)
    derived = get_storage().list_keys(f"derived/{seeded.tenant_id}/{upload_id}")
    assert get_storage().head(original) is not None and derived
    age(owner_engine, t.upload, upload_id, 200)  # older than the 180-day source retention

    sign_in(client, seeded, "dev-admin")
    hold = client.post(
        "/api/v1/retention/holds",
        headers=idem(),
        json={"object_type": "batch", "object_id": body["batch_id"], "reason": "Quality investigation"},
    )
    assert hold.status_code == 201, hold.text
    dry = client.post("/api/v1/retention/run", headers=idem(), json={"dry_run": True}).json()["data"]
    assert dry["eligible"]["SOURCE"] == {"eligible": 0, "held": 1} and dry["purged"] == 0
    held_run = client.post("/api/v1/retention/run", headers=idem(), json={"dry_run": False}).json()["data"]
    assert held_run["held"] == 1 and get_storage().head(original) is not None  # the hold wins

    client.post(f"/api/v1/retention/holds/{hold.json()['data']['id']}/release", headers=idem())
    run = client.post("/api/v1/retention/run", headers=idem(), json={"dry_run": False}).json()["data"]
    assert run["purged"] >= 1 and run["failed"] == 0
    assert (
        get_storage().head(original) is None
        and get_storage().list_keys(f"derived/{seeded.tenant_id}/{upload_id}") == []
    )
    sign_in(client, seeded, "dev-reviewer")
    gone = client.get(f"/api/v1/sources/{upload_id}/file")
    assert gone.status_code == 410 and gone.json()["error"]["code"] == "SOURCE_PURGED"
    with tenant_tx(seeded.tenant_id) as conn:  # metadata and approved records are untouched
        up = conn.execute(select(t.upload).where(t.upload.c.id == upload_id)).one()
        records = conn.execute(select(t.production_record.c.id)).all()
    assert up.declared_sha256 is not None and up.source_purged_at is not None and len(records) == len(seeded.records)
    again = client.post("/api/v1/retention/run", headers=idem(), json={"dry_run": False})
    assert again.status_code == 403  # administrators only


def test_report_file_retention_and_restore_does_not_resurrect(client, seeded, fake, owner_engine, monkeypatch):
    sign_in(client, seeded, "dev-reviewer")
    report = new_report(client)
    rid = uuid.UUID(report["id"])
    age(owner_engine, t.report, rid, 400)  # older than the 365-day business retention
    key = object_key_for("reports", seeded.tenant_id, rid, ".pdf")

    storage = get_storage()
    real_delete = type(storage).delete
    monkeypatch.setattr(type(storage), "delete", lambda self, k: (_ for _ in ()).throw(OSError("storage down")))
    failed = retention.run(seeded.tenant_id, dry_run=False)
    assert failed["failed"] >= 1 and storage.head(key) is not None  # nothing marked; retried next time
    monkeypatch.setattr(type(storage), "delete", real_delete)
    ok = retention.run(seeded.tenant_id, dry_run=False)
    assert ok["purged"] >= 1 and storage.head(key) is None
    r = client.get(f"/api/v1/reports/{rid}/file")
    assert r.status_code == 410 and r.json()["error"]["code"] == "REPORT_FILE_PURGED"
    assert client.get(f"/api/v1/reports/{rid}").json()["data"]["metrics"]["metrics"][0]["production_qty"] == "4830.000"

    storage.put_bytes(key, b"%PDF restored from an old backup", "application/pdf")  # a restore brings it back
    with owner_engine.begin() as conn:
        assert retention.replay_manifest(conn) >= 1
    assert storage.head(key) is None  # the deletion manifest removed it again


# --- sends pause and restore verification (FR29) ------------------------------------------------------------


def test_paused_sends_stay_queued_then_go_once(client, seeded, fake, owner_engine, monkeypatch):
    connect_mail(client, seeded)
    sign_in(client, seeded, "dev-sender")
    report = new_report(client)
    d = client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": report["id"]}).json()["data"]
    d = client.patch(
        f"/api/v1/email-drafts/{d['id']}",
        headers={**idem(), "If-Match": f'"{d["version"]}"'},
        json={"to": ["plant.manager@contoso.example"]},
    ).json()["data"]
    monkeypatch.setattr(get_settings(), "sends_paused", True)
    email_id = client.post(
        f"/api/v1/email-drafts/{d['id']}/send",
        headers={**idem(), "If-Match": f'"{d["version"]}"'},
        json={"version": d["version"], "confirmed_hash": d["content_hash"], "confirmation": True},
    ).json()["data"]["email_id"]
    drain(owner_engine, seeded.tenant_id)
    assert client.get(f"/api/v1/emails/{email_id}").json()["data"]["state"] == "QUEUED" and fake.sent == []
    verified = restore.verify(owner_engine)
    assert verified["sends_to_reconcile"].get("QUEUED", 0) >= 1  # the drill lists what to reconcile first
    monkeypatch.setattr(get_settings(), "sends_paused", False)
    drain(owner_engine, seeded.tenant_id)
    assert client.get(f"/api/v1/emails/{email_id}").json()["data"]["state"] == "ACCEPTED" and len(fake.sent) == 1


def test_verify_restore_checks_files_against_checksums(client, seeded, fake, owner_engine):
    sign_in(client, seeded, "dev-reviewer")
    report = new_report(client)
    first = restore.verify(owner_engine)
    assert report["id"] not in first["report_files"]["checksum_mismatch"]
    get_storage().put_bytes(
        object_key_for("reports", seeded.tenant_id, uuid.UUID(report["id"]), ".pdf"), b"tampered", "application/pdf"
    )
    second = restore.verify(owner_engine)
    assert report["id"] in second["report_files"]["checksum_mismatch"] and not second["ok"]


# --- monitoring ---------------------------------------------------------------------------------------------


def test_metrics_need_a_token_and_expose_counts_only(client, seeded, fake, monkeypatch):
    assert client.get("/api/v1/ops/metrics").status_code == 404  # not configured: not exposed
    monkeypatch.setattr(get_settings(), "metrics_token", "scrape-secret-123")
    assert client.get("/api/v1/ops/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.get("/api/v1/ops/metrics", headers={"Authorization": "Bearer scrape-secret-123"})
    assert r.status_code == 200 and "prodauto_jobs_oldest_due_seconds" in r.text and "prodauto_sends_paused" in r.text
    assert str(seeded.tenant_id) not in r.text
    sign_in(client, seeded, "dev-reviewer")
    assert client.get("/api/v1/ops/status").status_code == 403
    sign_in(client, seeded, "dev-admin")
    status = client.get("/api/v1/ops/status").json()["data"]
    assert {"queue", "company", "alerts", "retention"} <= set(status)


def test_unknown_email_raises_an_alert(client, seeded, fake, owner_engine):
    connect_mail(client, seeded)
    sign_in(client, seeded, "dev-sender")
    report = new_report(client)
    d = client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": report["id"]}).json()["data"]
    d = client.patch(
        f"/api/v1/email-drafts/{d['id']}",
        headers={**idem(), "If-Match": f'"{d["version"]}"'},
        json={"to": ["a@contoso.example"]},
    ).json()["data"]
    fake.mail_mode = "timeout_after_accept"
    client.post(
        f"/api/v1/email-drafts/{d['id']}/send",
        headers={**idem(), "If-Match": f'"{d["version"]}"'},
        json={"version": d["version"], "confirmed_hash": d["content_hash"], "confirmation": True},
    )
    drain(owner_engine, seeded.tenant_id)
    sign_in(client, seeded, "dev-admin")
    alerts = {a["code"] for a in client.get("/api/v1/ops/status").json()["data"]["alerts"]}
    assert "EMAIL_UNKNOWN" in alerts


# --- no leakage (TC49) --------------------------------------------------------------------------------------


def test_logs_and_errors_never_carry_secrets_documents_or_signed_urls(client, seeded, fake, caplog):
    canary_secret = "canary-secret-" + uuid.uuid4().hex
    canary_text = "CANARY-NOTE-" + uuid.uuid4().hex
    caplog.set_level(logging.DEBUG)
    sign_in(client, seeded, "dev-admin")
    fake.fail_once("POST", r"/oauth2/v2.0/token", httpx.Response(401, json={"error": canary_secret}))
    r = client.post(
        "/api/v1/integrations",
        headers=idem(),
        json={
            "provider": "power_bi",
            "name": "BI",
            "config": {
                "tenant": "11111111-2222-3333-4444-555555555555",
                "workspace_id": "11111111-2222-3333-4444-555555555555",
                "dataset_id": "11111111-2222-3333-4444-555555555555",
            },
            "secret": {"client_id": "11111111-2222-3333-4444-555555555555", "client_secret": canary_secret},
        },
    )
    drain()
    listed = client.get("/api/v1/integrations").text
    sign_in(client, seeded, "dev-reviewer")
    files = {"canary.txt": f"Production note {canary_text}\nTapeline 1250 m".encode()}
    body = start_batch(client, seeded, files)
    complete_all(client, body, files)
    drain()
    report = new_report(client)
    link = client.get(f"/api/v1/reports/{report['id']}/file").json()["data"]
    errors = [client.get(f"/api/v1/reports/{uuid.uuid4()}").text, client.post("/api/v1/email-drafts", json={}).text]

    app_logs = "\n".join(rec.getMessage() for rec in caplog.records if not rec.name.startswith(("httpx", "httpx2")))
    for haystack in (app_logs, listed, r.text, *errors):
        assert canary_secret not in haystack
        assert canary_text not in haystack
        assert "X-Amz-Signature" not in haystack
    assert "X-Amz-Signature" in link["url"]  # the link itself was produced, just never logged


# --- ROI (A10) ----------------------------------------------------------------------------------------------


def test_roi_needs_a_baseline_and_enough_pilot_data(client, seeded, fake, owner_engine):
    sign_in(client, seeded, "dev-admin")
    first = client.get("/api/v1/roi?date_from=2026-09-01&date_to=2026-09-30").json()["data"]
    assert first["comparison"] is None and any("baseline" in r for r in first["comparison_unavailable"])
    r = client.put(
        "/api/v1/roi/baseline",
        headers={**idem(), "If-Match": '"0"'},
        json={
            "measured_from": "2026-08-01",
            "measured_to": "2026-08-07",
            "manual_minutes_per_report": 45,
            "manual_minutes_per_entry": 3,
            "notes": "Measured by the production office over one week",
        },
    )
    assert r.status_code == 200 and r.headers["ETag"] == '"1"'
    assert (
        client.put(
            "/api/v1/roi/baseline", headers={**idem(), "If-Match": '"0"'}, json={"manual_minutes_per_report": 1}
        ).status_code
        == 412
    )
    with owner_engine.begin() as conn:  # twelve reports that each took two minutes to produce
        for _ in range(12):
            conn.execute(
                text(
                    "INSERT INTO report (tenant_id, series_id, version, title, filter_json, date_from, date_to,"
                    " department_ids,"
                    " timezone, data_version, record_count, include_detail, is_empty, metrics_json, facts_json,"
                    " template_version, state, file_key, sha256, created_by, created_at, ready_at) VALUES (:t,"
                    " gen_random_uuid(), 1, 'R', '{}', '2026-09-10', '2026-09-10', '{}', 'Asia/Kolkata', 1, 1,"
                    " true, false,"
                    " '{}', '{}', 'x', 'READY', 'k', 's', :u, '2026-09-10 06:00+00', '2026-09-10 06:02+00')"
                ),
                {"t": seeded.tenant_id, "u": seeded.users["dev-reviewer"]},
            )
    data = client.get("/api/v1/roi?date_from=2026-09-01&date_to=2026-09-30").json()["data"]
    assert data["pilot"]["reports_ready"] == 12 and data["pilot"]["report_generation_minutes_median"] == 2.0
    assert data["comparison"]["minutes_saved_per_report"] == 43.0 and data["comparison_unavailable"] == []


# --- end-to-end consistency (FR30, TC56, TC57) --------------------------------------------------------------


def test_every_output_agrees_and_a_correction_flows_through(client, seeded, fake, owner_engine):
    sign_in(client, seeded, "dev-admin")
    sheet = client.post(
        "/api/v1/integrations",
        headers=idem(),
        json={
            "provider": "google_sheets",
            "name": "Sheet",
            "config": {"spreadsheet_id": SPREADSHEET_ID},
            "secret": {"service_account_json": service_account_json()},
        },
    )
    assert sheet.status_code == 201
    connect_mail(client, seeded)
    drain(owner_engine, seeded.tenant_id)

    def sheet_total():
        rows = [r for r in fake.sheet[1:] if r[3] == "2026-09-27" and r[9] == "m" and r[13] == "ACTIVE"]
        return sum(float(r[7]) for r in rows)

    sign_in(client, seeded, "dev-sender")
    dash = client.get("/api/v1/dashboard?date_from=2026-09-27&date_to=2026-09-27&unit=m").json()["data"]
    report = new_report(client)
    link = client.get(f"/api/v1/reports/{report['id']}/file").json()["data"]
    pdf_text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(httpx.get(link["url"]).content)).pages)
    d = client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": report["id"]}).json()["data"]
    d = client.patch(
        f"/api/v1/email-drafts/{d['id']}",
        headers={**idem(), "If-Match": f'"{d["version"]}"'},
        json={"to": ["plant.manager@contoso.example"]},
    ).json()["data"]
    first = client.post(
        f"/api/v1/email-drafts/{d['id']}/send",
        headers={**idem(), "If-Match": f'"{d["version"]}"'},
        json={"version": d["version"], "confirmed_hash": d["content_hash"], "confirmation": True},
    ).json()["data"]["email_id"]
    drain(owner_engine, seeded.tenant_id)

    assert sheet_total() == 4830.0
    assert dash["metrics"][0]["production_qty"] == report["metrics"]["metrics"][0]["production_qty"] == "4830.000"
    assert "4,830" in pdf_text and "80.5%" in pdf_text
    assert "4,830 m against a target of 6,000 m, achieving 80.5%" in fake.sent[0]["body"]["content"]
    assert fake.sent[0]["attachments"][0]["name"] == f"Production_2026-09-27_{report['code']}_v1.pdf"
    history = client.get("/api/v1/history?kind=emails").json()["data"]
    assert history[0]["object_id"] == first and history[0]["state"] == "ACCEPTED"

    # Correction: Tapeline 1250 -> 1300 (TC57). Sheet catches up, old report outdated but unchanged, new version sent.
    sign_in(client, seeded, "dev-reviewer")
    rec = client.get(f"/api/v1/records/{seeded.records[0]}").json()["data"]
    rev = client.post(
        f"/api/v1/records/{seeded.records[0]}/revisions",
        headers={**idem(), "If-Match": f'"{rec["version"]}"'},
        json={"fields": {"production_qty": "1300"}, "reason": "Recount"},
    ).json()["data"]
    client.post(f"/api/v1/records/{seeded.records[0]}/revisions/{rev['revision_id']}/approve", headers=idem())
    drain(owner_engine, seeded.tenant_id)
    assert sheet_total() == 4880.0 and len(fake.sheet) == 1 + len(seeded.records)  # updated in place
    sign_in(client, seeded, "dev-sender")
    old = client.get(f"/api/v1/reports/{report['id']}").json()["data"]
    assert old["outdated"] and old["file"]["sha256"] == report["file"]["sha256"]
    v2 = client.post("/api/v1/reports", headers=idem(), json={"supersedes": report["id"]}).json()["data"]
    drain(owner_engine, seeded.tenant_id)
    v2 = client.get(f"/api/v1/reports/{v2['report_id']}").json()["data"]
    assert (v2["metrics"]["metrics"][0]["production_qty"], v2["metrics"]["metrics"][0]["achievement_pct"]) == (
        "4880.000",
        "81.3",
    )
    fix = client.post(
        "/api/v1/email-drafts", headers=idem(), json={"report_id": v2["id"], "correction_of_email_id": first}
    ).json()["data"]
    fix = client.patch(
        f"/api/v1/email-drafts/{fix['id']}",
        headers={**idem(), "If-Match": f'"{fix["version"]}"'},
        json={"to": ["plant.manager@contoso.example"]},
    ).json()["data"]
    client.post(
        f"/api/v1/email-drafts/{fix['id']}/send",
        headers={**idem(), "If-Match": f'"{fix["version"]}"'},
        json={"version": fix["version"], "confirmed_hash": fix["content_hash"], "confirmation": True},
    )
    drain(owner_engine, seeded.tenant_id)
    assert len(fake.sent) == 2 and "This corrects the report" in fake.sent[1]["body"]["content"]
    assert (
        hashlib.sha256(
            get_storage().get_bytes(
                object_key_for("reports", seeded.tenant_id, uuid.UUID(report["id"]), ".pdf"), 10_000_000
            )
        ).hexdigest()
        == report["file"]["sha256"]
    )
    _ = F1, add_record
