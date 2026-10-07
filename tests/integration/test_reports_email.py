"""M6 reports and email on the real stack (FR16-FR21; TC32-TC41, TC57, TC58).

Email goes through the real adapter and worker to an imitation Microsoft Graph (tests/fake_providers.py).
"""

import hashlib
import io
import threading
import uuid

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import select, text, update

from app import integrations
from app.db import tables as t
from app.db.engine import tenant_tx
from app.storage.objects import get_storage
from tests.api.test_insights import add_record
from tests.conftest import idem, sign_in
from tests.fake_providers import GUID, FakeProviders
from workers import dispatcher
from workers.runtime import run_one

pytestmark = [pytest.mark.db, pytest.mark.infra]
KINDS = [
    "report.render",
    "reports.invalidate",
    "export.render",
    "email.send",
    "integration.test",
    "integrations.fanout",
    "sheets.sync",
    "erp.sync",
    "powerbi.refresh",
]
F1 = {"date_from": "2026-09-27", "date_to": "2026-09-27", "units": ["m"]}


@pytest.fixture
def fake(owner_engine):
    with owner_engine.begin() as conn:  # other tests' leftovers must not run against this test's fakes
        conn.execute(
            update(t.job)
            .where(t.job.c.kind.in_(KINDS), t.job.c.state.in_(("QUEUED", "RETRY_WAIT")))
            .values(state="CANCELLED")
        )
    f = FakeProviders()
    integrations.set_transport(f.transport)
    yield f
    integrations.set_transport(None)


def drain() -> None:
    while True:
        dispatched = dispatcher.dispatch_batch()
        ran = False
        while run_one(KINDS, "test-worker"):
            ran = True
        if not dispatched and not ran:
            return


def due_now(owner_engine, tenant_id) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            update(t.job)
            .where(t.job.c.tenant_id == tenant_id, t.job.c.state.in_(("QUEUED", "RETRY_WAIT")))
            .values(next_attempt_at=text("now() - interval '1 second'"))
        )


def new_report(client, filter=F1, **body) -> dict:
    r = client.post("/api/v1/reports", headers=idem(), json={"filter": filter} | body)
    assert r.status_code == 202, r.text
    drain()
    return client.get(f"/api/v1/reports/{r.json()['data']['report_id']}").json()["data"]


def connect_mail(client, seeded) -> None:
    sign_in(client, seeded, "dev-admin")
    r = client.post(
        "/api/v1/integrations",
        headers=idem(),
        json={
            "provider": "ms_graph_mail",
            "name": "Mail",
            "config": {"tenant": "contoso.onmicrosoft.com", "sender_mailbox": "reports@contoso.example"},
            "secret": {"client_id": GUID, "client_secret": "client-secret-value"},
        },
    )
    assert r.status_code == 201, r.text
    drain()


def ready_draft(client, seeded, fake, to=("plant.manager@contoso.example",)) -> tuple[dict, dict]:
    """Mail connected, F1 report READY, draft with recipients. Returns (report, draft)."""
    connect_mail(client, seeded)
    sign_in(client, seeded, "dev-sender")
    report = new_report(client)
    d = client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": report["id"]})
    assert d.status_code == 201, d.text
    draft = d.json()["data"]
    p = client.patch(
        f"/api/v1/email-drafts/{draft['id']}",
        headers={**idem(), "If-Match": f'"{draft["version"]}"'},
        json={"to": list(to)},
    )
    assert p.status_code == 200, p.text
    return report, p.json()["data"]


def send(client, draft, key=None):
    return client.post(
        f"/api/v1/email-drafts/{draft['id']}/send",
        headers={"Idempotency-Key": key or uuid.uuid4().hex, "If-Match": f'"{draft["version"]}"'},
        json={"version": draft["version"], "confirmed_hash": draft["content_hash"], "confirmation": True},
    )


def email(client, email_id) -> dict:
    return client.get(f"/api/v1/emails/{email_id}").json()["data"]


# --- reports -----------------------------------------------------------------------------------------------


