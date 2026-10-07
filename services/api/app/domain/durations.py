"""stop_minutes parsing: accumulated downtime in minutes for the record's period, 0–1440.

Missing or unreadable values stay null with a blocking issue. They are never defaulted to zero.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.issues import FieldIssue

F = "stop_minutes"
MAX_MINUTES = 1440

_HM = re.compile(
    r"^(?:(?P<h>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours))?\s*"
    r"(?:(?P<m>\d+)\s*(?:m|min|mins|minute|minutes))?$"
)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass
class MinutesResult:
    value: int | None
    issues: list[FieldIssue] = field(default_factory=list)


def _range_checked(minutes: int, issues: list[FieldIssue]) -> MinutesResult:
    if not 0 <= minutes <= MAX_MINUTES:
        return MinutesResult(None, [*issues, FieldIssue(F, "OUT_OF_RANGE", "Stop time must be 0–1440 minutes.")])
    return MinutesResult(minutes, issues)


def parse_stop_minutes(raw: str | int | None) -> MinutesResult:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return MinutesResult(None, [FieldIssue(F, "MISSING_VALUE", "Stop time is required; enter 0 if none.")])
    if isinstance(raw, int):
        return _range_checked(raw, [])

    text = " ".join(raw.strip().lower().split())
    if text.isdigit():
        return _range_checked(int(text), [])
    if text.startswith("-"):
        return MinutesResult(None, [FieldIssue(F, "OUT_OF_RANGE", "Stop time must be 0–1440 minutes.")])

    if m := _CLOCK.match(text):
        # "1:30" may be a duration or a clock time. Propose the duration but require confirmation.
        return _range_checked(
            int(m[1]) * 60 + int(m[2]),
            [FieldIssue(F, "AMBIGUOUS_DURATION", f'"{raw}" could be a clock time. Confirm the minutes.')],
        )

    m = _HM.match(text)
    if m and (m["h"] or m["m"]):
        total = Decimal(m["h"] or 0) * 60 + Decimal(m["m"] or 0)
        if total != total.to_integral_value():
            return MinutesResult(
                None, [FieldIssue(F, "NOT_WHOLE_MINUTES", f'"{raw}" is not a whole number of minutes.')]
            )
        return _range_checked(int(total), [])

    return MinutesResult(None, [FieldIssue(F, "UNREADABLE", f'"{raw}" is not a recognised duration.')])
