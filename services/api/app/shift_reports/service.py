"""Daily production sheets: values read from photos merged into the day's sheet, review, approval, history,
the sheet list ("SQL sheet"), files and email.

Access (same scope rules as production records: the caller's departments only):
- read: Uploader, Reviewer, Sender, Viewer, Admin. Edit a draft: Uploader, Reviewer. Approve, or change an approved
  sheet (with a reason): Reviewer. Email: Reviewer, Sender, Admin. Targets and table parameters: Admin.
A day has one sheet per department (UNIQUE), so a second photo of the same day adds to the same sheet: empty cells
are filled, equal values confirm, a different value never overwrites. It is flagged on the cell for a person.
"""

import hashlib
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, Issue, conflict, forbidden, not_found, precondition_failed
from app.db import tables as t
from app.domain.enums import Role
from app.jobs import ledger
from app.shift_reports import catalog, compute
from app.shift_reports.reader import Cell
from app.storage.objects import get_storage, object_key_for

sr, sv, ss, sc, st, se = (
    t.shift_report,
    t.shift_report_value,
    t.shift_report_source,
    t.shift_report_change,
    t.shift_report_target,
    t.sheet_email,
)
READ_ROLES = (Role.UPLOADER, Role.REVIEWER, Role.SENDER, Role.VIEWER, Role.ADMIN)
EDIT_ROLES = (Role.UPLOADER, Role.REVIEWER)
SEND_ROLES = (Role.REVIEWER, Role.SENDER, Role.ADMIN)
EMAIL_KIND = "sheet.email"
FORMATS = ("xlsx", "pdf", "csv")
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;.]{2,}$")
SERVICE = audit.Actor("service", None)


# --- targets and parameters ------------------------------------------------------------------


def targets_and_params(conn: Connection, tenant_id: uuid.UUID) -> tuple[dict, dict]:
    stored = {(r.section, r.metric): r.target for r in conn.execute(select(st))}
    targets: dict[tuple[str, str], Decimal | None] = {}
    params: dict[str, dict[str, Decimal]] = {}
    for s in catalog.SECTIONS:
        for m in s.metrics:
            key = (s.key, m.key)
            targets[key] = stored[key] if key in stored else catalog.default_target(*key)
        params[s.key] = {p: stored.get((s.key, f"_{p}")) or Decimal(v) for p, v in s.params.items()}
    return targets, params


def targets_view(conn: Connection, principal: Principal) -> list[dict[str, Any]]:
    if not principal.has_any(*READ_ROLES):
        raise forbidden()
    targets, params = targets_and_params(conn, principal.tenant_id)
    out = []
    for s in catalog.SECTIONS:
        rows = [
            {"metric": f"_{p}", "label": catalog.PARAM_LABEL[p], "value": _s(v), "parameter": True}
            for p, v in params[s.key].items()
        ]
        rows += [
            {"metric": m.key, "label": m.label, "value": _s(targets[(s.key, m.key)]), "parameter": False}
            for m in s.metrics
            if m.key != "total"
        ]
        out.append({"section": s.key, "title": s.title, "rows": rows})
    return out


def set_targets(conn: Connection, principal: Principal, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not principal.has_any(Role.ADMIN):
        raise forbidden("Only administrators change targets and table parameters.")
    for it in items:
        s = catalog.BY_KEY.get(it["section"])
        ok = s is not None and (s.metric(it["metric"]) is not None or it["metric"][1:] in s.params)
        if not ok:
            raise ApiError(422, "VALIDATION_FAILED", f"Unknown row {it['section']}.{it['metric']}.")
        if it["metric"].startswith("_") and (it["value"] is None or Decimal(it["value"]) <= 0):
            raise ApiError(422, "VALIDATION_FAILED", f"{catalog.PARAM_LABEL[it['metric'][1:]]} must be above 0.")
        value = None if it["value"] in (None, "") else Decimal(str(it["value"]))
        stmt = pg_insert(st).values(
            tenant_id=principal.tenant_id,
            section=it["section"],
            metric=it["metric"],
            target=value,
            updated_by=principal.membership_id,
        )
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "section", "metric"],
                set_={"target": value, "updated_by": principal.membership_id, "updated_at": func.now()},
            )
        )
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="SHEET_TARGETS_CHANGED",
        object_type="tenant",
        object_id=principal.tenant_id,
        after={"rows": [f"{i['section']}.{i['metric']}" for i in items]},
    )
    return targets_view(conn, principal)


# --- merging what was read from a page -------------------------------------------------------


