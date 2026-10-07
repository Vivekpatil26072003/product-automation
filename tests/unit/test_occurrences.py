"""Schedule occurrences (FR24; TC44, TC45): periods, DST gap and repeat, month-end clamping, leap years."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from app.automation.occurrences import (
    Cadence,
    latest_completed_period,
    next_occurrences,
    occurrences_between,
    to_utc,
)


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_daily_reports_the_previous_local_day():  # TC44
    c = Cadence("DAILY", time(7, 0), "Asia/Kolkata")
    occ = next_occurrences(c, utc(2026, 9, 27, 0, 0))
    assert [o.local_date for o in occ] == [date(2026, 9, 27), date(2026, 9, 28), date(2026, 9, 29)]
    assert occ[0].due_at == utc(2026, 9, 27, 1, 30)  # 07:00 IST
    assert (occ[0].period_start, occ[0].period_end) == (date(2026, 9, 26), date(2026, 9, 26))


def test_weekly_reports_the_previous_monday_to_sunday():
    c = Cadence("WEEKLY", time(8, 0), "Asia/Kolkata", weekday=1)
    o = next_occurrences(c, utc(2026, 9, 29))[0]
    assert o.local_date == date(2026, 10, 5) and o.local_date.isoweekday() == 1
    assert (o.period_start, o.period_end) == (date(2026, 9, 28), date(2026, 10, 4))


def test_monthly_clamps_to_month_end_and_handles_leap_february():  # TC45
    c = Cadence("MONTHLY", time(6, 0), "UTC", monthday=31)
    occ = next_occurrences(c, utc(2027, 12, 1), n=4)
    assert [o.local_date for o in occ] == [date(2027, 12, 31), date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 31)]
    feb = next(o for o in occ if o.local_date.month == 3)
    assert (feb.period_start, feb.period_end) == (date(2028, 2, 1), date(2028, 2, 29))


def test_spring_gap_moves_to_first_valid_instant():  # TC45
    tz = ZoneInfo("America/New_York")
    due = to_utc(date(2026, 3, 8), time(2, 30), tz)  # 02:00-03:00 does not exist that night
    assert due == utc(2026, 3, 8, 7, 0)  # 03:00 EDT, the moment the clocks jump
    assert due.astimezone(tz).strftime("%H:%M") == "03:00"


def test_autumn_repeat_runs_once_at_the_first_occurrence():  # TC45
    c = Cadence("DAILY", time(1, 30), "America/New_York")
    occ = occurrences_between(c, utc(2026, 11, 1, 0, 0), utc(2026, 11, 2, 0, 0))
    assert len(occ) == 1 and occ[0].due_at == utc(2026, 11, 1, 5, 30)  # 01:30 EDT, not 01:30 EST


def test_invalid_cadences_are_refused():
    with pytest.raises(ValueError):
        Cadence("WEEKLY", time(8), "UTC")
    with pytest.raises(ValueError):
        Cadence("MONTHLY", time(8), "UTC", monthday=32)
    with pytest.raises(ZoneInfoNotFoundError):
        Cadence("DAILY", time(8), "Mars/Olympus")


def test_run_now_defaults_to_the_latest_completed_period():
    c = Cadence("WEEKLY", time(8), "Asia/Kolkata", weekday=1)
    assert latest_completed_period(c, utc(2026, 9, 30, 6)) == (date(2026, 9, 21), date(2026, 9, 27))
