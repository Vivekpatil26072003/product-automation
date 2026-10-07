"""One filter and one predicate for every view of approved production data (FR11, FR12, FR14).

The records list, dashboard, drill-down, Excel export and (from M6) reports all select rows through
`selection()`, so they can never disagree about which records are "in scope":
current approved revision only, ACTIVE records unless archived ones are explicitly included, and only
departments the caller is granted. Filters narrow scope; they never broaden it (spec §5, §13).
"""

import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, false, func, or_, select, tuple_

from app.auth.principal import Principal
from app.core.errors import ApiError, Issue, forbidden, validation_failed
from app.db import tables as t
from app.domain.enums import Status, Unit
from app.domain.metrics import achievement_pct, format_pct
from app.domain.quantities import decimal_string

MAX_RANGE_DAYS = 366
DEFAULT_DAYS = 7
r, rev, d, m = t.production_record, t.record_revision, t.department, t.machine


@dataclass(frozen=True)
class RecordFilter:
    date_from: date
    date_to: date
    department_ids: tuple[uuid.UUID, ...]
    machine_ids: tuple[uuid.UUID, ...] = ()
    operator_query: str | None = None
    statuses: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    include_archived: bool = False
    q: str | None = None

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["date_from"], data["date_to"] = self.date_from.isoformat(), self.date_to.isoformat()
        data["department_ids"] = [str(x) for x in self.department_ids]
        data["machine_ids"] = [str(x) for x in self.machine_ids]
        data["statuses"], data["units"] = list(self.statuses), list(self.units)
        return data


def local_today(timezone: str) -> date:
    return datetime.now(ZoneInfo(timezone)).date()