def merge_reading(
    conn: Connection,
    *,
    upload: Any,
    batch: Any,
    page_no: int,
    cells: list[Cell],
    report_date: date | None,
    supervisors: dict[str, str],
    notes: list[dict[str, Any]],
    reader: str,
    written_calc: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    """Called by the extraction worker for a page recognised as a daily sheet (idempotent per upload page)."""
    tenant = conn.execute(select(t.tenant).where(t.tenant.c.id == upload.tenant_id)).one()
    confirmed = report_date is not None
    day = report_date or batch.created_at.astimezone(ZoneInfo(tenant.timezone)).date()
    conn.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"shift_report:{upload.tenant_id}:{batch.department_id}:{day}", 0)
            )
        )
    )
    row = conn.execute(
        select(sr).where(sr.c.department_id == batch.department_id, sr.c.report_date == day).with_for_update()
    ).one_or_none()
    if row is None:
        report_id = uuid.uuid4()
        conn.execute(
            insert(sr).values(
                id=report_id,
                tenant_id=upload.tenant_id,
                department_id=batch.department_id,
                report_date=day,
                date_confirmed=confirmed,
                created_by=batch.owner_id,
            )
        )
        row = conn.execute(select(sr).where(sr.c.id == report_id)).one()
    existing = {(v.section, v.metric, v.shift): v for v in conn.execute(select(sv).where(sv.c.report_id == row.id))}
    added = conflicts = 0
    for c in cells:
        ev = [{"upload_id": str(upload.id), "span_id": i} for i in c.span_ids]
        old = existing.get((c.section, c.metric, c.shift))
        if old is None:
            conn.execute(
                insert(sv).values(
                    id=uuid.uuid4(),
                    tenant_id=upload.tenant_id,
                    report_id=row.id,
                    section=c.section,
                    metric=c.metric,
                    shift=c.shift,
                    value=c.value,
                    raw=c.raw,
                    source="ai" if reader.startswith("claude") else "read",
                    confidence=c.confidence,
                    uncertain=c.uncertain or (c.confidence is not None and c.confidence < 0.9),
                    note=c.note
                    or (
                        "Read with low confidence; check against the photo."
                        if c.confidence is not None and c.confidence < 0.9
                        else None
                    ),
                    evidence=ev,
                )
            )
            added += 1
        elif old.value == c.value:
            known = {(e.get("upload_id"), e.get("span_id")) for e in old.evidence}
            if not all((e["upload_id"], e["span_id"]) in known for e in ev):
                conn.execute(update(sv).where(sv.c.id == old.id).values(evidence=old.evidence + ev))
        else:
            conflicts += 1
            conn.execute(
                update(sv)
                .where(sv.c.id == old.id)
                .values(
                    uncertain=True,
                    note=f"Another page ({upload.display_name} p{page_no}) shows {c.value}; "
                    f"this cell has {old.value}. Check which is right."[:300],
                )
            )
    shifts = dict(row.shifts or {})
    for sh, name in supervisors.items():
        shifts.setdefault(sh, {})
        if not shifts[sh].get("supervisor"):
            shifts[sh] = {"supervisor": name}
    seen_notes = {n["text"] for n in row.notes or []}
    new_notes = [{"label": n["label"], "text": n["text"]} for n in notes if n["text"] not in seen_notes]
    values: dict[str, Any] = {"shifts": shifts, "notes": (row.notes or []) + new_notes, "version": sr.c.version + 1}
    if confirmed and not row.date_confirmed:
        values["date_confirmed"] = True
    if row.state == "APPROVED" and (added or conflicts):
        values["state"] = "DRAFT"  # new or different values need a person's check again
    conn.execute(update(sr).where(sr.c.id == row.id).values(**values))
    stmt = pg_insert(ss).values(
        id=uuid.uuid4(),
        tenant_id=upload.tenant_id,
        report_id=row.id,
        upload_id=upload.id,
        batch_id=batch.id,
        page_no=page_no,
        reader=reader,
        values_read=len(cells),
        conflicts=conflicts,
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["report_id", "upload_id", "page_no"],
            set_={"reader": reader, "values_read": len(cells), "conflicts": conflicts},
        )
    )
    audit.record(
        conn,
        tenant_id=upload.tenant_id,
        actor=SERVICE,
        action="SHEET_PAGE_READ",
        object_type="shift_report",
        object_id=row.id,
        after={
            "upload_id": str(upload.id),
            "page": page_no,
            "reader": reader,
            "values": len(cells),
            "added": added,
            "conflicts": conflicts,
        },
    )
    if written_calc:
        cross_check(conn, row.id, written_calc, f"{upload.display_name} p{page_no}")
    return row.id