def test_report_pdf_excel_and_dashboard_agree(client, seeded, fake):  # TC32
    sign_in(client, seeded, "dev-reviewer")
    report = new_report(client)
    assert report["state"] == "READY" and not report["outdated"] and report["version"] == 1
    m = report["metrics"]["metrics"][0]
    assert (m["production_qty"], m["target_qty"], m["achievement_pct"], m["variance"], report["record_count"]) == (
        "4830.000",
        "6000.000",
        "80.5",
        "-1170.000",
        5,
    )
    dash = client.get("/api/v1/dashboard?date_from=2026-09-27&date_to=2026-09-27&unit=m").json()["data"]
    assert (
        dash["metrics"] == report["metrics"]["metrics"] and dash["status_counts"] == report["metrics"]["status_counts"]
    )

    f = client.get(f"/api/v1/reports/{report['id']}/file").json()["data"]
    downloaded = httpx.get(f["download_url"]).content
    previewed = httpx.get(f["url"]).content
    assert hashlib.sha256(downloaded).hexdigest() == hashlib.sha256(previewed).hexdigest() == f["sha256"]
    pdf_text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(downloaded)).pages)
    assert "4,830" in pdf_text and "6,000" in pdf_text and "80.5%" in pdf_text
    assert f["name"] == f"Production_2026-09-27_{report['code']}_v1.pdf"

    x = client.post("/api/v1/exports", headers=idem(), json={"report_id": report["id"]})
    assert x.status_code == 202, x.text
    drain()
    with tenant_tx(seeded.tenant_id) as conn:
        exp = conn.execute(select(t.export).where(t.export.c.report_id == uuid.UUID(report["id"]))).one()
    assert exp.state == "READY" and exp.row_count == 5 and exp.metrics_json == report["metrics"]
    again = client.post("/api/v1/exports", headers=idem(), json={"report_id": report["id"]}).json()["data"]
    assert again["export_id"] == str(exp.id)  # one Excel snapshot per report


def test_failed_render_leaves_no_file_and_retry_uses_the_snapshot(client, seeded, fake, monkeypatch):  # TC33
    from app.reports import pdf

    sign_in(client, seeded, "dev-reviewer")
    real = pdf.render
    monkeypatch.setattr(pdf, "render", lambda _r: (_ for _ in ()).throw(RuntimeError("renderer crashed")))
    report = new_report(client)
    assert report["state"] == "FAILED" and report["file"] is None
    assert client.get(f"/api/v1/reports/{report['id']}/file").status_code == 409

    arch = client.post(f"/api/v1/records/{seeded.records[0]}/archive", headers=idem(), json={"reason": "Duplicate"})
    assert arch.status_code == 200
    monkeypatch.setattr(pdf, "render", real)
    r = client.post(f"/api/v1/reports/{report['id']}/retry", headers=idem())
    assert r.status_code == 202, r.text
    drain()
    after = client.get(f"/api/v1/reports/{report['id']}").json()["data"]
    assert after["state"] == "READY" and after["record_count"] == 5  # the original snapshot, not live data
    assert after["metrics"]["metrics"][0]["production_qty"] == "4830.000"
    assert after["outdated"] is True  # current values need a new generation


def test_new_record_in_filter_outdates_and_regenerate_adds_it(client, seeded, fake, owner_engine):  # TC35, FR18
    connect_mail(client, seeded)
    sign_in(client, seeded, "dev-sender")
    report = new_report(client)
    add_record(owner_engine, seeded, day="2026-09-27", qty="100", target="100")  # approved later, inside the filter
    assert client.get(f"/api/v1/reports/{report['id']}").json()["data"]["outdated"] is True
    d = client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": report["id"]})
    assert d.status_code == 409 and d.json()["error"]["code"] == "REPORT_OUTDATED"

    r = client.post("/api/v1/reports", headers=idem(), json={"supersedes": report["id"]})
    assert r.status_code == 202, r.text
    drain()
    v2 = client.get(f"/api/v1/reports/{r.json()['data']['report_id']}").json()["data"]
    assert v2["version"] == 2 and v2["series_id"] == report["series_id"] and v2["record_count"] == 6
    assert [x["version"] for x in v2["versions"]] == [2, 1]
    old = client.get(f"/api/v1/reports/{report['id']}").json()["data"]
    assert old["record_count"] == 5 and old["file"]["sha256"] == report["file"]["sha256"]  # never overwritten


def test_empty_period_needs_explicit_confirmation(client, seeded, fake):  # TC58 (manual part)
    sign_in(client, seeded, "dev-reviewer")
    empty = {"date_from": "2026-08-01", "date_to": "2026-08-01"}
    r = client.post("/api/v1/reports", headers=idem(), json={"filter": empty})
    assert r.status_code == 409 and r.json()["error"]["code"] == "EMPTY_PERIOD"
    report = new_report(client, filter=empty, allow_empty=True)
    assert report["state"] == "READY" and report["is_empty"] and report["record_count"] == 0
    assert "achievement is N/A" in report["summary"][0]["text"]


