"""Customer order notes: reading "Label : value" lines (typed text or OCR lines) and validating the values."""

import httpx

from app.ingestion.ocr import AzureReadOcr
from app.orders import fields as of

# A handwritten order note as an OCR reader returns it: one line per written line (invented example values).
NOTE = [
    "Date : 26/09/2026",
    "Customer : Asha Traders",
    "Mobile : 91234 56780",
    "Order Date : 26/09/2026",
    "Delivery Date : 05/10/2026",
    "Package : Corrugated Box",
    "Size : 20 x 15 x 10 inch",
    "Material : 5 Ply",
    "Quantity : 500",
    "Rate : 25",
    "Total : 12500",
    "Advance : 5000",
    "Remaining : 7500",
    "Priority : Urgent",
    "Remark : Need delivery before 5 Oct.",
    "Customer wants good quality printing.",
    "Employee : Ravi",
]


def page(lines: list[str]) -> dict:
    return {"page_no": 1, "spans": [{"id": f"p1-s{i}", "text": x} for i, x in enumerate(lines, start=1)]}


def values(lines: list[str]) -> tuple[dict, list]:
    norm = of.normalize(of.inputs_from_reading(of.read_order(page(lines))), "DMY")
    return {n: f["value"] for n, f in norm.fields.items()}, norm.issues


def test_every_labelled_line_maps_to_its_form_field_with_evidence():
    found = of.read_order(page(NOTE))
    assert found["customer_name"] == ("Asha Traders", ["p1-s2"])
    assert found["remarks"] == (
        "Need delivery before 5 Oct. Customer wants good quality printing.",
        ["p1-s15", "p1-s16"],
    )
    v, issues = values(NOTE)
    assert v == {
        "customer_name": "Asha Traders",
        "customer_number": None,
        "customer_email": None,
        "mobile": "9123456780",
        "order_number": None,
        "order_date": "2026-09-26",
        "delivery_date": "2026-10-05",
        "package": "Corrugated Box",
        "size": "20 x 15 x 10 inch",
        "material": "5 Ply",
        "quantity": "500",
        "rate": "25",
        "total": "12500",
        "advance": "5000",
        "remaining": "7500",
        "priority": "Urgent",
        "payment_status": None,
        "production_status": None,
        "delivery_status": None,
        "remarks": "Need delivery before 5 Oct. Customer wants good quality printing.",
        "employee": "Ravi",
    }
    # 05/10 could be 5 Oct or 10 May: read with the company's day-month order and flagged, not blocked.
    assert [(i["field"], i["code"], i["severity"]) for i in issues] == [("delivery_date", "AMBIGUOUS_DATE", "warning")]


def test_partial_notes_fill_what_was_read_and_leave_the_rest_editable():
    v, issues = values(["Customer: Asha Traders", "Rate - 25", "Qty 500"])
    assert (v["customer_name"], v["rate"], v["quantity"], v["order_date"]) == ("Asha Traders", "25", "500", None)
    assert {(i["field"], i["code"]) for i in issues if i["severity"] == "error"} == {("order_date", "MISSING_VALUE")}


def test_label_and_value_on_separate_lines_and_currency_text():
    v, _ = values(["Customer", "Asha Traders", "Order No : PO-77", "Total : Rs. 12,500/-", "Email: Asha@Example.com"])
    assert (v["customer_name"], v["order_number"], v["total"], v["customer_email"]) == (
        "Asha Traders",
        "PO-77",
        "12500",
        "asha@example.com",
    )


def test_production_notes_are_not_orders():
    production = [
        "Date: 27/09/2026",
        "Department: Tapeline",
        "Machine: TL-01",
        "Operator: Ravi",
        "Production: 1250 m",
        "Target: 1500 m",
        "Status: Running",
        "Remarks: belt change",
    ]
    assert of.read_order(page(production)) is None
    assert of.read_order(page(["Customer wants delivery soon", "Thank you"])) is None  # prose is not a label


def test_misread_values_are_flagged_for_the_reviewer():
    _, issues = values(
        [
            "Customer: A",
            "Order date: 31/02/2026",
            "Quantity: 5O0",
            "Rate: 25",
            "Total: 12000",
            "Mobile: 12ab",
            "Email: not-an-email",
        ]
    )
    codes = {(i["field"], i["code"]) for i in issues}
    assert {
        ("order_date", "INVALID_DATE"),
        ("quantity", "NOT_A_NUMBER"),
        ("mobile", "INVALID_MOBILE"),
        ("customer_email", "INVALID_EMAIL"),
    } <= codes
    _, issues = values(["Customer: A", "Order date: 26/09/2026", "Quantity: 500", "Rate: 25", "Total: 12000"])
    assert [(i["code"], i["severity"]) for i in issues] == [("TOTAL_MISMATCH", "warning")]


def test_azure_read_lines_reach_the_order_form():
    """The Azure adapter's lines (recorded response shape) are what the order reader receives."""
    lines = NOTE[:4]
    offsets, words, out_lines, pos = [], [], [], 0
    for i, text in enumerate(lines):
        out_lines.append(
            {
                "content": text,
                "polygon": [10, 10 + 40 * i, 500, 10 + 40 * i, 500, 40 + 40 * i, 10, 40 + 40 * i],
                "spans": [{"offset": pos, "length": len(text)}],
            }
        )
        words.append({"content": text, "confidence": 0.97, "span": {"offset": pos, "length": len(text)}})
        offsets.append(pos)
        pos += len(text) + 1
    result = {
        "status": "succeeded",
        "analyzeResult": {"pages": [{"width": 1000, "height": 1400, "lines": out_lines, "words": words}]},
    }
    responses = [
        httpx.Response(202, headers={"Operation-Location": "https://ocr.test.invalid/op"}),
        httpx.Response(200, json=result),
    ]
    ocr = AzureReadOcr(
        "https://ocr.test.invalid",
        "k",
        "2024-11-30",
        poll_seconds=0,
        http=httpx.Client(transport=httpx.MockTransport(lambda _: responses.pop(0))),
    )
    ocr_page = ocr.read(b"png")
    spans = [{"id": f"p1-s{i}", "text": line.text} for i, line in enumerate(ocr_page.lines, start=1)]
    found = of.read_order({"page_no": 1, "spans": spans})
    assert {k: v[0] for k, v in found.items()} == {
        "customer_name": "Asha Traders",
        "mobile": "91234 56780",
        "order_date": "26/09/2026",
    }
