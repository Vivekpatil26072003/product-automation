"""Diary automation end to end: photo -> AI-read orders (several customers on one page) -> review -> saved
orders and customers -> automatic consolidated PDF -> emailed to the owner by the worker -> status and history.

The Claude reader is replaced by a stand-in whose answer goes through the real checks (app.orders.ai.check), and
EmailJS by a recorded-response HTTP transport (tests.integration.test_orders.FakeEmailJs). Live AI reading and a
real mailbox need real keys; see docs/runbooks/diary-automation.md for that acceptance test.
"""

import io

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.core.config import get_settings
from app.db import tables as t
from app.orders import ai
from app.orders import fields as of
from tests.conftest import idem, sign_in
from tests.integration.test_orders import (
    FakeEmailJs,
    approve,
    configure_email,
    drafts,
    drain,
    patch,
    upload_note,
)

pytestmark = [pytest.mark.db, pytest.mark.infra]

# A diary page written as a table without borders: what an OCR reader returns line by line.
TABLE_PAGE = ["26/9/26", "Asha Traders  500  25  12500  adv 5000", "Mehta Box  200  30  6000  urgent"]


class FakeOrderReader:
    """Answers like Claude would for TABLE_PAGE; the answer is checked by the real ai.check()."""

    model, prompt_hash = "claude-opus-5-5", "test-hash"

    def read(self, page, image, date_order):
        spans = {s["id"]: s for s in page["spans"]}

        def f(**values):
            out = {
                n: {"value": None, "evidence_ids": [], "uncertain": False, "note": None, "crossed_out": None}
                for n in of.FIELDS
            }
            for name, (value, line) in values.items():
                out[name] |= {"value": value, "evidence_ids": [f"p1-s{line}"]}
            return out

        doc = {
            "page_kind": "orders",
            "languages": ["en"],
            "warnings": [],
            "orders": [
                {
                    "fields": f(
                        customer_name=("Asha Traders", 2),
                        order_date=("26/9/26", 1),
                        quantity=("500", 2),
                        rate=("25", 2),
                        total=("12500", 2),
                        advance=("5000", 2),
                    ),
                    "extra": [],
                },
                {
                    "fields": f(
                        customer_name=("Mehta Box", 3),
                        order_date=("26/9/26", 1),
                        quantity=("200", 3),
                        rate=("30", 3),
                        total=("6000", 3),
                        priority=("urgent", 3),
                    ),
                    "extra": [],
                },
            ],
        }
        return ai.OrderReading("orders", ai.check(doc, spans), [], ["en"], self.model, self.prompt_hash)