# --- access and views ------------------------------------------------------------------------


def _s(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral() else format(v.normalize(), "f")
    return str(v)


def _load(conn: Connection, principal: Principal, report_id: uuid.UUID, lock: bool = False) -> Any:
    if not principal.has_any(*READ_ROLES):
        raise forbidden()
    q = select(sr).where(sr.c.id == report_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None or not principal.can_access_department(row.department_id):
        raise not_found()
    return row


def _values(conn: Connection, report_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[tuple, Any]]:
    out: dict[uuid.UUID, dict[tuple, Any]] = {r: {} for r in report_ids}
    if report_ids:
        for v in conn.execute(select(sv).where(sv.c.report_id.in_(report_ids))):
            out[v.report_id][(v.section, v.metric, v.shift)] = v
    return out


def grids_for(conn: Connection, row: Any) -> tuple[dict, dict, dict[tuple, Any]]:
    """(grids of this day, to-date per (section, metric), raw value rows) using the month's sheets so far."""
    targets, params = targets_and_params(conn, row.tenant_id)
    month_start = row.report_date.replace(day=1)
    earlier = (
        conn.execute(
            select(sr.c.id).where(
                sr.c.department_id == row.department_id,
                sr.c.report_date >= month_start,
                sr.c.report_date < row.report_date,
            )
        )
        .scalars()
        .all()
    )
    vals = _values(conn, [row.id, *earlier])
    nums = {k: v.value for k, v in vals[row.id].items()}
    grids = compute.all_grids(nums, params)
    earlier_grids = [compute.all_grids({k: v.value for k, v in vals[r].items()}, params) for r in earlier]
    todate = {
        (s.key, m.key): compute.to_date(grids[s.key][m.key]["total"], [g[s.key][m.key]["total"] for g in earlier_grids])
        for s in catalog.SECTIONS
        for m in s.metrics
    }
    return grids, todate, vals[row.id] | {"_targets": targets, "_params": params}


def sheet_view(conn: Connection, principal: Principal, report_id: uuid.UUID) -> dict[str, Any]:
    row = _load(conn, principal, report_id)
    grids, todate, raw = grids_for(conn, row)
    targets, params = raw.pop("_targets"), raw.pop("_params")
    sections = []
    uncertain = missing = 0
    for s in catalog.SECTIONS:
        rows = []
        for m in s.metrics:
            cells = {}
            for sh in s.shifts:
                v = raw.get((s.key, m.key, sh))
                if m.kind == "input":
                    if v is None:
                        missing += 1
                    elif v.uncertain:
                        uncertain += 1
                cells[sh] = {
                    "value": _s(grids[s.key][m.key][sh]),
                    "source": v.source if v is not None else None,
                    "uncertain": bool(v.uncertain) if v is not None else False,
                    "note": v.note if v is not None else None,
                    "raw": v.raw if v is not None else None,
                    "evidence": v.evidence if v is not None else [],
                }
            rows.append(
                {
                    "metric": m.key,
                    "label": m.label,
                    "kind": m.kind,
                    "unit": m.unit,
                    "agg": m.agg,
                    "target": _s(targets[(s.key, m.key)]),
                    "cells": cells,
                    "total": _s(grids[s.key][m.key]["total"]),
                    "to_date": _s(todate[(s.key, m.key)]),
                }
            )
        sections.append(
            {
                "key": s.key,
                "title": s.title,
                "group": s.group,
                "shifts": list(s.shifts),
                "rows": rows,
                "params": {k: _s(v) for k, v in params[s.key].items()},
            }
        )
    dept = conn.execute(select(t.department.c.name).where(t.department.c.id == row.department_id)).scalar_one()
    names = dict(conn.execute(select(t.membership.c.id, t.membership.c.display_name)).all())
    sources = conn.execute(
        select(ss, t.upload.c.display_name)
        .join(t.upload, t.upload.c.id == ss.c.upload_id)
        .where(ss.c.report_id == row.id)
        .order_by(ss.c.created_at)
    ).all()
    changes = conn.execute(select(sc).where(sc.c.report_id == row.id).order_by(sc.c.created_at.desc()).limit(200)).all()
    emails = conn.execute(select(se).where(se.c.report_id == row.id).order_by(se.c.created_at.desc())).all()
    return {
        "id": str(row.id),
        "department": dept,
        "department_id": str(row.department_id),
        "report_date": row.report_date.isoformat(),
        "date_confirmed": row.date_confirmed,
        "state": row.state,
        "shifts": {sh: (row.shifts or {}).get(sh, {}) for sh in catalog.SHIFTS},
        "notes": row.notes or [],
        "sections": sections,
        "uncertain": uncertain,
        "missing": missing,
        "approvable": row.state == "DRAFT" and uncertain == 0 and row.date_confirmed,
        "approved_version": row.approved_version,
        "approved_by": names.get(row.approved_by),
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "sources": [
            {
                "upload_id": str(x.upload_id),
                "batch_id": str(x.batch_id),
                "file": x.display_name,
                "page_no": x.page_no,
                "reader": x.reader,
                "values_read": x.values_read,
                "conflicts": x.conflicts,
            }
            for x in sources
        ],
        "changes": [
            {
                "section": c.section,
                "metric": c.metric,
                "shift": c.shift,
                "old": _s(c.old_value),
                "new": _s(c.new_value),
                "reason": c.reason,
                "by": names.get(c.actor_id),
                "at": c.created_at.isoformat(),
            }
            for c in changes
        ],
        "emails": [email_view(e, names) for e in emails],
        "version": row.version,
    }


def email_view(e: Any, names: dict | None = None) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "to_email": e.to_email,
        "format": e.format,
        "attachment": e.attachment_name,
        "state": e.state,
        "http_status": e.http_status,
        "error": {"code": e.error_code, "message": e.error_message} if e.error_code else None,
        "by": (names or {}).get(e.created_by),
        "at": e.created_at.isoformat(),
        "finished_at": e.finished_at.isoformat() if e.finished_at else None,
    }


KEY_FIGURES = (
    ("production_m", "sulzer", "production_m", "Sulzer production (m)"),
    ("production_kg", "sulzer", "production_kg", "Sulzer production (kg)"),
    ("picks", "sulzer", "picks", "Picks"),
    ("total_eff_pct", "sulzer", "total_eff_pct", "Total efficiency %"),
    ("running_looms", "sulzer", "running_looms", "Running looms"),
    ("downtime", "downtime", "total", "Downtime (h)"),
    ("warping_m", "warping_prashant", "meters", "Warping Prashant (m)"),
)


def list_sheets(
    conn: Connection,
    principal: Principal,
    date_from: date | None,
    date_to: date | None,
    state: str | None,
    supervisor: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not principal.has_any(*READ_ROLES):
        raise forbidden()
    q = (
        select(sr, t.department.c.name.label("dept"))
        .join(t.department, t.department.c.id == sr.c.department_id)
        .where(sr.c.department_id.in_(list(principal.department_ids)))
    )
    if date_from:
        q = q.where(sr.c.report_date >= date_from)
    if date_to:
        q = q.where(sr.c.report_date <= date_to)
    if state:
        q = q.where(sr.c.state == state)
    rows = conn.execute(q.order_by(sr.c.report_date.desc()).limit(limit)).all()
    if supervisor:
        needle = supervisor.strip().lower()
        rows = [
            r
            for r in rows
            if any(needle in str((r.shifts or {}).get(sh, {}).get("supervisor", "")).lower() for sh in catalog.SHIFTS)
        ]
    targets_params = targets_and_params(conn, principal.tenant_id) if rows else ({}, {})
    vals = _values(conn, [r.id for r in rows])
    out = []
    for r in rows:
        grids = compute.all_grids({k: v.value for k, v in vals[r.id].items()}, targets_params[1])
        unc = sum(1 for v in vals[r.id].values() if v.uncertain)
        last_email = conn.execute(
            select(se.c.state).where(se.c.report_id == r.id).order_by(se.c.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        out.append(
            {
                "id": str(r.id),
                "report_date": r.report_date.isoformat(),
                "department": r.dept,
                "state": r.state,
                "supervisors": {sh: (r.shifts or {}).get(sh, {}).get("supervisor") for sh in catalog.SHIFTS},
                "figures": {k: _s(grids[sec][met]["total"]) for k, sec, met, _ in KEY_FIGURES},
                "values": len(vals[r.id]),
                "uncertain": unc,
                "last_email_state": last_email,
                "updated_at": r.updated_at.isoformat(),
                "version": r.version,
            }
        )
    return out


# --- editing and approval --------------------------------------------------------------------


def _check_cell(section: str, metric: str, shift: str) -> None:
    s = catalog.BY_KEY.get(section)
    m = s.metric(metric) if s else None
    if s is None or m is None or m.kind != "input" or shift not in s.shifts:
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            f"{section}.{metric}.{shift} is not a cell you can enter.",
            [Issue("UNKNOWN_CELL", "Not an input cell of the sheet.", f"{section}.{metric}.{shift}")],
        )


def _number(text: Any, where: str) -> Decimal | None:
    if text is None or str(text).strip() == "":
        return None
    raw = str(text).strip().replace(",", "").translate(str.maketrans("૦૧૨૩૪૫૬૭૮૯०१२३४५६७८९", "01234567890123456789"))
    try:
        v = Decimal(raw)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            f'"{text}" is not a number.',
            [Issue("NOT_A_NUMBER", f'"{text}" is not a number.', where)],
        ) from exc
    if abs(v) >= Decimal("1e14") or v != v.quantize(Decimal("0.0001")):
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            f'"{text}" has too many digits.',
            [Issue("TOO_PRECISE", "At most 4 decimal places.", where)],
        )
    return v


def patch_sheet(
    conn: Connection, principal: Principal, report_id: uuid.UUID, expected_version: int, body: dict[str, Any]
) -> dict[str, Any]:
    if not principal.has_any(*EDIT_ROLES):
        raise forbidden("Only Uploaders and Reviewers edit sheets.")
    row = _load(conn, principal, report_id, lock=True)
    if row.version != expected_version:
        raise precondition_failed(row.version)
    approved = row.state == "APPROVED"
    reason = (body.get("reason") or "").strip() or None
    if approved:
        if not principal.has_any(Role.REVIEWER):
            raise forbidden("An approved sheet is changed by a Reviewer only.")
        if not reason or len(reason) < 5:
            raise ApiError(
                422,
                "VALIDATION_FAILED",
                "Give a reason for changing an approved sheet (5+ characters).",
                [Issue("REASON_REQUIRED", "A reason is required.", "reason")],
            )
    existing = {(v.section, v.metric, v.shift): v for v in conn.execute(select(sv).where(sv.c.report_id == row.id))}
    changed = 0
    for c in body.get("values", []):
        key = (c["section"], c["metric"], c["shift"])
        _check_cell(*key)
        new = _number(c.get("value"), ".".join(key))
        old = existing.get(key)
        if old is None and new is None:
            continue
        if old is not None and old.value == new and not old.uncertain:
            continue
        if old is None:
            conn.execute(
                insert(sv).values(
                    id=uuid.uuid4(),
                    tenant_id=row.tenant_id,
                    report_id=row.id,
                    section=key[0],
                    metric=key[1],
                    shift=key[2],
                    value=new,
                    raw=None,
                    source="reviewer" if principal.has_any(Role.REVIEWER) else "manual",
                    updated_by=principal.membership_id,
                )
            )
        else:
            conn.execute(
                update(sv)
                .where(sv.c.id == old.id)
                .values(
                    value=new,
                    source="reviewer" if principal.has_any(Role.REVIEWER) else "manual",
                    uncertain=False,
                    note=None,
                    updated_by=principal.membership_id,
                    updated_at=func.now(),
                )
            )
        if old is None or old.value != new:
            conn.execute(
                insert(sc).values(
                    id=uuid.uuid4(),
                    tenant_id=row.tenant_id,
                    report_id=row.id,
                    section=key[0],
                    metric=key[1],
                    shift=key[2],
                    old_value=old.value if old else None,
                    new_value=new,
                    reason=reason,
                    actor_id=principal.membership_id,
                )
            )
        changed += 1
    for key in body.get("confirm", []):  # the value as read is right
        k = (key["section"], key["metric"], key["shift"])
        v = existing.get(k)
        if v is not None and v.uncertain:
            conn.execute(
                update(sv).where(sv.c.id == v.id).values(uncertain=False, note=None, updated_by=principal.membership_id)
            )
            changed += 1
    values: dict[str, Any] = {"version": sr.c.version + 1}
    if "shifts" in body:
        values["shifts"] = {
            sh: {"supervisor": (body["shifts"].get(sh) or {}).get("supervisor", "")[:80].strip()}
            for sh in catalog.SHIFTS
        }
    if "notes" in body:
        values["notes"] = [
            {"label": str(n.get("label", "Other"))[:80], "text": str(n.get("text", ""))[:300]}
            for n in body["notes"]
            if str(n.get("text", "")).strip()
        ][:50]
    if body.get("report_date"):
        new_day = date.fromisoformat(body["report_date"])
        if new_day != row.report_date:
            clash = conn.execute(
                select(sr.c.id).where(sr.c.department_id == row.department_id, sr.c.report_date == new_day)
            ).first()
            if clash:
                raise conflict("DATE_TAKEN", f"There is already a sheet for {new_day:%d %b %Y}. Open that sheet.")
            values["report_date"] = new_day
        values["date_confirmed"] = True
    conn.execute(update(sr).where(sr.c.id == row.id).values(**values))
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="SHEET_EDITED",
        object_type="shift_report",
        object_id=row.id,
        object_revision=row.version + 1,
        reason=reason,
        after={"cells": changed, "approved_sheet": approved},
    )
    return sheet_view(conn, principal, row.id)


