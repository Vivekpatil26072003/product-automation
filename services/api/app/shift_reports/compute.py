"""Calculated cells of the daily sheet: derived rows, the Total column and To date (rules in catalog.py).

A calculated cell is None when an input it needs is missing (never a guess), and a division by zero gives None.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.shift_reports.catalog import SECTIONS, Section

Values = dict[tuple[str, str, str], Decimal | None]
HOURS = Decimal(8)  # hours per shift


def _div(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _mul(*xs: Decimal | None) -> Decimal | None:
    out = Decimal(1)
    for x in xs:
        if x is None:
            return None
        out *= x
    return out


def _pct(x: Decimal | None) -> Decimal | None:
    return None if x is None else x * 100


def _loom_cell(metric: str, get, shift: str, p: dict[str, Decimal]) -> Decimal | None:
    running, picks = get("running_looms", shift), get("picks", shift)
    if metric == "theoretical_picks":
        return _mul(running, p["theo_rate"], HOURS)
    if metric == "loss_of_pick":
        theo = _mul(running, p["theo_rate"], HOURS)
        return None if theo is None or picks is None else theo - picks
    if metric == "utilization_pct":
        return _pct(_div(running, p["installed"]))
    if metric == "working_pct":
        return _pct(_div(picks, _mul(running, p["rate"], HOURS)))
    if metric == "total_eff_pct":
        return _pct(_div(picks, _mul(p["installed"], p["rate"], HOURS)))
    if metric == "picks_per_hour":
        return _div(picks, _mul(running, HOURS))
    return None


def section_grid(s: Section, values: Values, params: dict[str, Decimal]) -> dict[str, dict[str, Decimal | None]]:
    """{metric: {shift: value, ..., "total": value}} for one section."""
    grid: dict[str, dict[str, Decimal | None]] = {}

    def get(metric: str, shift: str) -> Decimal | None:
        return values.get((s.key, metric, shift))

    for m in s.metrics:
        row: dict[str, Decimal | None] = {}
        for sh in s.shifts:
            if m.kind == "input":
                row[sh] = get(m.key, sh)
            elif m.key == "total":
                parts = [grid[x.key][sh] for x in s.metrics if x.key != "total"]
                known = [v for v in parts if v is not None]
                row[sh] = sum(known, Decimal(0)) if known else None
            elif m.key == "meters_per_min":
                row[sh] = _div(get("meters", sh), _mul(get("working_hours", sh), Decimal(60)))
            else:
                row[sh] = _loom_cell(m.key, get, sh, params)
        shift_vals = [row[sh] for sh in s.shifts]
        if len(s.shifts) == 1:
            row["total"] = shift_vals[0]
        elif all(v is None for v in shift_vals):
            row["total"] = None
        elif m.agg == "avg":
            # The sheet averages the three shifts; a missing shift makes the average unknown, not lower.
            row["total"] = None if any(v is None for v in shift_vals) else sum(shift_vals, Decimal(0)) / len(shift_vals)
        else:
            row["total"] = sum((v for v in shift_vals if v is not None), Decimal(0))
        grid[m.key] = row
    return grid


def all_grids(values: Values, params: dict[str, dict[str, Decimal]]) -> dict[str, dict[str, dict[str, Any]]]:
    return {s.key: section_grid(s, values, params[s.key]) for s in SECTIONS}


def to_date(today_total: Decimal | None, earlier_totals: list[Decimal | None]) -> Decimal | None:
    """Average of the daily totals so far this month (days without a value are left out)."""
    known = [v for v in [*earlier_totals, today_total] if v is not None]
    return sum(known, Decimal(0)) / len(known) if known else None


def rounded(v: Decimal | None, places: int = 2) -> str:
    if v is None:
        return ""
    q = Decimal(1).scaleb(-places)
    return f"{v.quantize(q, rounding=ROUND_HALF_UP):,.{places}f}"
