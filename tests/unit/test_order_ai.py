"""AI order reading, multi-order / Gujarati / Hindi label reading, confirmation rules and the EmailJS REST client.

The Claude reader runs through the real Anthropic SDK with a mocked HTTP transport (no network, no key): this
checks the request we send (model, image, structured output schema) and how the answer is checked. Whether a
given handwriting is read correctly can only be shown with a live key on real diary pages.
"""

import base64
import json

import httpx
import httpx2
import pytest

from app.extraction.claude import AiError
from app.mail import emailjs_api
from app.orders import ai
from app.orders import fields as of
from tests import filegen
from tests.unit.test_claude_adapter import client_with, message


def page(lines: list[str], confidence: float | None = None) -> dict:
    return {
        "page_no": 1,
        "parser": "ocr:azure-read",
        "spans": [
            {
                "id": f"p1-s{i}",
                "text": x,
                "confidence": confidence,
                "polygon": [[0.1, 0.05 * i], [0.5, 0.05 * i], [0.5, 0.05 * i + 0.04], [0.1, 0.05 * i + 0.04]],
            }
            for i, x in enumerate(lines, start=1)
        ],
    }


def empty_fields() -> dict:
    return {
        n: {"value": None, "evidence_ids": [], "uncertain": False, "note": None, "crossed_out": None} for n in of.FIELDS
    }


def answer(orders: list[dict], kind: str = "orders") -> dict:
    return {"page_kind": kind, "languages": ["gu", "en"], "orders": orders, "warnings": []}


# --- deterministic reader --------------------------------------------------------------------


def test_several_customers_on_one_page_become_separate_orders():
    found = of.read_orders(
        page(
            [
                "Date: 26/09/2026",
                "Customer: Asha Traders",
                "Qty: 500",
                "Rate: 25",
                "Customer: Mehta Box",
                "Qty: 200",
                "Rate: 30",
                "Total: 6000",
            ]
        )
    )
    assert [f["customer_name"][0] for f in found] == ["Asha Traders", "Mehta Box"]
    assert all(f["order_date"][0] == "26/09/2026" for f in found)  # the page date applies to both
    assert found[1]["total"] == ("6000", ["p1-s8"])


def test_gujarati_and_hindi_labels_and_digits():
    found = of.read_order(page(["ગ્રાહક : રમેશ ટ્રેડર્સ", "તારીખ : ૨૬/૦૯/૨૦૨૬", "નંગ : ૫૦૦", "ભાવ : ૨૫", "કુલ : ૧૨૫૦૦"]))
    norm = of.normalize(of.inputs_from_reading(found), "DMY")
    v = {n: f["value"] for n, f in norm.fields.items()}
    assert (v["customer_name"], v["order_date"], v["quantity"], v["rate"], v["total"]) == (
        "રમેશ ટ્રેડર્સ",
        "2026-09-26",
        "500",
        "25",
        "12500",
    )
    hindi = of.read_order(page(["ग्राहक : सुरेश पैकेजिंग", "मात्रा : 300", "दर : 12"]))
    assert hindi["customer_name"][0] == "सुरेश पैकेजिंग" and hindi["rate"][0] == "12"


def test_uncertain_money_values_must_be_confirmed_by_a_person():
    inputs = {
        "customer_name": {"raw": "Asha", "evidence_ids": ["p1-s1"], "source": "extracted", "confidence": 0.99},
        "order_date": {"raw": "26/09/2026", "evidence_ids": ["p1-s2"], "source": "extracted"},
        "quantity": {"raw": "500", "evidence_ids": ["p1-s3"], "source": "extracted", "confidence": 0.62},
        "remarks": {"raw": "urgnt", "evidence_ids": ["p1-s4"], "source": "ai", "uncertain": True},
    }
    norm = of.normalize(inputs, "DMY")
    issues = {(i["field"], i["severity"]) for i in norm.issues if i["code"] == "CONFIRM_VALUE"}
    assert issues == {("quantity", "error"), ("remarks", "warning")}  # money/quantity blocks, notes only warn
    assert "62%" in next(i["message"] for i in norm.issues if i["field"] == "quantity")
    inputs["quantity"] = {"raw": "500", "evidence_ids": ["p1-s3"], "source": "reviewer"}  # confirmed as read
    assert not of.normalize(inputs, "DMY").blocking


# --- Claude order reader ---------------------------------------------------------------------


