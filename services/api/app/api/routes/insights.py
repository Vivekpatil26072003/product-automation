"""API operations 16, 21-23 and the control tower: records list, dashboard, exports (FR11, FR12, FR14, A9)."""

import base64
import json
import uuid
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.deps import idempotency_key, require
from app.audit import service as audit
from app.auth.principal import Principal
from app.control.service import control_tower
from app.core.errors import ApiError
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import tenant_tx
from app.domain.enums import Role
from app.exports import service as exports
from app.integrations import service as integ
from app.records import query
from app.storage.objects import get_storage

router = APIRouter(tags=["records"])


class FilterParams(BaseModel):
    """Shared filter (spec §10 ReportFilter). Empty department_ids means all granted departments."""

    model_config = ConfigDict(extra="forbid")
    date_from: date | None = None
    date_to: date | None = None
    department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    machine_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    operator_query: str | None = Field(default=None, max_length=120)
    statuses: list[str] = Field(default_factory=list, max_length=4)
    units: list[str] = Field(default_factory=list, max_length=3)
    include_archived: bool = False
    q: str | None = Field(default=None, max_length=120)


def filter_from_query(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    department_id: list[uuid.UUID] = Query(default=[]),
    machine_id: list[uuid.UUID] = Query(default=[]),
    operator: str | None = Query(default=None, max_length=120),
    status: list[str] = Query(default=[]),
    unit: list[str] = Query(default=[]),
    include_archived: bool = Query(default=False),
    q: str | None = Query(default=None, max_length=120),
) -> FilterParams:
    return FilterParams(
        date_from=date_from,
        date_to=date_to,
        department_ids=department_id,
        machine_ids=machine_id,
        operator_query=operator,
        statuses=status,
        units=unit,
        include_archived=include_archived,
        q=q,
    )


def resolve(principal: Principal, p: FilterParams) -> query.RecordFilter:
    return query.build_filter(principal, **p.model_dump())


def _cursor(raw: str | None) -> list | None:
    if not raw:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(raw.encode()))
        if not (isinstance(value, list) and len(value) == 2):
            raise ValueError
        uuid.UUID(value[1])
        return value
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "BAD_CURSOR", "The page cursor is invalid. Reload the list.") from exc


def _with_sync(row: dict, sheets: bool, states: dict[str, str]) -> dict:
    """Google Sheets state of each record: NOT_CONFIGURED without a connection, else the ledger state."""
    if sheets:
        row["sync_state"] = states.get(row["id"], "NOT_SYNCED")
    return row


def _data_version(conn, principal: Principal) -> int:
    return conn.execute(select(t.tenant.c.data_version).where(t.tenant.c.id == principal.tenant_id)).scalar_one()


@router.get("/records", summary="Approved records in scope, newest first (cursor paging)")
def list_records(
    params: FilterParams = Depends(filter_from_query),
    sort: str = Query(default="date_desc"),
    cursor: str | None = Query(default=None, max_length=500),
    size: int = Query(default=25),
    principal: Principal = Depends(require()),
) -> dict:
    if size not in (25, 50, 100):
        raise ApiError(400, "BAD_PAGE_SIZE", "Page size must be 25, 50 or 100.")
    f = resolve(principal, params)
    with tenant_tx(principal.tenant_id) as conn:
        rows = conn.execute(query.paged(f, sort, _cursor(cursor), size)).all()
        total = conn.execute(select(func.count()).select_from(query.selection(f).subquery())).scalar_one()
        version = _data_version(conn, principal)
        page = rows[:size]
        sheets = integ.sheets_configured(conn)
        states = integ.record_sync_states(conn, [x.record_id for x in page]) if sheets else {}
    nxt = None
    if len(rows) > size:
        nxt = base64.urlsafe_b64encode(json.dumps(query.cursor_value(sort, page[-1])).encode()).decode()
    return {
        "data": [_with_sync(query.row_json(x), sheets, states) for x in page],
        "next_cursor": nxt,
        "total": total,
        "data_version": version,
        "filter": f.as_json(),
    }


@router.get("/dashboard", summary="Totals by unit, department and status from one aggregate query")
def dashboard(params: FilterParams = Depends(filter_from_query), principal: Principal = Depends(require())) -> dict:
    f = resolve(principal, params)
    with tenant_tx(principal.tenant_id) as conn:
        agg = query.aggregate(conn, f)
        recent = conn.execute(
            query.selection(f).order_by(query.rev.c.production_date.desc(), query.r.c.created_at.desc()).limit(5)
        ).all()
        version = _data_version(conn, principal)
        power_bi = integ.powerbi_status(conn, principal.tenant_id)
    return {
        "data": agg
        | {
            "recent": [query.row_json(x) for x in recent],
            "filter": f.as_json(),
            "data_version": version,
            "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            # Read from the refresh ledger; the native dashboard never waits for Power BI.
            "power_bi": power_bi,
        }
    }


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: FilterParams | None = None
    report_id: uuid.UUID | None = None
    format: Literal["xlsx"] = "xlsx"


@router.post("/exports", status_code=202, summary="Snapshot the selection and render an XLSX in the background")
def create_export(
    body: ExportIn,
    principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    if (body.filter is None) == (body.report_id is None):
        raise ApiError(422, "VALIDATION_FAILED", "Send exactly one of filter or report_id.")
    if body.report_id is not None:  # Excel of the report's own snapshot: same snapshot ID as the PDF
        report_id = body.report_id
        with tenant_tx(principal.tenant_id) as conn:
            status, out = run_idempotent(
                conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id, route="POST /exports",
                key=key, payload=body.model_dump(mode="json"),
                effect=lambda: (202, {"data": exports.create_report_export(conn, principal, report_id)}),
            )  # fmt: skip
        return JSONResponse(out, status_code=status)
    f = resolve(principal, body.filter)
    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn,
            tenant_id=principal.tenant_id,
            actor_id=principal.membership_id,
            route="POST /exports",
            key=key,
            payload=body.model_dump(mode="json"),
            effect=lambda: (202, {"data": exports.create_export(conn, principal, f)}),
        )
    return JSONResponse(out, status_code=status)


@router.get("/exports/{export_id}", summary="Export status; a short-lived download link once READY")
def get_export(export_id: uuid.UUID, principal: Principal = Depends(require(Role.REVIEWER, Role.SENDER))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        row = exports.load_export(conn, principal, export_id)
        data = exports.export_view(row)
        if row.state == "READY":
            audit.record(
                conn,
                tenant_id=principal.tenant_id,
                actor=principal.actor,
                action="EXPORT_DOWNLOADED",
                object_type="export",
                object_id=row.id,
            )
    data["purged"] = row.file_purged_at is not None
    if row.state == "READY" and row.file_purged_at is None:
        name = f"production-records-{row.filter_json['date_from']}-to-{row.filter_json['date_to']}.xlsx"
        data["url"] = get_storage().presign_get(row.file_key, download_name=name)
    else:
        data["url"] = None
    return {"data": data}


@router.get("/control-tower", summary="Today's submissions, review queue and pipeline state per department")
def get_control_tower(
    day: date | None = Query(default=None, alias="date"), principal: Principal = Depends(require())
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": control_tower(conn, principal, day)}
