"""Schedule occurrences and reporting periods (FR24, spec §12 "Scheduling rules").

Pure functions, no database. A schedule stores an IANA time zone plus a local time; each occurrence is derived
from those, so daylight-saving changes never shift the local run time.

- DAILY    runs every day at local_time and reports the previous completed local day.
- WEEKLY   runs on `weekday` (ISO, 1 = Monday) and reports the previous completed Monday-Sunday week.
- MONTHLY  runs on `monthday` (a day beyond the month's length clamps to its last day) and reports the
           previous completed calendar month.
- A local time that does not exist (spring gap) moves to the first valid instant after the gap.
- A local time that happens twice (autumn repeat) runs once, at its first occurrence.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CADENCES = ("DAILY", "WEEKLY", "MONTHLY")


@dataclass(frozen=True)
class Cadence:
    kind: str
    local_time: time
    timezone: str
    weekday: int | None = None  # ISO 1..7, WEEKLY only
    monthday: int | None = None  # 1..31, MONTHLY only

    def __post_init__(self) -> None:
        if self.kind not in CADENCES:
            raise ValueError("cadence must be DAILY, WEEKLY or MONTHLY")
        if self.kind == "WEEKLY" and not (self.weekday and 1 <= self.weekday <= 7):
            raise ValueError("weekly schedules need a weekday 1-7 (1 = Monday)")
        if self.kind == "MONTHLY" and not (self.monthday and 1 <= self.monthday <= 31):
            raise ValueError("monthly schedules need a day of the month 1-31")
        ZoneInfo(self.timezone)  # raises for unknown zones


@dataclass(frozen=True)
class Occurrence:
    due_at: datetime  # UTC
    local_date: date
    period_start: date
    period_end: date


def to_utc(day: date, at: time, tz: ZoneInfo) -> datetime:
    """Local wall time -> UTC instant, applying the gap and repeat rules."""
    naive = datetime.combine(day, at)
    first = naive.replace(tzinfo=tz, fold=0)  # fold=0: the first of two repeated instants
    utc = first.astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) == naive:
        return utc
    # Non-existent local time: find the instant the clocks jumped (first valid instant after the gap).
    lo = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    hi = utc
    if lo > hi:
        lo, hi = hi, lo
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == hi.astimezone(tz).utcoffset():
            hi = mid
        else:
            lo = mid
    return hi.replace(microsecond=0)


def _runs_on(c: Cadence, d: date) -> bool:
    if c.kind == "DAILY":
        return True
    if c.kind == "WEEKLY":
        return d.isoweekday() == c.weekday
    last = calendar.monthrange(d.year, d.month)[1]
    return d.day == min(c.monthday or 1, last)


def period_for(c: Cadence, d: date) -> tuple[date, date]:
    """The completed reporting period for an occurrence on local date d."""
    if c.kind == "DAILY":
        prev = d - timedelta(days=1)
        return prev, prev
    if c.kind == "WEEKLY":
        monday = d - timedelta(days=d.isoweekday() - 1)
        return monday - timedelta(days=7), monday - timedelta(days=1)
    first_this = d.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


def occurrences_between(c: Cadence, start: datetime, end: datetime, limit: int = 400) -> list[Occurrence]:
    """Occurrences with start < due_at <= end (UTC), oldest first."""
    tz = ZoneInfo(c.timezone)
    out: list[Occurrence] = []
    d = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    while d <= last and len(out) < limit:
        if _runs_on(c, d):
            due = to_utc(d, c.local_time, tz)
            if start < due <= end:
                ps, pe = period_for(c, d)
                out.append(Occurrence(due, d, ps, pe))
        d += timedelta(days=1)
    return out


def next_occurrences(c: Cadence, after: datetime, n: int = 3) -> list[Occurrence]:
    horizon = {"DAILY": 2, "WEEKLY": 15, "MONTHLY": 63}[c.kind] * n
    return occurrences_between(c, after, after + timedelta(days=horizon))[:n]


def latest_completed_period(c: Cadence, now: datetime) -> tuple[date, date]:
    """Default period for Run now: the period of the most recent occurrence date up to today."""
    tz = ZoneInfo(c.timezone)
    d = now.astimezone(tz).date()
    while not _runs_on(c, d):
        d -= timedelta(days=1)
    return period_for(c, d)