def test_claude_reader_request_and_checked_answer():
    lines = ["Shreeji Packaging 9876543210", "500 box x 25 = 12500", "delivery 5/10", "advance 5000 cash"]
    fields = empty_fields()
    fields["customer_name"] |= {"value": "Shreeji Packaging", "evidence_ids": ["p1-s1"]}
    fields["mobile"] |= {"value": "9876543210", "evidence_ids": ["p1-s1"]}
    fields["quantity"] |= {"value": "500", "evidence_ids": ["p1-s2"]}
    fields["rate"] |= {"value": "25", "evidence_ids": ["p1-s2"], "crossed_out": "20"}
    fields["total"] |= {"value": "15000", "evidence_ids": ["p1-s2"]}  # not what is written: must be confirmed
    fields["delivery_date"] |= {"value": "5/10", "evidence_ids": ["p1-s9"]}  # unknown line ID
    fields["advance"] |= {"value": "5000", "evidence_ids": ["p1-s4"], "uncertain": True, "note": "smudged"}
    doc = answer([{"fields": fields, "extra": [{"label": "Payment mode", "value": "cash", "evidence_ids": ["p1-s4"]}]}])
    client, sent = client_with(httpx2.Response(200, json=message(doc)))
    image = filegen.png(200, 150)
    reading = ai.ClaudeOrderReader(model="claude-opus-5-5", client=client).read(page(lines, 0.97), image, "DMY")

    body = json.loads(sent[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][0]["type"] == "image"  # the photo goes with the lines
    payload = json.loads(body["messages"][0]["content"][1]["text"])
    assert payload["lines"][0]["id"] == "p1-s1" and payload["lines"][0]["position"] == [0.1, 0.05]

    [order] = reading.orders
    inp = order["inputs"]
    assert inp["customer_name"]["evidence_ids"] == ["p1-s1"] and not inp["customer_name"]["uncertain"]
    assert inp["total"]["uncertain"] and "not found as written" in inp["total"]["note"]
    assert inp["delivery_date"]["uncertain"] and inp["delivery_date"]["evidence_ids"] == []
    assert inp["advance"]["uncertain"] and inp["advance"]["note"] == "smudged"
    assert order["extra"] == [{"label": "Payment mode", "value": "cash", "evidence_ids": ["p1-s4"]}]
    norm = of.normalize(inp, "DMY")
    blocking = {i["field"] for i in norm.issues if i["severity"] == "error"}
    assert {"total", "delivery_date", "advance", "order_date"} <= blocking  # order date was not written
    assert any(i["code"] == "CORRECTED_ON_PAGE" and i["field"] == "rate" for i in norm.issues)
    assert reading.meta()["prompt_version"] == ai.PROMPT_VERSION and reading.meta()["languages"] == ["gu", "en"]


def test_claude_reader_production_pages_and_failures():
    client, _ = client_with(httpx2.Response(200, json=message(answer([], kind="production"))))
    assert ai.ClaudeOrderReader(model="m", client=client).read(page(["Machine T-01"]), None, "DMY").page_kind == (
        "production"
    )
    client, _ = client_with(
        httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
    )
    with pytest.raises(AiError) as exc:
        ai.ClaudeOrderReader(model="m", client=client).read(page(["x"]), None, "DMY")
    assert exc.value.code == "AI_AUTH_FAILED" and not exc.value.transient


# --- EmailJS REST client ---------------------------------------------------------------------

CONFIG = emailjs_api.Config("service_x", "template_x", "public_x", "private-secret")


def fake(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_emailjs_request_and_outcomes():
    sent = []

    def ok(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text="OK")

    params = {
        "to_email": "owner@example.com",
        "pdf_file": "data:application/pdf;base64," + base64.b64encode(b"%PDF").decode(),
    }
    assert emailjs_api.send(CONFIG, params, fake(ok)).outcome == "ACCEPTED"
    assert sent[0] == {
        "service_id": "service_x",
        "template_id": "template_x",
        "user_id": "public_x",
        "accessToken": "private-secret",
        "template_params": params,
    }
    assert "private-secret" not in repr(CONFIG)
    cases = [
        (400, "The template ID is invalid", "FAILED"),
        (403, "API calls are disabled for non-browser applications", "FAILED"),
        (429, "Too many", "RETRY"),
        (502, "bad gateway", "UNKNOWN"),
    ]
    for status, text, outcome in cases:
        result = emailjs_api.send(CONFIG, params, fake(lambda _r, s=status, t=text: httpx.Response(s, text=t)))
        assert result.outcome == outcome, status
    refused = emailjs_api.send(
        CONFIG, params, fake(lambda _r: httpx.Response(403, text="API calls are disabled for non-browser applications"))
    )
    assert "Account -> Security" in emailjs_api.explain(refused)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    def refused_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    assert emailjs_api.send(CONFIG, params, fake(timeout)).outcome == "UNKNOWN"  # may have been sent: never resent
    assert emailjs_api.send(CONFIG, params, fake(refused_connect)).outcome == "RETRY"  # certainly not sent