def approve(conn: Connection, principal: Principal, report_id: uuid.UUID, expected_version: int) -> dict[str, Any]:
    if not principal.has_any(Role.REVIEWER):
        raise forbidden("Only Reviewers approve sheets.")
    row = _load(conn, principal, report_id, lock=True)
    if row.version != expected_version:
        raise precondition_failed(row.version)
    if row.state == "APPROVED":
        raise conflict("ALREADY_APPROVED", "This sheet is already approved.")
    problems = []
    if not row.date_confirmed:
        problems.append(
            Issue("DATE_NOT_CONFIRMED", "The date was not found on the page. Confirm the sheet's date.", "report_date")
        )
    for v in conn.execute(select(sv).where(sv.c.report_id == row.id, sv.c.uncertain)):
        label = catalog.BY_KEY[v.section].metric(v.metric).label
        problems.append(
            Issue(
                "CONFIRM_VALUE",
                f"{catalog.BY_KEY[v.section].title} - {label} ({v.shift}): {v.note or 'check against the photo'}",
                f"{v.section}.{v.metric}.{v.shift}",
            )
        )
    if problems:
        raise ApiError(422, "VALIDATION_FAILED", f"{len(problems)} value(s) must be checked before approval.", problems)
    conn.execute(
        update(sr)
        .where(sr.c.id == row.id)
        .values(
            state="APPROVED",
            approved_version=sr.c.approved_version + 1,
            approved_by=principal.membership_id,
            approved_at=func.now(),
            version=sr.c.version + 1,
        )
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="SHEET_APPROVED",
        object_type="shift_report",
        object_id=row.id,
        object_revision=row.approved_version + 1,
    )
    return sheet_view(conn, principal, row.id)


