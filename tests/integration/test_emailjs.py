"""EmailJS channel (EMAIL_PROVIDER=emailjs): the browser sends, the server records intent, variables and outcome.

The browser's call to EmailJS itself is exercised in the web app; here the server contract is verified:
variables come from the latest saved report and draft, a send can be claimed once, stale or changed data is
refused, outcomes are recorded in the existing email history, and automatic sending is refused.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app.core.config import get_settings
from app.db import tables as t
from app.db.engine import tenant_tx
from app.mail import emailjs
from tests.api.test_insights import add_record
from tests.conftest import idem, sign_in
from tests.integration.test_reports_email import drain, new_report

pytestmark = [pytest.mark.db, pytest.mark.infra]


@pytest.fixture
def emailjs_channel(monkeypatch, owner_engine):
    with owner_engine.begin() as conn:  # other tests' leftovers must not run here
        conn.execute(
            update(t.job)
            .where(
                t.job.c.state.in_(("QUEUED", "RETRY_WAIT")),
                t.job.c.kind.in_(("report.render", "reports.invalidate", "email.send")),
            )
            .values(state="CANCELLED")
        )
    monkeypatch.setattr(get_settings(), "email_provider", "emailjs")


def confirmed_draft(client, seeded, to=("customer.one@example.org",), cc=()):
    sign_in(client, seeded, "dev-sender")
    report = new_report(client)
    d = client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": report["id"]}).json()["data"]
    d = client.patch(
        f"/api/v1/email-drafts/{d['id']}",
        headers={**idem(), "If-Match": f'"{d["version"]}"'},
        json={"to": list(to), "cc": list(cc)},
    ).json()["data"]
    return report, d


def send(client, d):
    return client.post(
        f"/api/v1/email-drafts/{d['id']}/send",
        headers={**idem(), "If-Match": f'"{d["version"]}"'},
        json={"version": d["version"], "confirmed_hash": d["content_hash"], "confirmation": True},
    )


def claim(client, email_id):
    return client.post(f"/api/v1/emails/{email_id}/client-send/claim", headers=idem())


def result(client, email_id, outcome, status=None, text_=None):
    return client.post(
        f"/api/v1/emails/{email_id}/client-send/result",
        headers=idem(),
        json={"outcome": outcome, "provider_status": status, "provider_text": text_},
    )


def test_variables_come_from_the_saved_report_and_draft(client, seeded, emailjs_channel, owner_engine):
    report, d = confirmed_draft(client, seeded, cc=("office@example.org",))
    assert d["channel"] == "emailjs" and d["sendable"], d["blocking"]  # no Microsoft connection needed
    r = send(client, d)
    assert r.status_code == 202 and r.json()["data"]["channel"] == "emailjs"
    email_id = r.json()["data"]["email_id"]
    with tenant_tx(seeded.tenant_id) as conn:  # nothing for the server worker to send
        jobs = conn.execute(select(t.job).where(t.job.c.object_id == uuid.UUID(email_id))).all()
    assert jobs == []

    c = claim(client, email_id)
    assert c.status_code == 200, c.text
    p = c.json()["data"]["template_params"]
    assert set(p) == set(emailjs.VARIABLES) and all(isinstance(v, str) for v in p.values())
    assert p["to_email"] == "customer.one@example.org" and p["cc_email"] == "office@example.org"
    assert (p["production_total"], p["target_total"], p["achievement_pct"], p["variance"], p["unit"]) == (
        "4,830",
        "6,000",
        "80.5%",
        "-1,170",
        "m",
    )
    assert p["record_count"] == "5" and p["report_code"] == report["code"] and p["report_version"] == "1"
    assert p["subject"] == d["subject"] and "4,830 m against a target of 6,000 m" in p["message"]
    assert "Tapeline (m): 1,250 of 1,500 m, 83.3%" in p["department_rows"]
    assert p["status_summary"] == "Running 2 · Completed 1 · Pending 1 · On hold 1"

    again = claim(client, email_id)  # double click / second tab: never a second EmailJS call
    assert again.status_code == 409 and again.json()["error"]["code"] == "ALREADY_CLAIMED"
    done = result(client, email_id, "ACCEPTED", 200, "OK")
    assert done.status_code == 200 and done.json()["data"]["state"] == "ACCEPTED"
    assert done.json()["data"]["channel"] == "emailjs"
    history = client.get("/api/v1/history?kind=emails").json()["data"]
    assert history[0]["object_id"] == email_id and history[0]["state"] == "ACCEPTED"


def test_changed_records_block_the_send_and_corrections_send_new_values(client, seeded, emailjs_channel, owner_engine):
    _, d = confirmed_draft(client, seeded)
    email_id = send(client, d).json()["data"]["email_id"]
    add_record(owner_engine, seeded, day="2026-09-27", qty="50", target="50")  # saved after confirmation
    c = claim(client, email_id)
    assert c.status_code == 409 and c.json()["error"]["code"] == "REPORT_OUTDATED"
    assert client.get(f"/api/v1/emails/{email_id}").json()["data"]["state"] == "FAILED"  # recorded, not sent

    report = client.get("/api/v1/reports").json()["data"][0]
    v2 = client.post("/api/v1/reports", headers=idem(), json={"supersedes": report["id"]}).json()["data"]
    drain()
    d2 = client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": v2["report_id"]}).json()["data"]
    d2 = client.patch(
        f"/api/v1/email-drafts/{d2['id']}",
        headers={**idem(), "If-Match": f'"{d2["version"]}"'},
        json={"to": ["customer.one@example.org"]},
    ).json()["data"]
    p = claim(client, send(client, d2).json()["data"]["email_id"]).json()["data"]["template_params"]
    assert (p["production_total"], p["record_count"], p["report_version"]) == ("4,880", "6", "2")


def test_failures_and_missing_reports_are_recorded_not_retried(client, seeded, emailjs_channel, owner_engine):
    _, d = confirmed_draft(client, seeded)
    email_id = send(client, d).json()["data"]["email_id"]
    claim(client, email_id)
    failed = result(client, email_id, "FAILED", 422, "The recipients address is corrupted").json()["data"]
    assert failed["state"] == "FAILED" and failed["error"]["code"] == "EMAILJS_REJECTED"
    assert failed["attempts"][0]["http_status"] == 422

    _, d2 = confirmed_draft(client, seeded)
    stuck = send(client, d2).json()["data"]["email_id"]
    claim(client, stuck)
    with owner_engine.begin() as conn:  # the browser closed before reporting back
        conn.execute(text("ALTER TABLE email_attempt DISABLE TRIGGER USER"))
        conn.execute(
            update(t.email_attempt)
            .where(t.email_attempt.c.email_id == uuid.UUID(stuck))
            .values(started_at=datetime.now(UTC) - timedelta(minutes=20))
        )
        conn.execute(text("ALTER TABLE email_attempt ENABLE TRIGGER USER"))
    with tenant_tx(seeded.tenant_id) as conn:
        assert emailjs.sweep_stale(conn, datetime.now(UTC)) == 1
    view = client.get(f"/api/v1/emails/{stuck}").json()["data"]
    assert view["state"] == "UNKNOWN"  # reconcile with the EmailJS history; never sent again automatically
    assert client.post(f"/api/v1/emails/{stuck}/client-send/claim", headers=idem()).status_code == 409


def test_graph_emails_cannot_be_claimed_and_auto_send_is_refused(client, seeded, emailjs_channel, monkeypatch):
    monkeypatch.setattr(get_settings(), "email_provider", "graph")
    sign_in(client, seeded, "dev-admin")
    s = client.get("/api/v1/settings")
    flags = s.json()["data"]["feature_flags"] | {"scheduling": True, "auto_send": True}
    client.patch("/api/v1/settings", headers={**idem(), "If-Match": s.headers["ETag"]}, json={"feature_flags": flags})
    monkeypatch.setattr(get_settings(), "email_provider", "emailjs")
    sign_in(client, seeded, "dev-sender")
    sched = client.post(
        "/api/v1/schedules",
        headers=idem(),
        json={"name": "Daily", "cadence": "DAILY", "local_time": "07:00", "to": ["a@example.org"], "mode": "AUTO_SEND"},
    )
    s = sched.json()["data"]
    assert s["auto_send_blocked_reason"] == "BROWSER_CHANNEL"
    r = client.post(
        f"/api/v1/schedules/{s['id']}/approve-auto-send",
        headers=idem(),
        json={"version": 1, "confirmed_policy_hash": s["policy"]["hash"], "confirmation": True},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "BROWSER_CHANNEL"
