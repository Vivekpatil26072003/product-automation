"""Authoritative production formulas (spec §4). Used by dashboard, reports, PDF and email alike.

- Quantities are aggregated per unit only; m, kg and pcs are never summed together.
- Achievement is the ratio of totals (never an average of row percentages); N/A when target is 0.
- Calculations use unrounded canonical Decimals; only display values are rounded.
- stop_total is "record downtime minutes". It is not plant downtime and feeds no OEE figure.
"""

from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.domain.enums import Status, Unit

_UNIT_ORDER = {Unit.M: 0, Unit.KG: 1, Unit.PCS: 2}
_PCT_DISPLAY = Decimal("0.1")


class RecordLike(Protocol):
    department_id: Hashable
    unit: Unit | str
    production_qty: Decimal
    target_qty: Decimal
    status: Status | str
    stop_minutes: int


@dataclass(frozen=True)
class UnitMetrics:
    unit: Unit
    production_total: Decimal
    target_total: Decimal
    achievement_pct: Decimal | None  # unrounded; None means N/A
    variance: Decimal
    record_count: int


@dataclass(frozen=True)
class DepartmentMetrics:
    department_id: Hashable
    metrics: UnitMetrics


@dataclass(frozen=True)
class Metrics:
    record_count: int
    by_unit: list[UnitMetrics]
    by_department: list[DepartmentMetrics]
    status_counts: dict[Status, int]
    status_shares: dict[Status, Decimal]  # unrounded percentages
    stop_total_minutes: int


def achievement_pct(production_total: Decimal, target_total: Decimal) -> Decimal | None:
    if target_total == 0:
        return None
    return Decimal(100) * production_total / target_total


def format_pct(value: Decimal | None) -> str:
    """One decimal, rounded half up; "N/A" when undefined."""
    return "N/A" if value is None else format(value.quantize(_PCT_DISPLAY, rounding=ROUND_HALF_UP), "f")


def _unit_metrics(unit: Unit, rows: list[RecordLike]) -> UnitMetrics:
    prod = sum((r.production_qty for r in rows), Decimal(0))
    targ = sum((r.target_qty for r in rows), Decimal(0))
    return UnitMetrics(unit, prod, targ, achievement_pct(prod, targ), prod - targ, len(rows))


def compute_metrics(records: Iterable[RecordLike]) -> Metrics:
    rows = list(records)
    by_unit: dict[Unit, list[RecordLike]] = defaultdict(list)
    by_dept: dict[tuple[Hashable, Unit], list[RecordLike]] = defaultdict(list)
    for r in rows:
        unit = Unit(r.unit)
        by_unit[unit].append(r)
        by_dept[(r.department_id, unit)].append(r)

    counts = Counter(Status(r.status) for r in rows)
    status_counts = {s: counts.get(s, 0) for s in Status}
    total = len(rows)
    shares = {s: Decimal(100) * c / total for s, c in status_counts.items()} if total else {}

    return Metrics(
        record_count=total,
        by_unit=[_unit_metrics(u, by_unit[u]) for u in sorted(by_unit, key=_UNIT_ORDER.__getitem__)],
        by_department=[
            DepartmentMetrics(dept, _unit_metrics(unit, group))
            for (dept, unit), group in sorted(by_dept.items(), key=lambda kv: (str(kv[0][0]), _UNIT_ORDER[kv[0][1]]))
        ],
        status_counts=status_counts,
        status_shares=shares,
        stop_total_minutes=sum(r.stop_minutes for r in rows),
    )