def for_batch(conn: Connection, principal: Principal, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    """Sheets that pages of this upload batch were merged into (shown on the batch and worker screens)."""
    rows = conn.execute(
        select(
            sr.c.id,
            sr.c.report_date,
            sr.c.state,
            sr.c.department_id,
            func.sum(ss.c.values_read).label("n"),
            func.array_agg(func.distinct(ss.c.upload_id)).label("uploads"),
        )
        .join(ss, ss.c.report_id == sr.c.id)
        .where(ss.c.batch_id == batch_id)
        .group_by(sr.c.id)
        .order_by(sr.c.report_date)
    ).all()
    out = []
    for r in rows:
        if not principal.can_access_department(r.department_id):
            continue
        unc = conn.execute(
            select(func.count()).select_from(sv).where(sv.c.report_id == r.id, sv.c.uncertain)
        ).scalar_one()
        out.append(
            {
                "id": str(r.id),
                "report_date": r.report_date.isoformat(),
                "state": r.state,
                "values_read": int(r.n or 0),
                "upload_ids": sorted(str(u) for u in r.uploads),
                "uncertain": unc,
            }
        )
    return out


# --- files and email -------------------------------------------------------------------------


def file_for(conn: Connection, principal: Principal, report_id: uuid.UUID, fmt: str) -> tuple[bytes, str, str]:
    from app.shift_reports import export

    row = _load(conn, principal, report_id)
    return export.render(conn, row, fmt)


def start_email(
    conn: Connection, principal: Principal, report_id: uuid.UUID, to_email: str, fmt: str, version: int
) -> dict[str, Any]:
    from app.owner_reports import settings as owner_settings
    from app.shift_reports import export

    if not principal.has_any(*SEND_ROLES):
        raise forbidden("Reviewers, Senders and administrators email sheets.")
    address = to_email.strip()
    if not _EMAIL.match(address):
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            "Enter one valid email address.",
            [Issue("INVALID_EMAIL", f'"{address}" is not a valid email address.', "to_email")],
        )
    if fmt not in FORMATS:
        raise ApiError(422, "VALIDATION_FAILED", "Choose xlsx, pdf or csv.")
    row = _load(conn, principal, report_id, lock=True)
    if row.version != version:
        raise conflict("SHEET_CHANGED", "This sheet changed since you opened it. Reload it and send again.")
    if owner_settings.missing(owner_settings.load(conn, row.tenant_id)):
        raise conflict(
            "EMAIL_NOT_CONFIGURED",
            "Email is not set up yet. An administrator completes Settings -> Owner report & email.",
        )
    busy = conn.execute(
        select(se.c.id).where(
            se.c.report_id == row.id,
            func.lower(se.c.to_email) == address.lower(),
            se.c.format == fmt,
            se.c.state.in_(("QUEUED", "SENDING")),
        )
    ).first()
    if busy:
        raise conflict("ALREADY_SENDING", f"This sheet is already being sent to {address}.")
    data, name, mime = export.render(conn, row, fmt)
    email_id = uuid.uuid4()
    # The exact file is stored now and attached later, so the recipient gets precisely what was queued.
    get_storage().put_bytes(object_key_for("exports", row.tenant_id, email_id, f".{fmt}"), data, mime)
    conn.execute(
        insert(se).values(
            id=email_id,
            tenant_id=row.tenant_id,
            report_id=row.id,
            report_version=row.version,
            to_email=address,
            format=fmt,
            attachment_name=name,
            attachment_sha256=hashlib.sha256(data).hexdigest(),
            attachment_bytes=len(data),
            created_by=principal.membership_id,
        )
    )
    ledger.create_job(
        conn, tenant_id=row.tenant_id, kind=EMAIL_KIND, object_id=email_id, created_by=principal.membership_id
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="SHEET_EMAIL_QUEUED",
        object_type="shift_report",
        object_id=row.id,
        after={"email_id": str(email_id), "format": fmt},
    )
    return email_view(conn.execute(select(se).where(se.c.id == email_id)).one())