def test_report_snapshot_is_immutable(client, seeded, fake, owner_engine):
    sign_in(client, seeded, "dev-reviewer")
    report = new_report(client)
    with pytest.raises(Exception, match="immutable"), owner_engine.begin() as conn:
        conn.execute(update(t.report).where(t.report.c.id == uuid.UUID(report["id"])).values(record_count=99))
    with pytest.raises(Exception, match="immutable"), owner_engine.begin() as conn:
        conn.execute(update(t.report).where(t.report.c.id == uuid.UUID(report["id"])).values(sha256="0" * 64))


def test_report_access_follows_role_and_grants(client, seeded, fake):  # TC41 (reports)
    sign_in(client, seeded, "dev-reviewer")
    report = new_report(client)
    sign_in(client, seeded, "dev-viewer")
    assert client.get("/api/v1/reports").status_code == 403
    assert client.get(f"/api/v1/reports/{report['id']}").status_code == 403
    sign_in(client, seeded, "dev-uploader")
    assert client.get("/api/v1/history?kind=reports").status_code == 403
    sign_in(client, seeded, "dev-reviewer")
    assert client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": report["id"]}).status_code == 403
    hist = client.get("/api/v1/history?kind=reports").json()["data"]
    assert hist[0]["object_id"] == report["id"] and hist[0]["detail_url"] == f"/reports/{report['id']}"


# --- email -------------------------------------------------------------------------------------------------


def test_draft_validation_rejects_unsafe_input(client, seeded, fake):  # TC36
    _, draft = ready_draft(client, seeded, fake)
    url, match = f"/api/v1/email-drafts/{draft['id']}", {"If-Match": f'"{draft["version"]}"'}
    bad = [
        {"to": ["not-an-address"]},
        {"to": [f"user{i}@contoso.example" for i in range(51)]},
        {"subject": "Report\r\nBcc: attacker@evil.example"},
        {"body": "Hello <script>alert(1)</script>"},
        {"to": ["a@contoso.example"], "cc": ["A@contoso.example"]},
    ]
    for body in bad:
        r = client.patch(url, headers={**idem(), **match}, json=body)
        assert r.status_code == 422, (body, r.text)
    same = client.get(url).json()["data"]
    assert same["version"] == draft["version"] and same["content_hash"] == draft["content_hash"]  # nothing saved
    ok = client.patch(url, headers={**idem(), **match}, json={"cc": ["qa@partner.example"], "subject": "Daily report"})
    assert ok.status_code == 200
    data = client.get(url).json()["data"]
    assert data["cc"][0]["address"] == "qa@partner.example" and data["external_domains"] == ["partner.example"]
    assert fake.sent == []  # saving never sends