def build_filter(
    principal: Principal,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    department_ids: Iterable[uuid.UUID] = (),
    machine_ids: Iterable[uuid.UUID] = (),
    operator_query: str | None = None,
    statuses: Iterable[str] = (),
    units: Iterable[str] = (),
    include_archived: bool = False,
    q: str | None = None,
) -> RecordFilter:
    """Validate a filter against the caller's grants. Default: the last 7 local days including today."""
    today = local_today(principal.timezone)
    date_to = date_to or today
    date_from = date_from or (date_to - timedelta(days=DEFAULT_DAYS - 1))
    issues: list[Issue] = []
    if date_from > date_to:
        issues.append(Issue("DATE_RANGE", "The start date must be on or before the end date.", "date_from"))
    elif (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        issues.append(Issue("DATE_RANGE", f"Choose at most {MAX_RANGE_DAYS} days.", "date_to"))
    statuses, units = tuple(sorted(set(statuses))), tuple(sorted(set(units)))
    if bad := [s for s in statuses if s not in {x.value for x in Status}]:
        issues.append(Issue("UNKNOWN_STATUS", f"Unknown status {bad[0]}.", "statuses"))
    if bad := [u for u in units if u not in {x.value for x in Unit}]:
        issues.append(Issue("UNKNOWN_UNIT", f"Unknown unit {bad[0]}.", "units"))
    for name, value in (("operator_query", operator_query), ("q", q)):
        if value is not None and len(value) > 120:
            issues.append(Issue("TOO_LONG", "Search text must be at most 120 characters.", name))
    if issues:
        raise validation_failed(issues)

    requested = set(department_ids)
    if requested - principal.department_ids:
        raise forbidden("You do not have access to one or more selected departments.")
    scope = requested or set(principal.department_ids)
    return RecordFilter(
        date_from=date_from,
        date_to=date_to,
        department_ids=tuple(sorted(scope, key=str)),
        machine_ids=tuple(sorted(set(machine_ids), key=str)),
        operator_query=(operator_query or "").strip() or None,
        statuses=statuses,
        units=units,
        include_archived=include_archived,
        q=(q or "").strip() or None,
    )


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def conditions(f: RecordFilter) -> list:
    conds = [
        r.c.production_date.between(f.date_from, f.date_to),
        r.c.department_id.in_(f.department_ids) if f.department_ids else false(),
    ]
    if not f.include_archived:
        conds.append(r.c.state == "ACTIVE")
    if f.machine_ids:
        conds.append(rev.c.machine_id.in_(f.machine_ids))
    if f.statuses:
        conds.append(rev.c.status.in_(f.statuses))
    if f.units:
        conds.append(rev.c.unit.in_(f.units))
    if f.operator_query:
        conds.append(rev.c.operator_name.ilike(_like(f.operator_query), escape="\\"))
    if f.q:
        pattern = _like(f.q)
        conds.append(
            or_(
                rev.c.operator_name.ilike(pattern, escape="\\"),
                m.c.code.ilike(pattern, escape="\\"),
                rev.c.remarks.ilike(pattern, escape="\\"),
            )
        )
    return conds


def selection(f: RecordFilter) -> Select:
    """Rows in scope: record + its current approved revision + display names."""
    return (
        select(
            r.c.id.label("record_id"),
            r.c.state,
            r.c.created_at,
            rev.c.id.label("revision_id"),
            rev.c.number.label("revision"),
            rev.c.production_date,
            rev.c.department_id,
            d.c.name.label("department_name"),
            rev.c.machine_id,
            m.c.code.label("machine_code"),
            rev.c.operator_name,
            rev.c.production_qty,
            rev.c.target_qty,
            rev.c.unit,
            rev.c.status,
            rev.c.stop_minutes,
            rev.c.remarks,
        )
        .select_from(r)
        .join(rev, rev.c.id == r.c.current_revision_id)
        .join(d, d.c.id == rev.c.department_id)
        .join(m, m.c.id == rev.c.machine_id)
        .where(and_(*conditions(f)))
    )


def row_json(row: Any) -> dict[str, Any]:
    ach = achievement_pct(Decimal(row.production_qty), Decimal(row.target_qty))
    return {
        "id": str(row.record_id),
        "state": row.state,
        "revision": row.revision,
        "production_date": row.production_date.isoformat(),
        "department": {"id": str(row.department_id), "name": row.department_name},
        "machine": {"id": str(row.machine_id), "code": row.machine_code},
        "operator_name": row.operator_name,
        "production_qty": decimal_string(row.production_qty),
        "target_qty": decimal_string(row.target_qty),
        "unit": row.unit,
        "achievement_pct": None if ach is None else format_pct(ach),
        "status": row.status,
        "stop_minutes": row.stop_minutes,
        "remarks": row.remarks,
        # Google Sheets projection arrives in M5; until then no record is synced anywhere.
        "sync_state": "NOT_CONFIGURED",
    }


# --- keyset paging ---------------------------------------------------------------------------

SORTS = {
    "date_desc": (lambda: rev.c.production_date, True),
    "date_asc": (lambda: rev.c.production_date, False),
    "department": (lambda: d.c.name, False),
    "production_desc": (lambda: rev.c.production_qty, True),
}


def paged(f: RecordFilter, sort: str, cursor: list | None, size: int) -> Select:
    if sort not in SORTS:
        raise ApiError(400, "BAD_SORT", f"Sort must be one of {', '.join(SORTS)}.")
    if sort == "production_desc" and len(f.units) != 1:
        raise ApiError(400, "UNIT_REQUIRED", "Choose exactly one unit to sort by quantity (m, kg and pcs differ).")
    column, descending = SORTS[sort][0](), SORTS[sort][1]
    q = selection(f)
    if cursor is not None:
        value, last_id = cursor
        value = {"date_desc": date.fromisoformat, "date_asc": date.fromisoformat, "production_desc": Decimal}.get(
            sort, str
        )(value)
        key = tuple_(column, r.c.id)
        q = q.where(key < tuple_(value, uuid.UUID(last_id)) if descending else key > tuple_(value, uuid.UUID(last_id)))
    order = (column.desc(), r.c.id.desc()) if descending else (column.asc(), r.c.id.asc())
    return q.order_by(*order).limit(size + 1)


def cursor_value(sort: str, row: Any) -> list:
    value = {
        "date_desc": row.production_date,
        "date_asc": row.production_date,
        "department": row.department_name,
        "production_desc": row.production_qty,
    }[sort]
    return [value.isoformat() if isinstance(value, date) else str(value), str(row.record_id)]


# --- aggregates (FR12): one statement, one snapshot ------------------------------------------


def aggregate(conn, f: RecordFilter) -> dict[str, Any]:
    """Totals per unit, per department+unit and per status in a single GROUPING SETS statement, so
    every panel of the dashboard comes from the same database snapshot. Sums are exact numerics."""
    sq = selection(f).subquery()
    g = func.grouping(sq.c.unit, sq.c.department_id, sq.c.status)
    rows = conn.execute(
        select(
            sq.c.unit,
            sq.c.department_id,
            sq.c.status,
            g.label("g"),
            func.sum(sq.c.production_qty).label("prod"),
            func.sum(sq.c.target_qty).label("target"),
            func.count().label("n"),
            func.sum(sq.c.stop_minutes).label("stop"),
        ).group_by(
            func.grouping_sets(tuple_(sq.c.unit), tuple_(sq.c.department_id, sq.c.unit), tuple_(sq.c.status), tuple_())
        )
    ).all()
    names = (
        dict(conn.execute(select(d.c.id, d.c.name).where(d.c.id.in_(f.department_ids))).all())
        if f.department_ids
        else {}
    )
    order = {"m": 0, "kg": 1, "pcs": 2}

    def metric(row: Any) -> dict[str, Any]:
        prod, target = Decimal(row.prod or 0), Decimal(row.target or 0)
        ach = achievement_pct(prod, target)
        return {
            "unit": row.unit,
            "production_qty": decimal_string(prod),
            "target_qty": decimal_string(target),
            "achievement_pct": None if ach is None else format_pct(ach),
            "variance": decimal_string(prod - target),
            "record_count": row.n,
        }

    by_unit = sorted((metric(x) for x in rows if x.g == 0b011), key=lambda x: order[x["unit"]])
    departments = sorted(
        (
            {"department_id": str(x.department_id), "department_name": names.get(x.department_id)} | metric(x)
            for x in rows
            if x.g == 0b001
        ),
        key=lambda x: (x["department_name"] or "", order[x["unit"]]),
    )
    total = next((x for x in rows if x.g == 0b111), None)
    count = total.n if total else 0
    status_counts = {s.value: 0 for s in Status}
    for x in rows:
        if x.g == 0b110:
            status_counts[x.status] = x.n
    shares = {s: (None if not count else format_pct(Decimal(100) * n / count)) for s, n in status_counts.items()}
    return {
        "record_count": count,
        "metrics": by_unit,
        "departments": departments,
        "status_counts": status_counts,
        "status_shares": shares,
        # Record downtime minutes: may overlap across machines; not plant downtime, not OEE (spec §4).
        "stop_total_minutes": int(total.stop or 0) if total else 0,
    }
