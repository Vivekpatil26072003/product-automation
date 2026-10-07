"""EmailJS account from the backend environment (EMAILJS_* in .env), used by the existing Send Email workflow.

Checks the complete order email path with environment configuration only (nothing stored in settings), that the
private key never leaves the backend (API responses, audit log, application log), that environment values are
read-only in Settings -> Owner report & email, and that a broken attachment is never sent. EmailJS itself is the
recorded transport from test_orders (no network, no real email).
"""

import json
import logging
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db import tables as t
from app.orders import service as orders
from tests.conftest import idem, sign_in
from tests.integration.test_orders import FakeEmailJs, approve, drafts, drain, send, upload_note

pytestmark = [pytest.mark.db, pytest.mark.infra]
SECRET = "test-private-key-never-leaves-backend"


@pytest.fixture
def env_account(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "emailjs_service_id", "service_75drj1q")
    monkeypatch.setattr(cfg, "emailjs_template_id", "template_i9cldst")
    monkeypatch.setattr(cfg, "emailjs_public_key", "public-from-env")
    monkeypatch.setattr(cfg, "emailjs_private_key", SecretStr(SECRET))


def test_send_email_with_environment_account(client, seeded, monkeypatch, env_account, owner_engine, caplog):
    caplog.set_level(logging.DEBUG)
    mail = FakeEmailJs(monkeypatch)
    sign_in(client, seeded, "dev-reviewer")
    body = upload_note(client, seeded, monkeypatch)
    order = approve(client, drafts(client, body["batch_id"])[0]).json()["data"]

    # 1-6: one address typed; the server builds everything else from the saved order and sends with the env account
    r = send(client, order["id"], "customer@example.com", 1)
    assert r.status_code == 202, r.text
    drain()
    view = client.get(f"/api/v1/orders/{order['id']}").json()["data"]
    assert view["emails"][0]["state"] == "ACCEPTED"  # 7: shown as sent only after EmailJS accepted it
    sent = mail.sent[0]
    assert (sent["service_id"], sent["template_id"], sent["user_id"]) == (
        "service_75drj1q",
        "template_i9cldst",
        "public-from-env",
    )
    assert sent["accessToken"] == SECRET  # only in the server-to-EmailJS request
    p = sent["template_params"]
    assert p["to_email"] == "customer@example.com" and p["customer_name"] == "Asha Traders"
    assert p["order_number"] == order["order_ref"] and p["quantity"] == "500" and p["total"] == "12,500.00"
    assert p["subject"] == f"Your Order/Report - {order['order_ref']}" and "Customer: Asha Traders" in p["message"]
    assert mail.pdf().startswith(b"%PDF-") and p["attachment_name"].endswith(".pdf")

    # 8: status in the database and every step in the audit log - without the key
    with owner_engine.connect() as conn:
        actions = [
            (a.action, a.after)
            for a in conn.execute(
                select(t.audit_event)
                .where(t.audit_event.c.object_id == uuid.UUID(order["id"]))
                .order_by(t.audit_event.c.created_at)
            )
        ]
    names = [a for a, _ in actions]
    assert names[-3:] == ["ORDER_EMAIL_QUEUED", "EMAIL_SEND_STARTED", "EMAIL_SEND_ACCEPTED"]
    assert SECRET not in json.dumps([after for _, after in actions])

    # The key never reaches API responses or the application log.
    sign_in(client, seeded, "dev-admin")
    settings = client.get("/api/v1/settings/owner-report")
    assert SECRET not in settings.text and settings.json()["data"]["email_ready"]
    assert settings.json()["data"]["emailjs"]["sources"]["emailjs_private_key"] == "environment"
    assert settings.json()["data"]["emailjs"]["template_id"] == "template_i9cldst"
    assert SECRET not in caplog.text


def test_environment_values_are_read_only_in_settings(client, seeded, env_account):
    sign_in(client, seeded, "dev-admin")
    current = client.get("/api/v1/settings/owner-report")
    r = client.put(
        "/api/v1/settings/owner-report",
        headers={**idem(), "If-Match": current.headers["ETag"]},
        json={"emailjs_template_id": "template_other", "emailjs_private_key": "typed-in-browser"},
    )
    assert r.status_code == 422
    assert {f["code"] for f in r.json()["error"]["fields"]} == {"SET_IN_ENVIRONMENT"}
    ok = client.put(
        "/api/v1/settings/owner-report",
        headers={**idem(), "If-Match": current.headers["ETag"]},
        json={"owner_email": "owner@company.example", "auto_send": True},  # owner settings still saved here
    )
    assert ok.status_code == 200 and ok.json()["data"]["auto_send"], ok.text


def test_duplicate_and_invalid_sends_are_refused(client, seeded, monkeypatch, env_account):
    FakeEmailJs(monkeypatch)
    sign_in(client, seeded, "dev-reviewer")
    body = upload_note(client, seeded, monkeypatch)
    order = approve(client, drafts(client, body["batch_id"])[0]).json()["data"]
    key = idem()
    first = client.post(
        f"/api/v1/orders/{order['id']}/emails", json={"to_email": "a@example.com", "revision": 1}, headers=key
    )
    replay = client.post(
        f"/api/v1/orders/{order['id']}/emails", json={"to_email": "a@example.com", "revision": 1}, headers=key
    )
    assert first.status_code == replay.status_code == 202 and first.json() == replay.json()  # a retried request
    again = send(client, order["id"], "a@example.com", 1)
    assert again.status_code == 409 and again.json()["error"]["code"] == "ALREADY_SENDING"  # a second click
    assert send(client, order["id"], "not an email", 1).status_code == 422
    drain()
    assert [e["state"] for e in client.get(f"/api/v1/orders/{order['id']}").json()["data"]["emails"]] == ["ACCEPTED"]


def test_broken_attachment_is_never_sent():
    for data, name in ((b"", "Order_A_r1.pdf"), (b"<html>", "Order_A_r1.pdf"), (b"%PDF-1.4 ... %%EOF", "a.txt")):
        with pytest.raises(ApiError) as exc:
            orders.check_attachment(data, name)
        assert exc.value.code == "ATTACHMENT_INVALID"
    orders.check_attachment(b"%PDF-1.4\n...\n%%EOF\n", "Order_A_r1.pdf")
