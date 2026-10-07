"""FR06 normalization rules (spec §4). Covers TC11–TC14 at the domain level."""

from datetime import date
from decimal import Decimal

import pytest

from app.domain.dates import parse_production_date
from app.domain.durations import parse_stop_minutes
from app.domain.enums import Status, Unit
from app.domain.quantities import decimal_string, parse_quantity
from app.domain.status import normalize_status
from app.domain.units import convert_pair, resolve_unit

TODAY = date(2026, 9, 28)


def codes(result):
    return {i.code for i in result.issues}


def blocking(result):
    return {i.code for i in result.issues if i.blocking}


# --- quantities ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1250", "1250.000"),
        ("1,250", "1250.000"),
        ("1,25,000", "125000.000"),
        ("1250.5", "1250.500"),
        ("0", "0.000"),
        (" 980 ", "980.000"),
        ("+12", "12.000"),
    ],
)
def test_quantity_parses_to_canonical_decimal(raw, expected):
    r = parse_quantity(raw, "production_qty")
    assert r.value == Decimal(expected)
    assert not blocking(r)
    assert decimal_string(r.value) == expected


def test_quantity_rounds_half_up_to_three_decimals_with_warning():
    r = parse_quantity("1.23456", "production_qty")
    assert r.value == Decimal("1.235")
    assert codes(r) == {"PRECISION_LOST"} and not blocking(r)
    assert parse_quantity("0.0005", "q").value == Decimal("0.001")  # half up, not banker's


@pytest.mark.parametrize("raw", ["1.250", "12,5", "1.250,50"])
def test_quantity_in_conflicting_locale_requires_confirmation(raw):  # TC12
    assert "AMBIGUOUS_NUMBER" in blocking(parse_quantity(raw, "production_qty"))


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, "MISSING_VALUE"),
        ("", "MISSING_VALUE"),
        ("-5", "NEGATIVE_QUANTITY"),
        ("NaN", "NOT_A_NUMBER"),
        ("Infinity", "NOT_A_NUMBER"),
        ("abc", "NOT_A_NUMBER"),
        ("1e5", "NOT_A_NUMBER"),
        ("9999999999999999", "OUT_OF_RANGE"),
    ],
)
def test_quantity_rejects_invalid(raw, code):  # TC14
    r = parse_quantity(raw, "target_qty")
    assert r.value is None
    assert code in blocking(r)


def test_missing_target_is_not_zero():
    assert parse_quantity(None, "target_qty").value is None


# --- units -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("qty", "raw_unit", "unit", "expected"),
    [
        ("100", "cm", Unit.M, "1.000"),
        ("1", "km", Unit.M, "1000.000"),
        ("1000", "g", Unit.KG, "1.000"),
        ("1250", "meter", Unit.M, "1250.000"),
        ("1250", "Mtr.", Unit.M, "1250.000"),
        ("12", "Nos", Unit.PCS, "12.000"),
    ],
)
def test_unit_conversion_is_exact(qty, raw_unit, unit, expected):  # TC13
    pair = convert_pair(Decimal(qty), resolve_unit(raw_unit), Decimal(qty))
    assert pair.unit is unit
    assert pair.production_qty == Decimal(expected) == pair.target_qty
    assert not pair.issues


def test_fractional_pieces_blocked():  # TC13
    pair = convert_pair(Decimal("1.5"), resolve_unit("pcs"), Decimal(2))
    assert "FRACTIONAL_PCS" in {i.code for i in pair.issues if i.blocking}


def test_dimension_mismatch_rejected():  # TC13: kg target against m production
    pair = convert_pair(Decimal(10), resolve_unit("m"), Decimal(10), resolve_unit("kg"))
    assert pair.unit is None
    assert "DIMENSION_MISMATCH" in {i.code for i in pair.issues}


def test_unknown_unit_is_not_guessed():
    r = resolve_unit("yards")
    assert r.unit is None and "UNKNOWN_UNIT" in codes(r)


def test_conversion_precision_loss_warns():
    pair = convert_pair(Decimal("1.23456"), resolve_unit("km"), Decimal(1))
    assert pair.production_qty == Decimal("1234.560")
    assert not {i.code for i in pair.issues if i.blocking}


# --- dates -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "27/09/2026",
        "27-09-2026",
        "27.09.2026",
        "2026-09-27",
        "27 Sept 2026",
        "27 September 2026",
        "Sep 27, 2026",
        "27-Sep-2026",
        "27th Sept. 2026",
    ],
)
def test_date_formats(raw):
    r = parse_production_date(raw, TODAY)
    assert r.value == date(2026, 9, 27)
    assert not blocking(r)


def test_ambiguous_day_month_requires_review():  # TC12
    r = parse_production_date("03/04/2026", TODAY)
    assert r.value == date(2026, 4, 3)  # configured DMY proposal
    assert "AMBIGUOUS_DATE" in blocking(r)


def test_same_day_and_month_is_not_ambiguous():
    assert not blocking(parse_production_date("05/05/2026", TODAY))


def test_date_order_mismatch_flagged():
    r = parse_production_date("09/27/2026", TODAY)
    assert r.value == date(2026, 9, 27)
    assert "DATE_ORDER_MISMATCH" in blocking(r)


def test_future_and_invalid_dates_rejected():
    assert "FUTURE_DATE" in blocking(parse_production_date("29/09/2026", TODAY))
    assert "INVALID_DATE" in blocking(parse_production_date("31/02/2026", TODAY))
    assert "INVALID_DATE" in blocking(parse_production_date("yesterday", TODAY))


def test_two_digit_year_warns():
    r = parse_production_date("27-09-26", TODAY)
    assert r.value == date(2026, 9, 27) and "TWO_DIGIT_YEAR" in codes(r) and not blocking(r)


# --- stop minutes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30", 30),
        ("30 min", 30),
        ("30m", 30),
        ("1h30m", 90),
        ("1 hr 30 min", 90),
        ("1.5h", 90),
        ("0", 0),
        ("1440", 1440),
        ("2 hours", 120),
        (45, 45),
    ],
)
def test_stop_minutes(raw, expected):
    r = parse_stop_minutes(raw)
    assert r.value == expected and not r.issues


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, "MISSING_VALUE"),
        ("", "MISSING_VALUE"),
        ("1441", "OUT_OF_RANGE"),
        ("-5", "OUT_OF_RANGE"),
        ("smudged", "UNREADABLE"),
        ("1.33h", "NOT_WHOLE_MINUTES"),
    ],
)
def test_stop_minutes_invalid_never_defaults_to_zero(raw, code):  # TC11, TC14
    r = parse_stop_minutes(raw)
    assert r.value is None and code in blocking(r)


def test_clock_like_duration_needs_confirmation():
    r = parse_stop_minutes("1:30")
    assert r.value == 90 and "AMBIGUOUS_DURATION" in blocking(r)


# --- status ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Running", Status.RUNNING), ("DONE", Status.COMPLETED), ("on-hold", Status.HOLD), ("pending", Status.PENDING)],
)
def test_status_aliases(raw, expected):
    assert normalize_status(raw).value is expected


@pytest.mark.parametrize("raw", ["Machine stopped", "stopped", "", None])
def test_status_not_inferred_from_downtime(raw):
    assert normalize_status(raw).value is None
