"""Report snapshots, versions and outdating (FR16, FR18; API operations 24-28).

Snapshot: in ONE repeatable-read transaction the request reads the approved records in scope (the same
predicate as the records list and dashboard), writes report_item rows with each record's revision and a
canonical copy of its fields, computes all metrics from those items, stores facts and commits. Rendering
happens later from that snapshot only, so a retry after a failure produces the same report.

Outdated: a report is current only while the approved records matching its frozen filter are exactly its
items at the same revisions. Corrections, archives and new records inside the filter all break that
(filter intersection, not only item membership). Outdated reports stay readable; sending them is blocked.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Connection, and_, cast, func, insert, select, update
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, conflict, forbidden, not_found
from app.db import tables as t
from app.domain.enums import Role, Status
from app.domain.metrics import compute_metrics, format_pct
from app.domain.quantities import decimal_string
from app.jobs import ledger
from app.records import query
from app.reports.facts import TEMPLATE_VERSION, build_facts

MAX_RECORDS = 10_000
REPORT_ROLES = (Role.REVIEWER, Role.SENDER)
RENDER_KIND = "report.render"
rp, ri = t.report, t.report_item
_UNIT_ORDER = {"m": 0, "kg": 1, "pcs": 2}


def code_for(series_id: uuid.UUID) -> str:
    """Short, stable report code shown to people and used in the attachment name."""
    return "P" + series_id.hex[:6].upper()


def filter_from_report(row: Any) -> query.RecordFilter:
    f = row.filter_json
    return query.RecordFilter(
        date_from=date.fromisoformat(f["date_from"]),
        date_to=date.fromisoformat(f["date_to"]),
        department_ids=tuple(uuid.UUID(x) for x in f["department_ids"]),
        machine_ids=tuple(uuid.UUID(x) for x in f.get("machine_ids", [])),
        operator_query=f.get("operator_query"),
        statuses=tuple(f.get("statuses", [])),
        units=tuple(f.get("units", [])),
        include_archived=False,
        q=f.get("q"),
    )


def _item_fields(row: Any) -> dict[str, Any]:
    return {
        "production_date": row.production_date.isoformat(),
        "department_id": str(row.department_id),
        "department_name": row.department_name,
        "machine_id": str(row.machine_id),
        "machine_code": row.machine_code,
        "operator_name": row.operator_name,
        "production_qty": decimal_string(row.production_qty),
        "target_qty": decimal_string(row.target_qty),
        "unit": row.unit,
        "status": row.status,
        "stop_minutes": row.stop_minutes,
        "remarks": row.remarks,
    }


def metrics_from_items(fields: list[dict[str, Any]], names: dict[str, str]) -> dict[str, Any]:
    """The dashboard's metrics shape, computed with the domain formulas from the snapshot items only."""
    rows = [
        SimpleNamespace(
            department_id=f["department_id"],
            unit=f["unit"],
            production_qty=Decimal(f["production_qty"]),
            target_qty=Decimal(f["target_qty"]),
            status=f["status"],
            stop_minutes=f["stop_minutes"],
        )
        for f in fields
    ]
    m = compute_metrics(rows)

    def unit_json(u: Any) -> dict[str, Any]:
        return {
            "unit": u.unit.value,
            "production_qty": decimal_string(u.production_total),
            "target_qty": decimal_string(u.target_total),
            "achievement_pct": None if u.achievement_pct is None else format_pct(u.achievement_pct),
            "variance": decimal_string(u.variance),
            "record_count": u.record_count,
        }

    departments = sorted(
        (
            {"department_id": str(d.department_id), "department_name": names.get(str(d.department_id))}
            | unit_json(d.metrics)
            for d in m.by_department
        ),
        key=lambda x: (x["department_name"] or "", _UNIT_ORDER[x["unit"]]),
    )
    counts = {s.value: m.status_counts.get(s, 0) for s in Status}
    return {
        "record_count": m.record_count,
        "metrics": [unit_json(u) for u in m.by_unit],
        "departments": departments,
        "status_counts": counts,
        "status_shares": {
            s: (None if not m.record_count else format_pct(Decimal(100) * n / m.record_count))
            for s, n in counts.items()
        },
        "stop_total_minutes": m.stop_total_minutes,
    }


def _pending_in_scope(conn: Connection, f: query.RecordFilter) -> int:
    """Entries still waiting for review whose date and department fall inside the report (excluded drafts)."""
    c = t.candidate
    day = c.c.fields["production_date"]["value"].astext
    dept = c.c.fields["department_id"]["value"].astext
    return conn.execute(
        select(func.count()).where(
            c.c.state == "NEEDS_REVIEW",
            day >= f.date_from.isoformat(),
            day <= f.date_to.isoformat(),
            dept.in_([str(x) for x in f.department_ids]),
        )
    ).scalar_one()


