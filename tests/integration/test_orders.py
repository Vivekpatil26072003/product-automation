"""Customer orders end to end through the API and the workers:

image upload -> OCR lines -> order form filled -> reviewer corrections -> approve -> saved order (Record Data)
-> PDF of the saved values -> email queued -> worker sends it through the EmailJS REST API with that exact PDF
-> correction -> new PDF -> send again.
The OCR reader is replaced by a stand-in returning lines in the shape the Azure adapter produces
(tests/unit/test_order_fields.py covers the adapter), and EmailJS by a recorded-response HTTP transport that
keeps every request, so the exact attachment can be checked. Live EmailJS needs real keys (see the runbook).
"""

import base64
import io
import json
import uuid

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import func, select, text, update

from app.db import tables as t
from app.ingestion.ocr import OcrLine, OcrPage
from app.ingestion.scanner import ScannerUnavailable, get_scanner
from app.storage.objects import get_storage
from tests import filegen
from tests.conftest import idem, sign_in
from tests.integration.test_ingestion_pipeline import complete_all, start_batch
from tests.unit.test_order_fields import NOTE
from workers.runtime import run_one

pytestmark = [pytest.mark.db, pytest.mark.infra]
WORK = ["upload.scan", "upload.parse", "upload.extract", "order.email", "batch_report.render", "batch_report.email"]


@pytest.fixture(autouse=True, scope="module")
def _infra():
    try:
        get_storage().ensure_bucket()
        get_scanner().scan(b"ping")
    except (ScannerUnavailable, Exception) as exc:  # noqa: BLE001
        pytest.skip(f"storage or scanner unreachable: {type(exc).__name__}")


def drain() -> None:
    while run_one(WORK, "test-worker"):
        pass


class NoteOcr:
    name = "fake-read"

    def __init__(self, lines, confidence=0.97):
        self.lines, self.confidence = lines, confidence

    def read(self, image_png: bytes) -> OcrPage:
        assert image_png.startswith(b"\x89PNG")
        return OcrPage(self.name, [OcrLine(x, None, self.confidence) for x in self.lines])


