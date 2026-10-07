"""Decimal quantity parsing. Binary floating point is never used (spec §4).

Canonical quantities are Decimal(18,3), quantized with ROUND_HALF_UP. The original source
text is kept by callers in provenance; this module only reports what it did.
"""

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.domain.issues import FieldIssue

QUANTUM = Decimal("0.001")
MAX_QTY = Decimal("999999999999999.999")

_PLAIN = re.compile(r"^\d+(\.\d+)?$")  # 1250 / 1250.5
_GROUPED_EN = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")  # 1,250 / 1,250.5
_GROUPED_IN = re.compile(r"^\d{1,2}(,\d{2})*,\d{3}(\.\d+)?$")  # 1,25,000 (Indian grouping)
_SINGLE_DOT_THREE = re.compile(r"^\d{1,3}\.\d{3}$")  # 1.250: European thousands or 1.25?
_DECIMAL_COMMA = re.compile(r"^(\d+,\d{1,2}|\d{1,3}(\.\d{3})+(,\d+)?)$")  # 12,5 / 1.250,50

_NON_FINITE = {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


@dataclass
class QuantityResult:
    value: Decimal | None
    issues: list[FieldIssue] = field(default_factory=list)


def quantize(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def parse_quantity(raw: str | None, field_name: str) -> QuantityResult:
    """Parse a source quantity string using the pilot convention: comma groups, dot decimal.

    Input that another common convention would read differently gets a blocking
    AMBIGUOUS_NUMBER issue so a reviewer must confirm it (TC12).
    """
    if raw is None or not str(raw).strip():
        return QuantityResult(None, [FieldIssue(field_name, "MISSING_VALUE", "A value is required.")])

    text = str(raw).strip().replace(" ", "").replace(" ", "")
    if text.lower() in _NON_FINITE:
        return QuantityResult(None, [FieldIssue(field_name, "NOT_A_NUMBER", "Enter a finite number.")])
    if text.startswith("-"):
        return QuantityResult(None, [FieldIssue(field_name, "NEGATIVE_QUANTITY", "Quantity cannot be negative.")])
    text = text.removeprefix("+")

    issues: list[FieldIssue] = []
    if _PLAIN.match(text):
        normalized = text
        if _SINGLE_DOT_THREE.match(text):
            issues.append(
                FieldIssue(
                    field_name,
                    "AMBIGUOUS_NUMBER",
                    f'"{raw}" could mean {text} or {text.replace(".", "")}. Confirm the value.',
                )
            )
    elif _GROUPED_EN.match(text) or _GROUPED_IN.match(text):
        normalized = text.replace(",", "")
    elif _DECIMAL_COMMA.match(text):
        return QuantityResult(
            None,
            [
                FieldIssue(
                    field_name,
                    "AMBIGUOUS_NUMBER",
                    f'"{raw}" uses a different number format. Confirm the value.',
                )
            ],
        )
    else:
        return QuantityResult(None, [FieldIssue(field_name, "NOT_A_NUMBER", f'"{raw}" is not a number.')])

    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return QuantityResult(None, [FieldIssue(field_name, "NOT_A_NUMBER", f'"{raw}" is not a number.')])
    return _canonical(value, field_name, issues)


def canonical_decimal(value: Decimal, field_name: str) -> QuantityResult:
    """Validate an already-numeric Decimal (e.g. a reviewer-entered JSON decimal string)."""
    return _canonical(value, field_name, [])


def _canonical(value: Decimal, field_name: str, issues: list[FieldIssue]) -> QuantityResult:
    if not value.is_finite():
        return QuantityResult(None, [FieldIssue(field_name, "NOT_A_NUMBER", "Enter a finite number.")])
    if value < 0:
        return QuantityResult(None, [FieldIssue(field_name, "NEGATIVE_QUANTITY", "Quantity cannot be negative.")])
    q = quantize(value)
    if q > MAX_QTY:
        return QuantityResult(None, [FieldIssue(field_name, "OUT_OF_RANGE", "Quantity is too large.")])
    if q != value:
        issues.append(FieldIssue(field_name, "PRECISION_LOST", f"Rounded {value} to {q} (3 decimals).", "warning"))
    return QuantityResult(q, issues)


def decimal_string(value: Decimal) -> str:
    """JSON representation of a canonical quantity: three decimals, never exponent form."""
    return format(quantize(value), "f")