def email_params(conn: Connection, e: Any) -> tuple[dict[str, str], bytes]:
    """EmailJS variables for one sheet email: key figures in the message and as a table, the file attached."""
    import base64

    from app.owner_reports.settings import company_name
    from app.shift_reports import export

    row = conn.execute(select(sr).where(sr.c.id == e.report_id)).one()
    data = get_storage().get_bytes(object_key_for("exports", e.tenant_id, e.id, f".{e.format}"), 20_000_000)
    if hashlib.sha256(data).hexdigest() != e.attachment_sha256:
        raise RuntimeError("the stored file does not match the one recorded for this email")
    name, mime = e.attachment_name, export.MIME[e.format]
    grids, todate, _ = grids_for(conn, row)
    company = company_name(conn, row.tenant_id)
    dept = conn.execute(select(t.department.c.name).where(t.department.c.id == row.department_id)).scalar_one()
    lines, html_rows = [], []
    for _, sec, met, label in KEY_FIGURES:
        g = grids[sec][met]
        vals = [compute.rounded(g.get(sh)) for sh in catalog.SHIFTS]
        lines.append(
            f"{label}: I {vals[0] or '-'} | II {vals[1] or '-'} | III {vals[2] or '-'} | "
            f"total {compute.rounded(g['total']) or '-'} | to date {compute.rounded(todate[(sec, met)]) or '-'}"
        )
        html_rows.append(
            "<tr>"
            + "".join(
                f'<td style="border:1px solid #ccc;padding:4px">{x}</td>'
                for x in (label, *vals, compute.rounded(g["total"]), compute.rounded(todate[(sec, met)]))
            )
            + "</tr>"
        )
    title = f"Daily production sheet {row.report_date:%d %b %Y} - {dept}"
    message = (
        f"Hello,\n\nPlease find attached the daily production sheet for {row.report_date:%d %b %Y} "
        f"({dept}, {'approved' if row.state == 'APPROVED' else 'not yet approved'}).\n\n"
        + "\n".join(lines)
        + f"\n\nThe full sheet is attached ({name}).\n\n{company}"
    )
    head = "".join(
        f'<th style="border:1px solid #ccc;padding:4px;background:#eef2f7">{h}</th>'
        for h in ("", "I", "II", "III", "Total", "To date")
    )
    encoded = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    params = {
        "to_email": e.to_email,
        "subject": title,
        "title": title,
        "message": message,
        "name": company,
        "email": e.to_email,
        "record_reference": f"{row.report_date:%Y-%m-%d} {dept}",
        "customer_name": "",
        "company_name": company,
        "from_name": company,
        "reply_to": "",
        "order_count": "",
        "orders_text": "\n".join(lines),
        "orders_html": f'<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px">'
        f"<tr>{head}</tr>{''.join(html_rows)}</table>",
        "attachment_name": name,
        "email_reference": str(e.id),
        # The template's variable attachment "pdf_file" is a PDF slot; Excel / CSV go in "sheet_file".
        "pdf_file": encoded if e.format == "pdf" else "",
        "sheet_file": encoded if e.format != "pdf" else "",
        "sheet_file_name": name,
    }
    return params, data