class FakeEmailJs:
    """Stands in for api.emailjs.com: records every request, answers from a queue (default 200 "OK")."""

    def __init__(self, monkeypatch):
        self.sent: list[dict] = []
        self.answers: list = []
        monkeypatch.setattr(
            "app.mail.emailjs_api.http_client", lambda: httpx.Client(transport=httpx.MockTransport(self.handle))
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        answer = self.answers.pop(0) if self.answers else (200, "OK")
        if isinstance(answer, Exception):
            raise answer
        self.sent.append(json.loads(request.content))
        return httpx.Response(answer[0], text=answer[1])

    def pdf(self, n: int = -1) -> bytes:
        return base64.b64decode(self.sent[n]["template_params"]["pdf_file"].split(",", 1)[1])


def configure_email(client, seeded, owner="owner@company.example", auto=False, back_as="dev-reviewer") -> dict:
    sign_in(client, seeded, "dev-admin")
    current = client.get("/api/v1/settings/owner-report")
    r = client.put(
        "/api/v1/settings/owner-report",
        headers={**idem(), "If-Match": current.headers["ETag"]},
        json={
            "owner_email": owner,
            "auto_send": auto,
            "company_name": "Test Packaging Co",
            "emailjs_service_id": "service_75drj1q",
            "emailjs_template_id": "template_docs",
            "emailjs_public_key": "public-test",
            "emailjs_private_key": "private-test-secret",
        },
    )
    assert r.status_code == 200, r.text
    sign_in(client, seeded, back_as)
    return r.json()["data"]


def upload_note(client, seeded, monkeypatch, lines=NOTE, name="order-note.png", confidence=0.97) -> dict:
    monkeypatch.setattr("workers.ingestion.get_ocr", lambda: NoteOcr(lines, confidence))
    files = {name: filegen.png(420, 300)}
    body = start_batch(client, seeded, files)
    complete_all(client, body, files)
    drain()
    return body


def drafts(client, batch_id) -> list[dict]:
    r = client.get(f"/api/v1/batches/{batch_id}/order-drafts")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def patch(client, d, fields=None, **more) -> dict:
    r = client.patch(
        f"/api/v1/order-drafts/{d['id']}",
        json={"fields": fields or {}, **more},
        headers={**idem(), "If-Match": f'"{d["version"]}"'},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


def approve(client, d):
    return client.post(f"/api/v1/order-drafts/{d['id']}/approve", headers={**idem(), "If-Match": f'"{d["version"]}"'})


def pdf_text(client, order_id, revision=None) -> tuple[bytes, str]:
    r = client.get(f"/api/v1/orders/{order_id}/pdf" + (f"?revision={revision}" if revision else ""))
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text
    return r.content, PdfReader(io.BytesIO(r.content)).pages[0].extract_text()


def send(client, order_id, to, revision):
    return client.post(f"/api/v1/orders/{order_id}/emails", json={"to_email": to, "revision": revision}, headers=idem())


def emails(client, order_id) -> list[dict]:
    return client.get(f"/api/v1/orders/{order_id}").json()["data"]["emails"]


def test_handwritten_order_to_record_pdf_and_email(client, seeded, monkeypatch, owner_engine):
    mail = FakeEmailJs(monkeypatch)
    sign_in(client, seeded, "dev-reviewer")
    body = upload_note(client, seeded, monkeypatch)

    # TEST 1: the read values are in the order form, each with the source line it came from.
    [d] = drafts(client, body["batch_id"])
    f = d["fields"]
    assert d["source"] == "extracted" and d["state"] == "NEEDS_REVIEW" and d["reading"]["reader"] == "order-labelled-1"
    assert (
        f["customer_name"]["value"],
        f["mobile"]["value"],
        f["order_date"]["value"],
        f["quantity"]["value"],
        f["rate"]["value"],
        f["total"]["value"],
    ) == ("Asha Traders", "9123456780", "2026-09-26", "500", "25", "12500")
    assert f["customer_name"]["evidence"][0]["text"] == "Customer : Asha Traders"
    assert f["quantity"]["confidence"] == pytest.approx(0.97)
    assert f["customer_email"]["value"] is None and d["approvable"]  # partial: missing values stay editable
    cands = client.get(f"/api/v1/batches/{body['batch_id']}/candidates").json()["data"]
    assert cands["candidates"] == [] and cands["extractions"][0]["state"] == "SUCCEEDED"  # not a production entry

    # TEST 2: corrections, then approve -> one saved order with exactly the corrected values.
    d = patch(
        client,
        d,
        {
            "customer_name": "Asha Traders Pvt Ltd",
            "customer_email": "orders@asha.example",
            "quantity": "600",
            "total": "15000",
            "remaining": "10000",
        },
        extra=[{"label": "Printing", "value": "2 colour"}],
    )
    assert d["fields"]["customer_name"]["source"] == "reviewer" and d["fields"]["customer_name"]["evidence"] == []
    assert not [i for i in d["issues"] if i["severity"] == "error"]
    r = approve(client, d)
    assert r.status_code == 201, r.text
    order = r.json()["data"]
    assert approve(client, d).status_code == 412  # a second click cannot create a second order
    listed = client.get("/api/v1/orders").json()["data"]
    assert [o["id"] for o in listed] == [order["id"]]
    v = listed[0]["values"]
    assert (v["customer_name"], v["customer_email"], v["quantity"], v["total"], v["remaining"]) == (
        "Asha Traders Pvt Ltd",
        "orders@asha.example",
        "600",
        "15000",
        "10000",
    )
    assert order["customer"]["name"] == "Asha Traders Pvt Ltd" and order["extra"] == [
        {"label": "Printing", "value": "2 colour"}
    ]
    with owner_engine.connect() as conn:
        saved = conn.execute(
            select(t.order_revision).where(t.order_revision.c.order_id == uuid.UUID(order["id"]))
        ).one()
    assert (saved.customer_name, str(saved.quantity), str(saved.total)) == (
        "Asha Traders Pvt Ltd",
        "600.000",
        "15000.00",
    )
    assert saved.provenance["fields"]["mobile"]["evidence_ids"]  # read values keep their source line
    assert [a["code"] for a in saved.attention] == ["AMBIGUOUS_DATE"]  # kept for the owner report

    # TEST 3: the PDF shows the saved values.
    pdf1, words = pdf_text(client, order["id"])
    assert "Asha Traders Pvt Ltd" in words and "15,000.00" in words and "orders@asha.example" in words

    # Not set up yet: refused at once, nothing queued.
    refused = send(client, order["id"], "buyer@asha.example", 1)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "EMAIL_NOT_CONFIGURED"
    configure_email(client, seeded)
    pdf1, words = pdf_text(client, order["id"])
    assert "Test Packaging Co" in words  # the company name from the owner settings

    # TEST 4: the worker sends exactly this order's latest PDF; the result is recorded.
    s = send(client, order["id"], "buyer@asha.example", 1)
    assert s.status_code == 202 and s.json()["data"]["state"] == "QUEUED", s.text
    assert send(client, order["id"], "buyer@asha.example", 1).json()["error"]["code"] == "ALREADY_SENDING"
    drain()
    assert emails(client, order["id"])[0]["state"] == "ACCEPTED"
    p = mail.sent[0]["template_params"]
    assert mail.sent[0]["service_id"] == "service_75drj1q" and mail.sent[0]["accessToken"] == "private-test-secret"
    assert p["to_email"] == "buyer@asha.example" and p["customer_name"] == "Asha Traders Pvt Ltd"
    assert p["subject"] == f"Your Order/Report - {order['order_ref']}" and order["order_ref"] in p["message"]
    assert mail.pdf() == pdf1 and p["attachment_name"] == f"Order_{order['order_ref']}_r1.pdf"
    assert (p["order_number"], p["quantity"], p["rate"], p["total"], p["order_date"]) == (
        order["order_ref"],
        "600",
        "25.00",
        "15,000.00",
        "26 Sep 2026",
    )
    assert p["email"] == "buyer@asha.example" and "Asha Traders Pvt Ltd" in p["orders_html"]
    assert "Quantity: 600" in p["message"]  # the data is in the email text, not only in the PDF

    # TEST 5: a correction is a new revision; Record Data, PDF and the next email use the latest values.
    r = client.post(
        f"/api/v1/orders/{order['id']}/revisions",
        headers={**idem(), "If-Match": '"1"'},
        json={
            "fields": {"delivery_date": "2026-10-08", "remarks": "Deliver by 8 Oct."},
            "reason": "Customer moved the delivery date",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["revision"] == 2 and r.json()["data"]["revisions"][0]["changed"] == [
        "delivery_date",
        "remarks",
    ]
    listed = client.get("/api/v1/orders").json()["data"]
    assert len(listed) == 1 and listed[0]["values"]["delivery_date"] == "2026-10-08" and listed[0]["revision"] == 2
    pdf2, words = pdf_text(client, order["id"])
    assert "8 Oct 2026" in words and pdf2 != pdf1
    assert pdf_text(client, order["id"], 1)[0] == pdf1  # an earlier revision still renders the same bytes
    stale = send(client, order["id"], "buyer@asha.example", 1)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "ORDER_CHANGED"

    # TEST 7: EmailJS refuses -> recorded as failed with a useful reason, order untouched, a retry works.
    mail.answers.append((400, "The Public Key is invalid"))
    send(client, order["id"], "buyer@asha.example", 2)
    drain()
    failed = emails(client, order["id"])[0]
    assert failed["state"] == "FAILED" and "public key" in failed["error"]["message"].lower()
    send(client, order["id"], "buyer@asha.example", 2)
    drain()
    assert mail.pdf() == pdf2 and mail.sent[-1]["template_params"]["attachment_name"].endswith("_r2.pdf")
    view = client.get(f"/api/v1/orders/{order['id']}").json()["data"]
    assert view["revision"] == 2 and [e["state"] for e in view["emails"]] == ["ACCEPTED", "FAILED", "ACCEPTED"]
    history = client.get("/api/v1/history?kind=order_emails").json()["data"]
    assert [h["state"] for h in history] == ["ACCEPTED", "FAILED", "ACCEPTED"]
    assert client.get("/api/v1/history?kind=orders").json()["data"][0]["state"] == "CORRECTED"


def test_invalid_email_sends_nothing(client, seeded, monkeypatch, owner_engine):  # TEST 6
    sign_in(client, seeded, "dev-reviewer")
    body = upload_note(client, seeded, monkeypatch)
    order = approve(client, drafts(client, body["batch_id"])[0]).json()["data"]
    configure_email(client, seeded)
    for bad in ("not-an-email", "a@b", "one@example.com, two@example.com"):
        r = send(client, order["id"], bad, 1)
        assert r.status_code == 422 and r.json()["error"]["fields"][0]["field"] == "to_email", r.text
    with owner_engine.connect() as conn:
        sends = select(func.count()).where(t.order_email.c.order_id == uuid.UUID(order["id"]))
        assert conn.execute(sends).scalar_one() == 0


def test_unreadable_photo_gets_an_empty_order_form(client, seeded):
    """No OCR reader configured: the photo is kept, and the reviewer types the values beside it."""
    sign_in(client, seeded, "dev-uploader")
    files = {"diary.jpg": filegen.jpeg()}
    body = start_batch(client, seeded, files)
    complete_all(client, body, files)
    drain()
    up = body["uploads"][0]["id"]
    r = client.post(f"/api/v1/uploads/{up}/order-drafts", headers=idem())
    assert r.status_code == 201, r.text
    d = r.json()["data"]
    assert d["source"] == "manual" and not d["approvable"]
    assert {i["field"] for i in d["issues"] if i["code"] == "MISSING_VALUE"} == {
        "customer_name",
        "order_date",
        "quantity",
    }
    d = patch(client, d, {"customer_name": "Asha Traders", "order_date": "2026-09-26", "quantity": "500"})
    assert d["approvable"]
    assert approve(client, d).status_code == 403  # uploaders enter; only reviewers save orders
    page = client.get(f"/api/v1/uploads/{up}/pages/1").json()["data"]
    assert page["image_url"]  # the photo is shown beside the form
    pipeline = client.get(f"/api/v1/batches/{body['batch_id']}/pipeline").json()["data"]
    reading = next(s for s in pipeline["stages"] if s["key"] == "reading")
    assert reading["state"] == "failed" and "OCR" in reading["detail"]  # the real reason, not "done"


def test_low_confidence_values_need_confirmation(client, seeded, monkeypatch):
    sign_in(client, seeded, "dev-reviewer")
    body = upload_note(client, seeded, monkeypatch, confidence=0.55)
    [d] = drafts(client, body["batch_id"])
    blocking = {i["field"] for i in d["issues"] if i["code"] == "CONFIRM_VALUE" and i["severity"] == "error"}
    assert {"customer_name", "quantity", "rate", "total"} <= blocking and not d["approvable"]
    assert approve(client, d).status_code == 422  # never saved on an uncertain reading
    d = patch(client, d, confirm=sorted(blocking))
    assert d["approvable"] and d["fields"]["quantity"]["source"] == "reviewer"
    assert approve(client, d).status_code == 201


def test_scope_roles_and_interrupted_sends(client, seeded, monkeypatch, owner_engine):
    FakeEmailJs(monkeypatch)
    sign_in(client, seeded, "dev-reviewer")
    body = upload_note(client, seeded, monkeypatch)  # Tapeline
    order = approve(client, drafts(client, body["batch_id"])[0]).json()["data"]
    configure_email(client, seeded, back_as="dev-viewer")

    # Tapeline + Warping viewer: may read, may not send or correct
    assert client.get(f"/api/v1/orders/{order['id']}").status_code == 200
    assert send(client, order["id"], "a@example.com", 1).status_code == 403

    sign_in(client, seeded, "dev-sender")  # all departments: may send
    s = send(client, order["id"], "a@example.com", 1).json()["data"]
    with owner_engine.begin() as conn:  # the worker stopped after marking it SENDING, before the answer
        conn.execute(update(t.order_email).where(t.order_email.c.id == uuid.UUID(s["id"])).values(state="SENDING"))
    drain()
    unknown = emails(client, order["id"])[0]
    assert unknown["state"] == "UNKNOWN" and unknown["error"]["code"] == "INTERRUPTED"  # never sent twice
    r = client.post(
        f"/api/v1/orders/{order['id']}/emails/{s['id']}/reconcile",
        headers=idem(),
        json={"outcome": "ACCEPTED", "note": "Seen in EmailJS history"},
    )
    assert r.status_code == 200 and r.json()["data"]["state"] == "ACCEPTED"

    with owner_engine.begin() as conn:  # move the order to a department the viewer cannot see
        elsewhere = next(d for c, d in seeded.departments.items() if c not in ("TAPELINE", "WARPING"))
        conn.execute(
            update(t.customer_order)
            .where(t.customer_order.c.id == uuid.UUID(order["id"]))
            .values(department_id=elsewhere)
        )
        conn.execute(text("SELECT 1"))
    sign_in(client, seeded, "dev-viewer")
    assert client.get(f"/api/v1/orders/{order['id']}").status_code == 404
    assert client.get("/api/v1/orders").json()["data"] == []