@pytest.fixture
def ai_reader(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_provider", "claude")
    monkeypatch.setattr("app.orders.ai.ClaudeOrderReader", FakeOrderReader)


def reports(client, batch_id) -> list[dict]:
    r = client.get(f"/api/v1/batches/{batch_id}/reports")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def stages(client, batch_id) -> dict[str, dict]:
    return {s["key"]: s for s in client.get(f"/api/v1/batches/{batch_id}/pipeline").json()["data"]["stages"]}


def deliver(client, report_id, resend=False):
    return client.post(f"/api/v1/batch-reports/{report_id}/deliveries", json={"resend": resend}, headers=idem())


def test_settings_are_admin_only_and_never_return_the_private_key(client, seeded, monkeypatch):
    mail = FakeEmailJs(monkeypatch)
    sign_in(client, seeded, "dev-reviewer")
    assert client.get("/api/v1/settings/owner-report").status_code == 403
    sign_in(client, seeded, "dev-admin")
    view = client.get("/api/v1/settings/owner-report").json()["data"]
    assert not view["email_ready"] and "emailjs_private_key" in view["missing"]
    r = client.put("/api/v1/settings/owner-report", headers={**idem(), "If-Match": '"0"'}, json={"auto_send": True})
    assert r.status_code == 422  # cannot switch automation on before email works
    view = configure_email(client, seeded, back_as="dev-admin")
    assert view["email_ready"] and view["emailjs"]["private_key_set"] and "private-test-secret" not in str(view)
    test = client.post("/api/v1/settings/owner-report/test", headers=idem()).json()["data"]
    assert test["outcome"] == "ACCEPTED" and mail.sent[0]["template_params"]["to_email"] == "owner@company.example"
    assert mail.pdf().startswith(b"%PDF")


def test_photo_to_owner_email_automatically(client, seeded, monkeypatch, ai_reader, owner_engine):
    mail = FakeEmailJs(monkeypatch)
    configure_email(client, seeded, auto=True)
    body = upload_note(client, seeded, monkeypatch, lines=TABLE_PAGE, name="diary-page.jpg.png")
    batch_id = body["batch_id"]

    # Right after reading: the diary data as a table (marked "To review") and as a PDF.
    data = client.get(f"/api/v1/batches/{batch_id}/diary-data").json()["data"]
    assert [(r["customer"], r["quantity"], r["total"], r["status"][:9]) for r in data["rows"]] == [
        ("Asha Traders", "500", "12,500.00", "To review"),
        ("Mehta Box", "200", "6,000.00", "To review"),
    ]
    assert not data["reviewed"]
    draft_pdf = client.get(f"/api/v1/batches/{batch_id}/diary-data/pdf")
    assert draft_pdf.status_code == 200 and draft_pdf.headers["content-type"] == "application/pdf"
    draft_text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(draft_pdf.content)).pages)
    assert "DRAFT" in draft_text and "Asha Traders" in draft_text and "Mehta Box" in draft_text

    first, second = drafts(client, batch_id)
    assert first["reading"]["reader"] == "claude-orders" and first["reading"]["model"] == "claude-opus-5-5"
    assert [d["fields"]["customer_name"]["value"] for d in (first, second)] == ["Asha Traders", "Mehta Box"]
    assert first["fields"]["total"]["evidence"][0]["text"].startswith("Asha Traders")
    assert any(i["code"] == "UNEVALUATED_MODEL" for i in first["issues"])  # development: model not evaluated yet
    assert stages(client, batch_id)["review"]["state"] == "current"

    assert approve(client, first).status_code == 201
    assert reports(client, batch_id) == []  # one order still waiting: no report yet
    assert approve(client, second).status_code == 201
    [report] = reports(client, batch_id)
    assert report["trigger"] == "AUTO" and report["orders"] == 2 and report["state"] == "QUEUED"
    drain()

    [report] = reports(client, batch_id)
    assert report["state"] == "READY" and report["deliveries"][0]["state"] == "ACCEPTED"
    assert len(mail.sent) == 1
    params = mail.sent[0]["template_params"]
    assert params["to_email"] == "owner@company.example" and "2 order(s)" in params["subject"]
    assert "Total order value: 18,500.00" in params["message"]
    # The data itself travels in the email, under the names an order template uses and as a whole table.
    assert params["order_count"] == "2" and params["total"] == "18,500.00"
    assert params["customer_name"] == "Asha Traders, Mehta Box" and params["title"] == params["subject"]
    assert "Asha Traders" in params["orders_text"] and "Mehta Box" in params["message"]
    assert params["orders_html"].startswith("<table") and "6,000.00" in params["orders_html"]
    assert params["email"] == "owner@company.example" and params["name"] == "Test Packaging Co"
    saved = client.get(f"/api/v1/batches/{batch_id}/diary-data").json()["data"]
    assert saved["reviewed"] and {r["status"] for r in saved["rows"]} == {"Saved"}
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(mail.pdf())).pages)
    assert "Asha Traders" in text and "Mehta Box" in text and "18,500.00" in text and "Test Packaging Co" in text
    pdf = client.get(f"/api/v1/batch-reports/{report['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content == mail.pdf()  # the attachment is the stored report

    s = stages(client, batch_id)
    assert [s[k]["state"] for k in ("uploaded", "reading", "review", "saved", "report", "email")] == ["done"] * 6
    customers = client.get("/api/v1/customers").json()["data"]
    assert {c["name"] for c in customers} >= {"Asha Traders", "Mehta Box"}
    with owner_engine.connect() as conn:
        kinds = conn.execute(
            select(t.notification.c.title).where(
                t.notification.c.kind == "REPORT", t.notification.c.tenant_id == seeded.tenant_id
            )
        )
        assert any("emailed to the owner" in k for k in kinds.scalars())
    assert client.get("/api/v1/history?kind=owner_reports").json()["data"][0]["state"] == "ACCEPTED"

    # Never twice by accident: sending again needs an explicit "send again".
    assert deliver(client, report["id"]).json()["error"]["code"] == "ALREADY_SENT"
    assert deliver(client, report["id"], resend=True).status_code == 202
    assert deliver(client, report["id"], resend=True).json()["error"]["code"] == "ALREADY_SENDING"
    drain()
    assert len(mail.sent) == 2 and reports(client, batch_id)[0]["deliveries"][0]["trigger"] == "RESEND"


def test_failed_and_unknown_owner_emails(client, seeded, monkeypatch):
    mail = FakeEmailJs(monkeypatch)
    configure_email(client, seeded, auto=True)
    mail.answers.append((400, "The template ID not found"))
    body = upload_note(client, seeded, monkeypatch)
    assert approve(client, drafts(client, body["batch_id"])[0]).status_code == 201
    drain()
    [report] = reports(client, body["batch_id"])
    failed = report["deliveries"][0]
    assert report["state"] == "READY" and failed["state"] == "FAILED" and "template" in failed["error"]["message"]
    assert stages(client, body["batch_id"])["email"]["state"] == "failed"  # never shown as sent
    assert deliver(client, report["id"]).status_code == 202  # retry keeps the same PDF
    mail.answers.append(httpx.ReadTimeout("no answer"))
    drain()
    unknown = reports(client, body["batch_id"])[0]["deliveries"][0]
    assert unknown["state"] == "UNKNOWN"  # it may have gone out
    sent_before = len(mail.sent)
    drain()
    assert len(mail.sent) == sent_before  # never resent automatically
    r = client.post(
        f"/api/v1/report-deliveries/{unknown['id']}/reconcile",
        headers=idem(),
        json={"outcome": "ACCEPTED", "note": "Found in EmailJS history"},
    )
    assert r.status_code == 200 and r.json()["data"]["deliveries"][0]["state"] == "ACCEPTED"


def test_manual_report_needs_email_settings_and_auto_off_skips(client, seeded, monkeypatch):
    sign_in(client, seeded, "dev-reviewer")
    body = upload_note(client, seeded, monkeypatch)
    assert approve(client, drafts(client, body["batch_id"])[0]).status_code == 201
    assert reports(client, body["batch_id"]) == []  # automatic reports are off by default
    assert stages(client, body["batch_id"])["email"]["state"] == "skipped"
    r = client.post(f"/api/v1/batches/{body['batch_id']}/reports", json={"email_owner": True}, headers=idem())
    assert r.status_code == 409 and r.json()["error"]["code"] == "OWNER_EMAIL_MISSING"
    r = client.post(f"/api/v1/batches/{body['batch_id']}/reports", json={"email_owner": False}, headers=idem())
    assert r.status_code == 201 and r.json()["data"]["trigger"] == "MANUAL"
    drain()
    assert reports(client, body["batch_id"])[0]["state"] == "READY"


def test_same_order_again_updates_instead_of_duplicating(client, seeded, monkeypatch):
    sign_in(client, seeded, "dev-reviewer")
    first = upload_note(client, seeded, monkeypatch, name="page-1.png")
    order = approve(client, drafts(client, first["batch_id"])[0]).json()["data"]
    again = upload_note(
        client,
        seeded,
        monkeypatch,
        lines=[
            "Customer : Asha Traders",
            "Order Date : 26/09/2026",
            "Quantity : 500",
            "Rate : 25",
            "Total : 12500",
            "Delivery Date : 12/10/2026",
        ],
        name="page-2.png",
    )
    [d] = drafts(client, again["batch_id"])
    assert d["matches"][0]["order_id"] == order["id"] and not d["approvable"]
    r = approve(client, d)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_DECISION_REQUIRED"
    d = patch(client, d, decision={"mode": "update", "order_id": order["id"]})
    assert d["approvable"]
    updated = approve(client, d).json()["data"]
    assert updated["id"] == order["id"] and updated["revision"] == 2
    assert updated["values"]["delivery_date"] == "2026-10-12"
    assert updated["revisions"][0]["reason"].startswith("Updated from diary page")
    listed = client.get("/api/v1/orders?q=Asha").json()["data"]
    assert [o["id"] for o in listed] == [order["id"]]  # still one order