def create_report(
    conn: Connection,
    principal: Principal,
    f: query.RecordFilter,
    *,
    title: str,
    include_detail: bool,
    allow_empty: bool,
    supersedes: uuid.UUID | None,
) -> dict[str, Any]:
    """Must run in a REPEATABLE READ transaction (tenant_tx(..., isolation="REPEATABLE READ"))."""
    if not principal.has_any(*REPORT_ROLES):
        raise forbidden()
    series_id, version = uuid.uuid4(), 1
    if supersedes is not None:
        old = load_report(conn, principal, supersedes)
        series_id = old.series_id
        version = conn.execute(select(func.max(rp.c.version)).where(rp.c.series_id == old.series_id)).scalar_one() + 1

    rows = conn.execute(
        query.selection(f)
        .order_by(query.rev.c.production_date, query.d.c.name, query.m.c.code, query.r.c.id)
        .limit(MAX_RECORDS + 1)
    ).all()
    if len(rows) > MAX_RECORDS:
        raise ApiError(413, "TOO_MANY_RECORDS", f"Reports are limited to {MAX_RECORDS:,} records. Narrow the filter.")
    if not rows and not allow_empty:
        raise conflict("EMPTY_PERIOD", "No approved records match this report. Confirm to create an empty report.")

    tenant = conn.execute(
        select(t.tenant.c.data_version, t.tenant.c.timezone).where(t.tenant.c.id == principal.tenant_id)
    ).one()
    names = dict(
        conn.execute(
            select(t.department.c.id, t.department.c.name).where(t.department.c.id.in_(f.department_ids))
        ).all()
    )
    fields = [_item_fields(x) for x in rows]
    metrics = metrics_from_items(fields, {str(k): v for k, v in names.items()})
    facts = build_facts(
        date_from=f.date_from,
        date_to=f.date_to,
        timezone=tenant.timezone,
        metrics=metrics,
        department_count=len({x["department_id"] for x in fields}),
        excluded_pending=_pending_in_scope(conn, f),
    )
    report_id = uuid.uuid4()
    conn.execute(
        insert(rp).values(
            id=report_id,
            tenant_id=principal.tenant_id,
            series_id=series_id,
            version=version,
            supersedes_id=supersedes,
            title=title,
            filter_json=f.as_json(),
            date_from=f.date_from,
            date_to=f.date_to,
            department_ids=list(f.department_ids),
            timezone=tenant.timezone,
            data_version=tenant.data_version,
            record_count=len(rows),
            include_detail=include_detail,
            is_empty=not rows,
            metrics_json=metrics,
            facts_json=facts,
            template_version=TEMPLATE_VERSION,
            created_by=principal.membership_id,
        )
    )
    if rows:
        conn.execute(
            insert(ri),
            [
                {
                    "tenant_id": principal.tenant_id,
                    "report_id": report_id,
                    "record_id": x.record_id,
                    "revision_id": x.revision_id,
                    "revision_number": x.revision,
                    "position": n,
                    "fields": fields[n],
                }
                for n, x in enumerate(rows)
            ],
        )
    job_id = ledger.create_job(
        conn, tenant_id=principal.tenant_id, kind=RENDER_KIND, object_id=report_id, created_by=principal.membership_id
    )
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="REPORT_REQUESTED",
        object_type="report",
        object_id=report_id,
        object_revision=version,
        after={
            "records": len(rows),
            "filter": f.as_json(),
            "supersedes": str(supersedes) if supersedes else None,
            "empty": not rows,
        },
    )
    return {
        "report_id": str(report_id),
        "job_id": str(job_id),
        "state": "QUEUED",
        "status_url": f"/api/v1/jobs/{job_id}",
    }


# --- reading ------------------------------------------------------------------------------------------