def batch_waiting(conn: Connection, batch_id: uuid.UUID) -> int:
    """Draft sheets fed by this batch that still have values to check (for the batch status steps)."""
    return conn.execute(
        select(func.count(func.distinct(sr.c.id)))
        .select_from(sr)
        .join(ss, ss.c.report_id == sr.c.id)
        .where(ss.c.batch_id == batch_id, sr.c.state == "DRAFT")
    ).scalar_one()


# Calculated values a worker also wrote, compared with the sheet's own calculation. A mismatch means one of the
# written values it is calculated from is probably misread or miswritten: those cells are highlighted.
CHECK_INPUTS = {
    "utilization_pct": ("running_looms",),
    "total_eff_pct": ("picks",),
    "working_pct": ("running_looms", "picks"),
    "loss_of_pick": ("running_looms", "picks"),
    "picks_per_hour": ("running_looms", "picks"),
    "theoretical_picks": ("running_looms",),
    "meters_per_min": ("meters", "working_hours"),
}


def _tolerance(metric: str, written: Decimal) -> Decimal:
    if metric.endswith("_pct") or metric in ("meters_per_min", "picks_per_hour"):
        return Decimal("0.06")  # written with two decimals
    return max(Decimal("1.5"), abs(written) * Decimal("0.002"))  # loss of pick, totals: whole numbers


