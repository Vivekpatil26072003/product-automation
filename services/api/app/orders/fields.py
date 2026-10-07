"""Customer order fields: reading "Label : value" lines and validating the values (typed, OCR or AI read).

The deterministic reader never guesses: a value is taken only from a line whose label is a known synonym
(English, Gujarati or Hindi), and every value keeps the ID of the source line it came from (evidence). A page
may hold several orders: a repeated label starts the next one. Anything not found stays empty and editable.
The same normalization runs for read, AI-read, manually entered and corrected values, so what is saved is
always validated the same way. A value the reader was not sure of (low OCR confidence, flagged by the AI, or
without evidence) must be confirmed by a person before an order can be saved when it is a customer, date,
quantity or money value.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.domain.dates import parse_production_date
from app.domain.quantities import parse_quantity

FIELDS = [
    "customer_name",
    "customer_number",
    "customer_email",
    "mobile",
    "order_number",
    "order_date",
    "delivery_date",
    "package",
    "size",
    "material",
    "quantity",
    "rate",
    "total",
    "advance",
    "remaining",
    "priority",
    "payment_status",
    "production_status",
    "delivery_status",
    "remarks",
    "employee",
]
REQUIRED = ("customer_name", "order_date", "quantity")
DATES = ("order_date", "delivery_date")
MONEY = ("rate", "total", "advance", "remaining")
LABEL = {
    "customer_name": "Customer name",
    "customer_number": "Customer number",
    "customer_email": "Customer email",
    "mobile": "Mobile number",
    "order_number": "Order number",
    "order_date": "Order date",
    "delivery_date": "Delivery date",
    "package": "Package",
    "size": "Size",
    "material": "Material",
    "quantity": "Quantity",
    "rate": "Rate",
    "total": "Total",
    "advance": "Advance",
    "remaining": "Remaining",
    "priority": "Priority",
    "payment_status": "Payment status",
    "production_status": "Production status",
    "delivery_status": "Delivery status",
    "remarks": "Remarks",
    "employee": "Employee",
}
MAX_LEN = {
    "customer_name": 200,
    "customer_number": 60,
    "customer_email": 254,
    "mobile": 20,
    "order_number": 60,
    "package": 200,
    "size": 200,
    "material": 200,
    "priority": 60,
    "payment_status": 60,
    "production_status": 60,
    "delivery_status": 60,
    "remarks": 2000,
    "employee": 120,
}

# Values that must be confirmed by a person when the reader was not sure of them (never saved on a guess).
CONFIRM = {
    "customer_name",
    "customer_number",
    "customer_email",
    "mobile",
    "order_number",
    "order_date",
    "delivery_date",
    "quantity",
    "rate",
    "total",
    "advance",
    "remaining",
}

# "_date" is a bare "Date": used as the order date only when the note has no explicit order date.
# Gujarati and Hindi labels are common diary words; values are kept exactly as written.
SYNONYMS: dict[str, list[str]] = {
    "customer_name": [
        "customer name",
        "customer",
        "party name",
        "party",
        "client name",
        "client",
        "ગ્રાહકનું નામ",
        "ગ્રાહક",
        "પાર્ટી",
        "ग्राहक का नाम",
        "ग्राहक",
        "पार्टी",
    ],
    "customer_number": ["customer no", "customer number", "customer code", "customer id", "cust no", "party code"],
    "customer_email": ["customer email", "email id", "e-mail", "email", "mail id", "ઈમેલ", "ईमेल"],
    "mobile": [
        "mobile no",
        "mobile number",
        "mobile",
        "mob no",
        "mob",
        "phone no",
        "phone",
        "contact no",
        "contact",
        "મોબાઈલ",
        "મોબાઇલ",
        "ફોન",
        "मोबाइल",
        "फोन",
        "फ़ोन",
    ],
    "order_number": [
        "order no",
        "order number",
        "order id",
        "po no",
        "po number",
        "order ref",
        "ઓર્ડર નં",
        "ऑर्डर नं",
        "आर्डर नं",
    ],
    "order_date": ["order date", "date of order", "ઓર્ડર તારીખ", "ऑर्डर तारीख", "आर्डर तारीख"],
    "_date": ["date", "તારીખ", "तारीख", "दिनांक"],
    "delivery_date": [
        "delivery date",
        "dispatch date",
        "due date",
        "delivery",
        "ડિલિવરી તારીખ",
        "ડિલિવરી",
        "डिलीवरी तारीख",
        "डिलीवरी",
    ],
    "package": ["package type", "package", "packaging", "product", "item", "માલ", "वस्तु"],
    "size": ["size", "dimensions", "dimension", "સાઈઝ", "માપ", "साइज", "आकार"],
    "material": ["material", "મટીરીયલ", "सामग्री"],
    "quantity": ["quantity", "qty", "no of boxes", "nos", "જથ્થો", "નંગ", "मात्रा", "नग"],
    "rate": ["rate per piece", "unit price", "rate", "price", "ભાવ", "दर", "भाव"],
    "total": ["total amount", "grand total", "total", "amount", "કુલ રકમ", "કુલ", "रकम", "कुल"],
    "advance": ["advance paid", "advance", "એડવાન્સ", "ઉપાડ", "एडवांस", "अग्रिम"],
    "remaining": ["balance due", "remaining", "balance", "due amount", "pending amount", "બાકી", "बाकी", "शेष"],
    "priority": ["priority", "urgency"],
    "payment_status": ["payment status", "payment", "paid status", "પેમેન્ટ", "भुगतान"],
    "production_status": ["production status", "work status", "job status"],
    "delivery_status": ["delivery status", "dispatch status"],
    "remarks": ["remarks", "remark", "notes", "note", "instructions", "comment", "નોંધ", "टिप्पणी", "नोट"],
    "employee": ["employee", "taken by", "salesman", "staff", "prepared by", "entered by", "કર્મચારી", "कर्मचारी"],
}
# A page is an order note only when it names the customer, an order number or money. Production notes
# ("Production", "Machine", "Operator", ...) never contain these labels, so they keep their own extractor.
ANCHORS = {"customer_name", "customer_number", "order_number", "rate", "total", "advance", "remaining"}
MIN_FIELDS = 3

_LABELS = sorted(((s, f) for f, syns in SYNONYMS.items() for s in syns), key=lambda x: -len(x[0]))
# Gujarati and Devanagari digits read the same as 0-9; dates, numbers and mobiles are checked on 0-9.
_DIGITS = str.maketrans("૦૧૨૩૪૫૬૭૮૯०१२३४५६७८९", "01234567890123456789")
_ALT = "|".join(re.escape(s) for s, _ in _LABELS)
# "Label : value" (any of : = - – ; OCR sometimes reads ":" as ";" or "."), or a bare label waiting for its value.
_SEP_LINE = re.compile(rf"^\s*[-•*]?\s*(?P<label>{_ALT})\s*[:;=\-–.]+\s*(?P<value>.*?)\s*$", re.IGNORECASE)
# Without a separator only a number may follow ("Qty 500"), so prose such as "Customer wants ..." is not a label.
_NUM_LINE = re.compile(rf"^\s*(?P<label>{_ALT})\s+(?P<value>[+\d₹૦-૯०-९].*?)\s*$", re.IGNORECASE)
_BARE = re.compile(rf"^\s*(?P<label>{_ALT})\s*$", re.IGNORECASE)
_FIELD_OF = {s: f for s, f in _LABELS}


def _match(text: str) -> tuple[str, str] | None:
    for pattern in (_SEP_LINE, _NUM_LINE, _BARE):
        if m := pattern.match(text):
            return _FIELD_OF[m["label"].lower()], (m.groupdict().get("value") or "").strip()
    return None


Found = dict[str, tuple[str, list[str]]]


def read_orders(page: dict[str, Any]) -> list[Found]:
    """Every order note on a page as {field: (raw text, [span ids])}; [] when the page holds no order note.

    A label that repeats starts the next order (several customers on one page). A bare "Date" written before
    the first order is the page date: it is used for each order that has no date of its own.
    """
    orders: list[Found] = [{}]
    last: str | None = None  # field of the previous labelled line (continuation lines of remarks join it)
    waiting: str | None = None  # a bare label whose value is on the next line
    page_date: tuple[str, list[str]] | None = None

    def put(name: str, value: str, ids: list[str]) -> None:
        nonlocal page_date
        if name == "_date" and not orders[-1] and len(orders) == 1 and page_date is None:
            page_date = (value, ids)
            return
        if name in orders[-1]:
            orders.append({})
        orders[-1][name] = (value, ids)

    for span in page.get("spans", []):
        text = " ".join(span["text"].split())
        if not text:
            continue
        hit = _match(text)
        if hit and hit[1]:
            name, value = hit
            put(name, value, [span["id"]])
            last, waiting = name, None
        elif hit:
            waiting = last = hit[0]
        elif waiting:
            put(waiting, text.lstrip(":;=-– ").strip(), [span["id"]])
            waiting = None
        elif last == "remarks" and "remarks" in orders[-1]:  # a remark written over several lines
            raw, ids = orders[-1]["remarks"]
            orders[-1]["remarks"] = (f"{raw} {text}", [*ids, span["id"]])
    out = []
    for found in orders:
        bare_date = found.pop("_date", None) or page_date
        if bare_date and "order_date" not in found:
            found["order_date"] = bare_date
        if len(found) >= MIN_FIELDS and ANCHORS & found.keys():
            out.append(found)
    return out


def read_order(page: dict[str, Any]) -> Found | None:
    """The first order note on a page."""
    found = read_orders(page)
    return found[0] if found else None


# --- normalization ---------------------------------------------------------------------------


@dataclass
class Normalized:
    fields: dict[str, dict[str, Any]]
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(i["severity"] == "error" for i in self.issues)


def _issue(name: str | None, code: str, message: str, severity: str = "error") -> dict[str, Any]:
    return {"field": name, "code": code, "message": message, "severity": severity}


_CURRENCY = re.compile(r"(?i)(rs\.?|inr|₹|/-)")
_COUNT_WORDS = re.compile(r"(?i)\s*(pcs|pc|pieces|nos|no|boxes|box|units)\.?\s*$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")


def _number(name: str, raw: str, issues: list[dict[str, Any]]) -> Decimal | None:
    text = _COUNT_WORDS.sub("", _CURRENCY.sub("", raw)).strip()
    result = parse_quantity(text, name)
    for i in result.issues:
        message = i.message if i.code != "NEGATIVE_QUANTITY" else f"{LABEL[name]} cannot be negative."
        issues.append(_issue(name, i.code, message, i.severity))
    if result.value is None:
        return None
    places = Decimal("0.01") if name in MONEY else Decimal("0.001")
    if result.value != result.value.quantize(places):
        issues.append(_issue(name, "TOO_MANY_DECIMALS", f"{LABEL[name]} has too many decimal places."))
        return None
    return result.value


def _date(name: str, raw: str, date_order: str, issues: list[dict[str, Any]]) -> date | None:
    result = parse_production_date(raw, date(9999, 12, 31), date_order)  # orders may be dated ahead
    for i in result.issues:
        # A reviewer sees the date as read (shown in words) and corrects it if needed.
        severity = "warning" if i.code in ("AMBIGUOUS_DATE", "TWO_DIGIT_YEAR") else i.severity
        issues.append(_issue(name, i.code, i.message, severity))
    return result.value


CONFIDENCE_THRESHOLD = 0.90  # same pilot heuristic as production entries (spec §8)


def normalize(inputs: dict[str, dict[str, Any]], date_order: str) -> Normalized:
    """inputs: {field: {"raw": text|None, "evidence_ids": [...], "source": ..., "confidence": float|None,
    "uncertain": bool, "note": str|None, "corrected_from": str|None}} -> checked canonical values."""
    issues: list[dict[str, Any]] = []
    out: dict[str, dict[str, Any]] = {}
    values: dict[str, Any] = {}
    for name in FIELDS:
        inp = inputs.get(name) or {}
        raw = inp.get("raw")
        raw = " ".join(str(raw).split()) if raw is not None else ""
        if raw and (name in DATES or name in MONEY or name in ("quantity", "mobile")):
            raw = raw.translate(_DIGITS)
        value: Any = None
        display: str | None = None
        if not raw:
            if name in REQUIRED:
                issues.append(_issue(name, "MISSING_VALUE", f"{LABEL[name]} is required."))
        elif name in DATES:
            d = _date(name, raw, date_order, issues)
            if d is not None:
                value, display = d.isoformat(), f"{d.day} {d:%b %Y}"
        elif name in MONEY or name == "quantity":
            n = _number(name, raw, issues)
            if n is not None:
                value = format(n.normalize(), "f")
        elif name == "customer_email":
            if _EMAIL.match(raw.replace(" ", "")):
                value = raw.replace(" ", "").lower()
            else:
                issues.append(_issue(name, "INVALID_EMAIL", f'"{raw}" is not a valid email address.'))
        elif name == "mobile":
            digits = re.sub(r"[\s\-().]", "", raw)
            if re.fullmatch(r"\+?\d{7,15}", digits):
                value = digits
            else:
                issues.append(_issue(name, "INVALID_MOBILE", f'"{raw}" is not a valid mobile number (7 to 15 digits).'))
        elif len(raw) > MAX_LEN[name]:
            issues.append(_issue(name, "TOO_LONG", f"{LABEL[name]} is longer than {MAX_LEN[name]} characters."))
        else:
            value = raw
        values[name] = value
        source = inp.get("source", "extracted")
        confidence = inp.get("confidence")
        uncertain = (
            bool(raw)
            and source in ("extracted", "ai")
            and (
                bool(inp.get("uncertain"))
                or (confidence is not None and confidence < CONFIDENCE_THRESHOLD)
                or (source == "ai" and not inp.get("evidence_ids"))
            )
        )
        if uncertain:
            why = inp.get("note") or (
                f"read with {confidence:.0%} confidence" if confidence is not None else "the reader was not sure"
            )
            severity = "error" if name in CONFIRM else "warning"
            issues.append(
                _issue(
                    name,
                    "CONFIRM_VALUE",
                    f"Check {LABEL[name].lower()} against the page ({why}), then confirm it.",
                    severity,
                )
            )
        if raw and inp.get("corrected_from") and source in ("extracted", "ai"):
            issues.append(
                _issue(
                    name,
                    "CORRECTED_ON_PAGE",
                    f'Corrected on the page (crossed out: "{inp["corrected_from"]}"). Check the new value.',
                    "warning",
                )
            )
        out[name] = {
            "value": value,
            "display": display,
            "raw": inp.get("raw"),
            "evidence_ids": list(inp.get("evidence_ids") or []),
            "source": source,
            "confidence": confidence,
            "uncertain": uncertain,
            "note": inp.get("note"),
            "corrected_from": inp.get("corrected_from"),
        }
    _consistency(values, issues)
    return Normalized(out, issues)


def _consistency(v: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    """Advisory checks: a misread digit usually breaks one of these sums."""
    d = {k: Decimal(v[k]) for k in ("quantity", *MONEY) if v.get(k) is not None}
    if {"quantity", "rate", "total"} <= d.keys() and d["quantity"] * d["rate"] != d["total"]:
        issues.append(
            _issue(
                "total",
                "TOTAL_MISMATCH",
                f"Quantity × rate is {d['quantity'] * d['rate']:,.2f}, but the total is {d['total']:,.2f}. "
                "Check the values.",
                "warning",
            )
        )
    if {"total", "advance", "remaining"} <= d.keys() and d["total"] - d["advance"] != d["remaining"]:
        issues.append(
            _issue(
                "remaining",
                "BALANCE_MISMATCH",
                f"Total − advance is {d['total'] - d['advance']:,.2f}, but remaining is "
                f"{d['remaining']:,.2f}. Check the values.",
                "warning",
            )
        )
    if v.get("order_date") and v.get("delivery_date") and v["delivery_date"] < v["order_date"]:
        issues.append(
            _issue("delivery_date", "DELIVERY_BEFORE_ORDER", "Delivery date is before the order date.", "warning")
        )


def inputs_from_reading(found: dict[str, tuple[str, list[str]]]) -> dict[str, dict[str, Any]]:
    return {n: {"raw": found[n][0], "evidence_ids": found[n][1], "source": "extracted"} for n in found}


_KEEP = ("raw", "evidence_ids", "confidence", "uncertain", "note", "corrected_from")


def inputs_from_stored(fields: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        n: {k: f.get(k) for k in _KEEP if f.get(k) is not None} | {"source": f.get("source", "extracted")}
        for n, f in fields.items()
    }


def revision_values(fields: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Checked field values -> order_revision columns (None for empty optional values)."""
    out: dict[str, Any] = {}
    for name in FIELDS:
        v = (fields.get(name) or {}).get("value")
        if name in DATES:
            out[name] = date.fromisoformat(v) if v else None
        elif name in MONEY or name == "quantity":
            out[name] = Decimal(v) if v is not None else None
        else:
            out[name] = v
    out["remarks"] = out["remarks"] or ""
    return out