def test_stale_confirmation_is_refused(client, seeded, fake):  # TC37
    _, draft = ready_draft(client, seeded, fake)
    confirmed = dict(draft)
    edited = client.patch(
        f"/api/v1/email-drafts/{draft['id']}",
        headers={**idem(), "If-Match": f'"{draft["version"]}"'},
        json={"to": ["someone.else@contoso.example"]},
    ).json()["data"]
    r = client.post(
        f"/api/v1/email-drafts/{draft['id']}/send",
        headers={**idem(), "If-Match": f'"{edited["version"]}"'},
        json={"version": edited["version"], "confirmed_hash": confirmed["content_hash"], "confirmation": True},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_CONFIRMATION"
    drain()
    assert fake.sent == [] and client.get("/api/v1/emails").json()["data"] == []


def test_double_confirmation_sends_once(client, seeded, fake):  # TC38
    _, draft = ready_draft(client, seeded, fake)
    results: list[httpx.Response] = []
    errors: list[BaseException] = []
    key = uuid.uuid4().hex

    def go(k):
        try:
            results.append(send(client, draft, k))
        except BaseException as exc:  # noqa: BLE001 - surface thread failures in the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(key,)) for _ in range(2)]
    threads.append(threading.Thread(target=go, args=(uuid.uuid4().hex,)))  # a third click with a new key
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors, errors
    assert all(r.status_code == 202 for r in results), [r.text for r in results]
    ids = {r.json()["data"]["email_id"] for r in results}
    assert len(ids) == 1
    drain()
    assert len(fake.sent) == 1
    e = email(client, ids.pop())
    assert e["state"] == "ACCEPTED" and len(e["attempts"]) == 1
    assert all(r["state"] == "ACCEPTED" and r["observation_source"] == "provider_response" for r in e["recipients"])
    assert "not confirmed" in e["state_text"]  # accepted is never shown as delivered
    msg = fake.sent[0]
    assert msg["toRecipients"][0]["emailAddress"]["address"] == "plant.manager@contoso.example"
    assert msg["attachments"][0]["name"].endswith("_v1.pdf")


def test_provider_outcomes_and_no_blind_retry(client, seeded, fake, owner_engine):  # TC39
    report, draft = ready_draft(client, seeded, fake)
    fake.mail_mode = "reject"
    e1 = send(client, draft).json()["data"]["email_id"]
    drain()
    assert email(client, e1)["state"] == "FAILED"

    resend = client.post(f"/api/v1/emails/{e1}/resend-draft", headers=idem(), json={"reason": "Fixed recipient list"})
    assert resend.status_code == 201, resend.text
    d2 = resend.json()["data"]
    assert d2["resend_of_email_id"] == e1 and d2["to"] == draft["to"]
    fake.mail_mode = "timeout_after_accept"
    e2 = send(client, d2).json()["data"]["email_id"]
    drain()
    unknown = email(client, e2)
    assert unknown["state"] == "UNKNOWN" and len(fake.sent) == 1  # it did reach the provider
    assert all(r["state"] == "UNKNOWN" for r in unknown["recipients"])
    blocked = client.post(f"/api/v1/emails/{e2}/resend-draft", headers=idem(), json={"reason": "Try again"})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "RECONCILE_REQUIRED"
    d3 = client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": report["id"]}).json()["data"]
    d3 = client.patch(
        f"/api/v1/email-drafts/{d3['id']}",
        headers={**idem(), "If-Match": f'"{d3["version"]}"'},
        json={"to": ["x@contoso.example"]},
    ).json()["data"]
    r = send(client, d3)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECONCILE_REQUIRED"
    due_now(owner_engine, seeded.tenant_id)
    drain()
    assert len(fake.sent) == 1  # nothing retried the unknown send


def test_throttling_before_acceptance_is_retried(client, seeded, fake, owner_engine):
    _, draft = ready_draft(client, seeded, fake)
    fake.mail_mode = "throttle_once"
    email_id = send(client, draft).json()["data"]["email_id"]
    drain()
    assert email(client, email_id)["state"] == "QUEUED"
    due_now(owner_engine, seeded.tenant_id)
    drain()
    e = email(client, email_id)
    assert e["state"] == "ACCEPTED" and [a["outcome"] for a in e["attempts"]] == ["RETRY", "ACCEPTED"]
    assert len(fake.sent) == 1


def test_unknown_is_reconciled_with_evidence(client, seeded, fake):  # TC40
    _, draft = ready_draft(client, seeded, fake)
    fake.mail_mode = "timeout_after_accept"
    email_id = send(client, draft).json()["data"]["email_id"]
    drain()
    bad = client.post(f"/api/v1/emails/{email_id}/reconcile", headers=idem(), json={"outcome": "ACCEPTED"})
    assert bad.status_code == 422  # evidence and reason are mandatory
    r = client.post(
        f"/api/v1/emails/{email_id}/reconcile",
        headers=idem(),
        json={
            "outcome": "ACCEPTED",
            "evidence_ref": "Sent Items 27-09 18:02, message-id <abc@contoso>",
            "reason": "Found in the sender mailbox",
        },
    )
    assert r.status_code == 200, r.text
    e = r.json()["data"]
    assert e["state"] == "ACCEPTED" and e["reconciliation"]["outcome"] == "ACCEPTED"
    assert (
        client.post(
            f"/api/v1/emails/{email_id}/reconcile",
            headers=idem(),
            json={"outcome": "NOT_ACCEPTED", "evidence_ref": "x" * 5, "reason": "change"},
        ).status_code
        == 409
    )
    resend = client.post(f"/api/v1/emails/{email_id}/resend-draft", headers=idem(), json={"reason": "Manager asked"})
    assert resend.status_code == 201 and resend.json()["data"]["resend_of_email_id"] == email_id
    with tenant_tx(seeded.tenant_id) as conn:
        actions = (
            conn.execute(
                select(t.audit_event.c.action)
                .where(t.audit_event.c.object_id == uuid.UUID(email_id))
                .order_by(t.audit_event.c.created_at)
            )
            .scalars()
            .all()
        )
    assert actions == ["EMAIL_SEND_CONFIRMED", "EMAIL_UNKNOWN", "EMAIL_RECONCILED"]


def test_interrupted_send_becomes_unknown_without_resending(client, seeded, fake, owner_engine):
    _, draft = ready_draft(client, seeded, fake)
    email_id = uuid.UUID(send(client, draft).json()["data"]["email_id"])
    with owner_engine.begin() as conn:  # a worker set SENDING and died before recording the answer
        conn.execute(update(t.email_message).where(t.email_message.c.id == email_id).values(state="SENDING"))
    drain()
    assert email(client, email_id)["state"] == "UNKNOWN" and fake.sent == []


def test_report_changed_after_confirmation_blocks_the_send(client, seeded, fake, owner_engine):
    _, draft = ready_draft(client, seeded, fake)
    email_id = send(client, draft).json()["data"]["email_id"]
    add_record(owner_engine, seeded, day="2026-09-27")  # lands before the worker runs
    drain()
    e = email(client, email_id)
    assert e["state"] == "FAILED" and e["error"]["code"] == "REPORT_OUTDATED" and fake.sent == []


def test_send_needs_a_connected_mailbox_and_to_recipient(client, seeded, fake):
    sign_in(client, seeded, "dev-sender")
    report = new_report(client)
    draft = client.post("/api/v1/email-drafts", headers=idem(), json={"report_id": report["id"]}).json()["data"]
    codes = {b["code"] for b in draft["blocking"]}
    assert {"NO_TO", "EMAIL_NOT_CONNECTED"} <= codes and not draft["sendable"]
    r = send(client, draft)
    assert r.status_code in (409, 422) and fake.sent == []


def test_edited_numbers_must_match_the_report(client, seeded, fake):
    _, draft = ready_draft(client, seeded, fake)
    body = draft["body"].replace("4,830 m", "4,930 m")
    d = client.patch(
        f"/api/v1/email-drafts/{draft['id']}",
        headers={**idem(), "If-Match": f'"{draft["version"]}"'},
        json={"body": body},
    ).json()["data"]
    assert any(b["code"] == "UNVERIFIED_NUMBERS" and "4,930 m" in b["message"] for b in d["blocking"])
    r = send(client, d)
    assert r.status_code == 409 and r.json()["error"]["code"] == "UNVERIFIED_NUMBERS"


def test_correction_notice_after_an_accepted_report(client, seeded, fake):  # TC57 (report and email parts)
    report, draft = ready_draft(client, seeded, fake)
    first = send(client, draft).json()["data"]["email_id"]
    drain()
    old_pdf = get_storage()
    sign_in(client, seeded, "dev-reviewer")
    rec = client.get(f"/api/v1/records/{seeded.records[0]}").json()["data"]
    rev = client.post(
        f"/api/v1/records/{seeded.records[0]}/revisions",
        headers={**idem(), "If-Match": f'"{rec["version"]}"'},
        json={"fields": {"production_qty": "1300"}, "reason": "Recount"},
    ).json()["data"]
    assert (
        client.post(
            f"/api/v1/records/{seeded.records[0]}/revisions/{rev['revision_id']}/approve", headers=idem()
        ).status_code
        == 200
    )
    drain()
    sign_in(client, seeded, "dev-sender")
    assert client.get(f"/api/v1/reports/{report['id']}").json()["data"]["outdated"] is True
    r = client.post("/api/v1/reports", headers=idem(), json={"supersedes": report["id"]})
    drain()
    v2 = client.get(f"/api/v1/reports/{r.json()['data']['report_id']}").json()["data"]
    m = v2["metrics"]["metrics"][0]
    assert (m["production_qty"], m["achievement_pct"]) == ("4880.000", "81.3")
    fix = client.post(
        "/api/v1/email-drafts", headers=idem(), json={"report_id": v2["id"], "correction_of_email_id": first}
    )
    assert fix.status_code == 201, fix.text
    assert "This corrects the report sent on" in fix.json()["data"]["body"]
    assert "4,880 m" in fix.json()["data"]["body"]
    v1 = client.get(f"/api/v1/reports/{report['id']}").json()["data"]
    with tenant_tx(seeded.tenant_id) as conn:
        key = conn.execute(select(t.report.c.file_key).where(t.report.c.id == uuid.UUID(report["id"]))).scalar_one()
    assert hashlib.sha256(old_pdf.get_bytes(key, 10_000_000)).hexdigest() == v1["file"]["sha256"]  # old PDF unchanged
    assert email(client, first)["state"] == "ACCEPTED"


def test_history_is_scoped_per_tab(client, seeded, fake):  # TC41
    _, draft = ready_draft(client, seeded, fake)
    send(client, draft)
    drain()
    emails = client.get("/api/v1/history?kind=emails").json()["data"]
    assert emails[0]["state"] == "ACCEPTED" and "not confirmed" in emails[0]["state_text"]
    assert client.get("/api/v1/history?kind=sync").status_code == 403
    sign_in(client, seeded, "dev-admin")
    assert client.get("/api/v1/history?kind=sync").status_code == 200
    assert client.get("/api/v1/history?kind=emails").status_code == 403