def load_report(conn: Connection, principal: Principal, report_id: uuid.UUID, lock: bool = False) -> Any:
    q = select(rp).where(rp.c.id == report_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None or not principal.has_any(*REPORT_ROLES):
        raise not_found()
    if not principal.can_access_all(set(row.department_ids)):  # a revoked grant revokes old reports too
        raise not_found()
    return row


def is_current(conn: Connection, row: Any) -> bool:
    """True while the approved records matching the frozen filter are exactly the items, same revisions."""
    if row.outdated_at is not None:
        return False
    sq = query.selection(filter_from_report(row)).subquery()
    current = set(conn.execute(select(sq.c.record_id, sq.c.revision_id)).all())
    items = set(conn.execute(select(ri.c.record_id, ri.c.revision_id).where(ri.c.report_id == row.id)).all())
    return current == items


def mark_outdated(conn: Connection, row: Any, reason: str) -> bool:
    done = conn.execute(
        update(rp)
        .where(rp.c.id == row.id, rp.c.outdated_at.is_(None))
        .values(outdated_at=func.now(), outdated_reason=reason)
        .returning(rp.c.id)
    ).first()
    if done:
        audit.record(
            conn,
            tenant_id=row.tenant_id,
            actor=audit.Actor("service", None),
            action="REPORT_OUTDATED",
            object_type="report",
            object_id=row.id,
            object_revision=row.version,
            reason=reason,
        )
    return done is not None


def invalidate_for_record(conn: Connection, record_id: uuid.UUID) -> int:
    """Called for every production_record.changed event. Returns the number of reports marked outdated."""
    rec = conn.execute(
        select(query.r.c.department_id, query.r.c.production_date, query.rev.c.production_date.label("rev_date"))
        .join(query.rev, query.rev.c.id == query.r.c.current_revision_id)
        .where(query.r.c.id == record_id)
    ).one_or_none()
    in_items = select(ri.c.report_id).where(ri.c.record_id == record_id)
    conds = [rp.c.id.in_(in_items)]
    if rec is not None:
        day = rec.rev_date or rec.production_date
        conds.append(
            and_(
                rp.c.date_from <= day,
                rp.c.date_to >= day,
                rp.c.department_ids.op("@>")(cast([rec.department_id], ARRAY(UUID(as_uuid=True)))),
            )
        )
    candidates = conn.execute(
        select(rp).where(
            rp.c.outdated_at.is_(None), rp.c.state != "FAILED", conds[0] if len(conds) == 1 else (conds[0] | conds[1])
        )
    ).all()
    n = 0
    for row in candidates:
        if not is_current(conn, row):
            n += mark_outdated(conn, row, "RECORDS_CHANGED")
    return n


def summary_of(row: Any) -> list[dict[str, Any]]:
    return (row.narrative_json or {}).get("sentences", [])


def report_view(row: Any, *, current: bool | None = None, creator: str | None = None) -> dict[str, Any]:
    outdated = row.outdated_at is not None or current is False
    return {
        "id": str(row.id),
        "code": code_for(row.series_id),
        "series_id": str(row.series_id),
        "version": row.version,
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
        "title": row.title,
        "filter": row.filter_json,
        "timezone": row.timezone,
        "data_version": row.data_version,
        "record_count": row.record_count,
        "include_detail": row.include_detail,
        "is_empty": row.is_empty,
        "state": row.state,
        "outdated": outdated,
        "outdated_at": row.outdated_at.isoformat() if row.outdated_at else None,
        "metrics": row.metrics_json,
        "facts": row.facts_json,
        "summary": summary_of(row),
        "summary_source": row.narrative_source,
        "summary_fallback_reason": row.narrative_fallback_reason,
        "file": {"sha256": row.sha256, "bytes": row.bytes} if row.state == "READY" else None,
        "error": {"code": row.error_code, "message": row.error_message} if row.error_code else None,
        "created_at": row.created_at.isoformat(),
        "ready_at": row.ready_at.isoformat() if row.ready_at else None,
        "created_by": creator,
    }


def render_input(conn: Connection, row: Any, summary: list[dict[str, Any]], source: str) -> dict[str, Any]:
    names = dict(
        conn.execute(
            select(t.department.c.id, t.department.c.name).where(t.department.c.id.in_(row.department_ids))
        ).all()
    )
    items = (
        conn.execute(select(ri).where(ri.c.report_id == row.id).order_by(ri.c.position)).all()
        if row.include_detail
        else []
    )
    snapshot_at = row.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "id": str(row.id),
        "code": code_for(row.series_id),
        "version": row.version,
        "title": row.title,
        "date_from": row.date_from.isoformat(),
        "date_to": row.date_to.isoformat(),
        "timezone": row.timezone,
        "snapshot_at": snapshot_at,
        "data_version": row.data_version,
        "departments": sorted(names.get(d, "?") for d in row.department_ids),
        "record_count": row.record_count,
        "metrics": row.metrics_json,
        "facts": row.facts_json,
        "summary": summary,
        "summary_source": source,
        "include_detail": row.include_detail,
        "items": [{"fields": x.fields} for x in items],
        "excluded_pending": row.facts_json["excluded_pending"],
    }


def export_snapshot(conn: Connection, row: Any) -> list[dict[str, Any]]:
    """Report items in the export row shape, so the Excel snapshot and the PDF share one snapshot ID."""
    out = []
    for x in conn.execute(select(ri).where(ri.c.report_id == row.id).order_by(ri.c.position)).all():
        f = x.fields
        ach = (
            compute_metrics(
                [
                    SimpleNamespace(
                        department_id=0,
                        unit=f["unit"],
                        status=f["status"],
                        stop_minutes=0,
                        production_qty=Decimal(f["production_qty"]),
                        target_qty=Decimal(f["target_qty"]),
                    )
                ]
            )
            .by_unit[0]
            .achievement_pct
        )
        out.append(
            {
                "record_id": str(x.record_id),
                "revision_id": str(x.revision_id),
                "revision": x.revision_number,
                "state": "ACTIVE",
                **f,
                "achievement_pct": None if ach is None else format_pct(ach),
            }
        )
    return out


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
