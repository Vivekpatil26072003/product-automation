"""Authoritative formulas against the F1 golden fixture (FR12, TC13, TC14, TC24, TC57)."""

import json
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from app.domain.enums import Status, Unit
from app.domain.metrics import achievement_pct, compute_metrics, format_pct

FIXTURE = json.loads(
    (Path(__file__).parents[2] / "packages/contracts/fixtures/f1_records.json").read_text(encoding="utf-8")
)


@dataclass(frozen=True)
class Row:
    department_id: str
    unit: Unit
    production_qty: Decimal
    target_qty: Decimal
    status: Status
    stop_minutes: int


def f1_rows() -> list[Row]:
    return [
        Row(
            r["department_code"],
            Unit(FIXTURE["unit"]),
            Decimal(r["production_qty"]),
            Decimal(r["target_qty"]),
            Status(r["status"]),
            r["stop_minutes"],
        )
        for r in FIXTURE["records"]
    ]


def test_f1_totals_reconcile():
    exp = FIXTURE["expected"]
    m = compute_metrics(f1_rows())
    assert m.record_count == exp["record_count"]
    assert len(m.by_unit) == 1
    u = m.by_unit[0]
    assert u.unit is Unit.M
    assert u.production_total == Decimal(exp["production_total"])
    assert u.target_total == Decimal(exp["target_total"])
    assert format_pct(u.achievement_pct) == exp["achievement_pct_display"]
    assert u.variance == Decimal(exp["variance"])
    assert m.stop_total_minutes == exp["stop_total_minutes"]
    assert {s.value: c for s, c in m.status_counts.items()} == exp["status_counts"]
    assert {s.value: format_pct(p) for s, p in m.status_shares.items()} == {
        "RUNNING": "40.0",
        "COMPLETED": "20.0",
        "PENDING": "20.0",
        "HOLD": "20.0",
    }


def test_f1_department_achievement():
    m = compute_metrics(f1_rows())
    got = {d.department_id: format_pct(d.metrics.achievement_pct) for d in m.by_department}
    assert got == FIXTURE["expected"]["department_achievement_display"]


def test_tc57_correction_changes_totals():
    rows = [replace(r, production_qty=Decimal("1300")) if r.department_id == "TAPELINE" else r for r in f1_rows()]
    u = compute_metrics(rows).by_unit[0]
    exp = FIXTURE["expected"]["after_tc57_correction"]
    assert u.production_total == Decimal(exp["production_total"])
    assert format_pct(u.achievement_pct) == exp["achievement_pct_display"]


def test_achievement_is_ratio_of_totals_not_average_of_rows():
    rows = f1_rows()
    avg_of_rows = sum(achievement_pct(r.production_qty, r.target_qty) for r in rows) / len(rows)
    assert format_pct(avg_of_rows) != "80.5"  # 78.8 — the wrong formula gives a different answer
    assert format_pct(compute_metrics(rows).by_unit[0].achievement_pct) == "80.5"


def test_zero_target_is_na_and_over_100_allowed():  # TC14
    zero = Row("X", Unit.M, Decimal(0), Decimal(0), Status.RUNNING, 0)
    over = Row("Y", Unit.KG, Decimal(150), Decimal(100), Status.COMPLETED, 0)
    m = compute_metrics([zero, over])
    by_unit = {u.unit: u for u in m.by_unit}
    assert by_unit[Unit.M].achievement_pct is None and format_pct(None) == "N/A"
    assert format_pct(by_unit[Unit.KG].achievement_pct) == "150.0"


def test_mixed_units_never_aggregated():  # TC13
    rows = [
        Row("A", Unit.M, Decimal(10), Decimal(20), Status.RUNNING, 0),
        Row("A", Unit.KG, Decimal(5), Decimal(5), Status.RUNNING, 0),
        Row("B", Unit.PCS, Decimal(3), Decimal(4), Status.HOLD, 0),
    ]
    m = compute_metrics(rows)
    assert [u.unit for u in m.by_unit] == [Unit.M, Unit.KG, Unit.PCS]
    assert [u.production_total for u in m.by_unit] == [Decimal(10), Decimal(5), Decimal(3)]


def test_empty_scope():  # TC25/TC58 domain part
    m = compute_metrics([])
    assert m.record_count == 0 and m.by_unit == [] and m.status_shares == {}
