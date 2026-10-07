"""Excel exports (FR11, FR14; API operations 22-23).

The snapshot (rows + metrics) is taken in the request transaction with the same predicate as the records
list and dashboard, so "export row count and totals equal the selected snapshot". Rendering happens later
from that snapshot only. Access requires Reviewer or Sender and a grant for every department in scope.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, insert, select

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, forbidden, not_found
from app.db import tables as t
from app.domain.enums import Role
from app.domain.metrics import achievement_pct, format_pct
from app.domain.quantities import decimal_string
from app.jobs import ledger
from app.records import query

MAX_ROWS = 10_000
EXPORT_ROLES = (Role.REVIEWER, Role.SENDER)


def create_export(conn: Connection, principal: Principal, f: query.RecordFilter) -> dict[str, Any]:
    if not principal.has_any(*EXPORT_ROLES):
        raise forbidden()
    rows = conn.execute(
        query.selection(f).order_by(query.rev.c.production_date, query.d.c.name, query.r.c.id).limit(MAX_ROWS + 1)
    ).all()
    if len(rows) > MAX_ROWS:
        raise ApiError(413, "TOO_MANY_RECORDS", f"Exports are limited to {MAX_ROWS:,} records. Narrow the filter.")
    snapshot = []
    for row in rows:
        ach = achievement_pct(Decimal(row.production_qty), Decimal(row.target_qty))
        snapshot.append(
            {
                "record_id": str(row.record_id),
                "revision_id": str(row.revision_id),
                "revision": row.revision,
                "state": row.state,
                "production_date": row.production_date.isoformat(),
                "department_id": str(row.department_id),
                "department_name": row.department_name,
                "machine_id": str(row.machine_id),
                "machine_code": row.machine_code,
                "operator_name": row.operator_name,
                "production_qty": decimal_string(row.production_qty),
                "target_qty": decimal_string(row.target_qty),
                "unit": row.unit,
                "achievement_pct": None if ach is None else format_pct(ach),
                "status": row.status,
                "stop_minutes": row.stop_minutes,
                "remarks": row.remarks,
            }
        )
    metrics = query.aggregate(conn, f)
    tenant = conn.execute(
        select(t.tenant.c.data_version, t.tenant.c.timezone).where(t.tenant.c.id == principal.tenant_id)
    ).one()
    export_id = uuid.uuid4()
    conn.execute(
        insert(t.export).values(
            id=export_id,
            tenant_id=principal.tenant_id,
            requested_by=principal.membership_id,
            filter_json=f.as_json(),
            snapshot_json=snapshot,
            metrics_json=metrics,
            data_version=tenant.data_version,
            timezone=tenant.timezone,
            row_count=len(snapshot),
        )
    )
    job_id = ledger.create_job(
        conn,
        tenant_id=principal.tenant_id,
        kind="export.render",
        object_id=export_id,
        created_by=principal.membership_id,
    )
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="EXPORT_REQUESTED",
        object_type="export",
        object_id=export_id,
        after={"rows": len(snapshot), "filter": f.as_json()},
    )
    return {"export_id": str(export_id), "job_id": str(job_id), "state": "QUEUED", "row_count": len(snapshot)}


def load_export(conn: Connection, principal: Principal, export_id: uuid.UUID) -> Any:
    row = conn.execute(select(t.export).where(t.export.c.id == export_id)).one_or_none()
    if row is None or not principal.has_any(*EXPORT_ROLES):
        raise not_found()
    scope = {uuid.UUID(x) for x in row.filter_json["department_ids"]}
    if not principal.can_access_all(scope):  # a revoked grant revokes access to old exports too
        raise not_found()
    return row


def export_view(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "state": row.state,
        "row_count": row.row_count,
        "sha256": row.sha256,
        "bytes": row.bytes,
        "filter": row.filter_json,
        "data_version": row.data_version,
        "created_at": row.created_at.isoformat(),
        "error_code": row.error_code,
    }


def create_report_export(conn: Connection, principal: Principal, report_id: uuid.UUID) -> dict[str, Any]:
    """Excel snapshot of a report: its items and metrics, never live rows. Reuses an existing export."""
    from app.reports import service as reports

    if not principal.has_any(*EXPORT_ROLES):
        raise forbidden()
    report = reports.load_report(conn, principal, report_id)
    existing = conn.execute(
        select(t.export)
        .where(t.export.c.report_id == report.id, t.export.c.state != "FAILED")
        .order_by(t.export.c.created_at.desc())
        .limit(1)
    ).one_or_none()
    if existing is not None:
        return {"export_id": str(existing.id), "job_id": None, "state": existing.state, "row_count": existing.row_count}
    snapshot = reports.export_snapshot(conn, report)
    export_id = uuid.uuid4()
    conn.execute(
        insert(t.export).values(
            id=export_id,
            tenant_id=principal.tenant_id,
            requested_by=principal.membership_id,
            filter_json=report.filter_json | {"report_id": str(report.id), "report_version": report.version},
            snapshot_json=snapshot,
            metrics_json=report.metrics_json,
            data_version=report.data_version,
            timezone=report.timezone,
            row_count=len(snapshot),
            report_id=report.id,
        )
    )
    job_id = ledger.create_job(
        conn,
        tenant_id=principal.tenant_id,
        kind="export.render",
        object_id=export_id,
        created_by=principal.membership_id,
    )
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="EXPORT_REQUESTED",
        object_type="export",
        object_id=export_id,
        after={"rows": len(snapshot), "report_id": str(report.id)},
    )
    return {"export_id": str(export_id), "job_id": str(job_id), "state": "QUEUED", "row_count": len(snapshot)}
