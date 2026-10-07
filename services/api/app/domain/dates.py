"""production_date parsing (spec §4). The authoritative value is a company-local calendar date.

Pilot convention is DMY. An input that is valid under both day/month orders (03/04/2026) is
returned with its configured-order value and a blocking AMBIGUOUS_DATE issue: a reviewer must
confirm it. A date after the company-local "today" is rejected.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.domain.issues import FieldIssue

DateOrder = Literal["DMY", "MDY"]
F = "production_date"

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_NUMERIC = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})$")
_DAY_MONTHNAME = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-/]+([a-z]+)[\s\-/,]+(\d{2}|\d{4})$")
_MONTHNAME_DAY = re.compile(r"^([a-z]+)[\s\-/]+(\d{1,2})(?:st|nd|rd|th)?[\s,\-/]+(\d{2}|\d{4})$")


@dataclass
class DateResult:
    value: date | None
    issues: list[FieldIssue] = field(default_factory=list)


def _year(text: str, issues: list[FieldIssue]) -> int:
    if len(text) == 2:
        issues.append(FieldIssue(F, "TWO_DIGIT_YEAR", f"Year {text} read as 20{text}.", "warning"))
        return 2000 + int(text)
    return int(text)


def _build(y: int, m: int, d: int, issues: list[FieldIssue], today: date) -> DateResult:
    try:
        value = date(y, m, d)
    except ValueError:
        return DateResult(None, [*issues, FieldIssue(F, "INVALID_DATE", "This date does not exist.")])
    if value > today:
        issues.append(FieldIssue(F, "FUTURE_DATE", "Production date cannot be in the future."))
    return DateResult(value, issues)


def parse_production_date(raw: str | None, today: date, order: DateOrder = "DMY") -> DateResult:
    if raw is None or not raw.strip():
        return DateResult(None, [FieldIssue(F, "MISSING_VALUE", "Production date is required.")])
    text = raw.strip()
    if not (_ISO.match(text) or _NUMERIC.match(text)):
        # Word forms: "27 Sept. 2026", "Sep 27, 2026" -> lower-case, dots dropped, single spaces.
        text = " ".join(text.lower().replace(".", " ").split())
    issues: list[FieldIssue] = []

    if m := _ISO.match(text):
        return _build(int(m[1]), int(m[2]), int(m[3]), issues, today)

    if m := _NUMERIC.match(text):
        a, b, y = int(m[1]), int(m[2]), _year(m[3], issues)
        day, month = (a, b) if order == "DMY" else (b, a)
        if month > 12 and day <= 12:
            issues.append(FieldIssue(F, "DATE_ORDER_MISMATCH", f'"{raw}" does not match the configured {order} order.'))
            day, month = month, day
        elif a <= 12 and b <= 12 and a != b:
            issues.append(
                FieldIssue(F, "AMBIGUOUS_DATE", f'"{raw}" could be read as day/month or month/day. Confirm the date.')
            )
        return _build(y, month, day, issues, today)

    for pattern, day_idx, mon_idx in ((_DAY_MONTHNAME, 1, 2), (_MONTHNAME_DAY, 2, 1)):
        if (m := pattern.match(text)) and m[mon_idx] in _MONTHS:
            return _build(_year(m[3], issues), _MONTHS[m[mon_idx]], int(m[day_idx]), issues, today)

    return DateResult(None, [FieldIssue(F, "INVALID_DATE", f'"{raw}" is not a recognised date.')])