def cross_check(conn: Connection, report_id: uuid.UUID, written: list[dict[str, Any]], source: str) -> int:
    row = conn.execute(select(sr).where(sr.c.id == report_id)).one()
    _, params = targets_and_params(conn, row.tenant_id)
    stored = {(v.section, v.metric, v.shift): v for v in conn.execute(select(sv).where(sv.c.report_id == row.id))}
    grids = compute.all_grids({k: v.value for k, v in stored.items()}, params)
    flagged, notes = 0, list(row.notes or [])
    checks = []
    for w in written:
        sec, met, sh, value = w["section"], w["metric"], w["shift"], Decimal(str(w["value"]))
        computed = grids[sec][met].get(sh)
        checks.append(
            (w, sec, met, sh, value, computed, computed is not None and abs(computed - value) <= _tolerance(met, value))
        )
    # A written figure that matches confirms the single value it depends on (e.g. total efficiency -> picks).
    vouched = {(c[1], CHECK_INPUTS[c[2]][0], c[3]) for c in checks if c[6] and len(CHECK_INPUTS.get(c[2], ())) == 1}
    for w, sec, met, sh, value, computed, ok in checks:
        title = catalog.BY_KEY[sec].title
        if computed is None:
            notes.append(
                {
                    "label": "Not checked",
                    "text": f"{title}: {w['label']} written as {value} ({source}), but "
                    "the values it is calculated from are not all on the page.",
                }
            )
            continue
        if ok:
            continue
        text = (
            f"The page also writes {w['label']} = {value}; calculated from the written values it is "
            f"{compute.rounded(computed)}. This value is probably misread or miswritten."
        )
        inputs = CHECK_INPUTS.get(met)
        if not inputs:  # a Total row: it cannot point at one value
            notes.append(
                {
                    "label": "Check",
                    "text": f"{title}: written {w['label']} {value} does not match the "
                    f"rows of shift {sh} ({compute.rounded(computed)}) ({source}).",
                }
            )
            continue
        suspects = [i for i in inputs if (sec, i, sh) not in vouched] or list(inputs)
        for inp in suspects:
            v = stored.get((sec, inp, sh))
            if v is not None and v.source in ("read", "ai") and not v.uncertain:
                conn.execute(update(sv).where(sv.c.id == v.id).values(uncertain=True, note=text[:300]))
                flagged += 1
    if notes != (row.notes or []):
        conn.execute(update(sr).where(sr.c.id == row.id).values(notes=notes[:50]))
    return flagged
