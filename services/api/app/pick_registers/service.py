"""Pick reading registers: pages read from photos merged into the day's register, review, approval, history, the
register list, files (Excel / PDF / CSV / SQL) and email.

Access (the caller's departments only), as for daily sheets:
- read: Uploader, Reviewer, Sender, Viewer, Admin. Edit a draft: Uploader, Reviewer. Approve, or change an approved
  register (with a reason): Reviewer. Email: Reviewer, Sender, Admin.
A day has one register per department (UNIQUE): the shift pages of a day (and a second photo of the same page) go
into the same register. Empty cells are filled, equal values confirm, a different value never overwrites: it is
flagged on the cell for a person.
"""

import hashlib
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
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
from app.pick_registers import compute
from app.pick_registers.layout import SHIFT_HOURS, SHIFTS, SLOTS, TIMES, machine_key, machine_sort, status_label
from app.pick_registers.reader import RegisterReading
from app.storage.objects import get_storage, object_key_for

pr, pv, pt, ps, pc, re_ = (
    t.pick_register,
    t.pick_register_value,
    t.pick_register_total,
    t.pick_register_source,
    t.pick_register_change,
    t.register_email,
)
READ_ROLES = (Role.UPLOADER, Role.REVIEWER, Role.SENDER, Role.VIEWER, Role.ADMIN)
EDIT_ROLES = (Role.UPLOADER, Role.REVIEWER)
SEND_ROLES = (Role.REVIEWER, Role.SENDER, Role.ADMIN)
EMAIL_KIND = "register.email"
FORMATS = ("xlsx", "pdf", "csv", "sql")
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;.]{2,}$")
SERVICE = audit.Actor("service", None)


def _s(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


# --- merging what was read from a page -------------------------------------------------------


def merge_reading(
    conn: Connection, *, upload: Any, batch: Any, page_no: int, reading: RegisterReading, reader: str
) -> uuid.UUID:
    """Called by the extraction worker for a page recognised as a pick register (idempotent per upload page)."""
    tenant = conn.execute(select(t.tenant).where(t.tenant.c.id == upload.tenant_id)).one()
    confirmed = reading.register_date is not None
    day = reading.register_date or batch.created_at.astimezone(ZoneInfo(tenant.timezone)).date()
    shift = reading.shift
    if shift not in SHIFTS:
        raise ValueError("a register page needs its shift (the column times)")
    conn.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"pick_register:{upload.tenant_id}:{batch.department_id}:{day}", 0)
            )
        )
    )
    row = conn.execute(
        select(pr).where(pr.c.department_id == batch.department_id, pr.c.register_date == day).with_for_update()
    ).one_or_none()
    if row is None:
        register_id = uuid.uuid4()
        conn.execute(
            insert(pr).values(
                id=register_id,
                tenant_id=upload.tenant_id,
                department_id=batch.department_id,
                register_date=day,
                date_confirmed=confirmed,
                created_by=batch.owner_id,
            )
        )
        row = conn.execute(select(pr).where(pr.c.id == register_id)).one()
    same_photo = conn.execute(
        select(t.upload.c.display_name)
        .join(ps, ps.c.upload_id == t.upload.c.id)
        .where(
            ps.c.register_id == row.id,
            ps.c.page_no == page_no,
            ps.c.shift == shift,
            ps.c.upload_id != upload.id,
            t.upload.c.declared_sha256 == upload.declared_sha256,
        )
        .limit(1)
    ).scalar_one_or_none()
    if same_photo is not None:  # the exact same photo again: reading it twice only adds differences to check
        return _same_photo(conn, row, upload, batch, page_no, shift, reader, same_photo)
    existing = {
        (v.machine, v.slot): v for v in conn.execute(select(pv).where(pv.c.register_id == row.id, pv.c.shift == shift))
    }
    added = conflicts = 0
    unverified: list[tuple[str, int]] = []
    for c in reading.cells:
        ev = [{"upload_id": str(upload.id), "span_id": i} for i in c.span_ids]
        old = existing.get((c.machine, c.slot))
        if old is None:
            new_id = uuid.uuid4()
            conn.execute(
                insert(pv).values(
                    id=new_id,
                    tenant_id=upload.tenant_id,
                    register_id=row.id,
                    shift=shift,
                    machine=c.machine,
                    slot=c.slot,
                    reading=c.reading,
                    picks=c.picks if c.slot else None,
                    status=c.status,
                    raw=c.raw[:200],
                    source="ai" if reader.startswith("claude") else "read",
                    confidence=c.confidence,
                    uncertain=c.uncertain or c.unverified,
                    note=(c.note or ("Read from the photo only; check it." if c.unverified else None)),
                    evidence=ev,
                )
            )
            # A second value for the same cell later on this page is compared with this one.
            existing[(c.machine, c.slot)] = SimpleNamespace(
                id=new_id, reading=c.reading, picks=c.picks if c.slot else None, status=c.status, evidence=ev
            )
            added += 1
            if c.unverified and not c.uncertain:
                unverified.append((c.machine, c.slot))
        elif (old.reading, old.picks, old.status) == (c.reading, c.picks if c.slot else None, c.status):
            known = {(e.get("upload_id"), e.get("span_id")) for e in old.evidence}
            if not all((e["upload_id"], e["span_id"]) in known for e in ev):
                conn.execute(update(pv).where(pv.c.id == old.id).values(evidence=old.evidence + ev))
        else:
            conflicts += 1
            conn.execute(
                update(pv)
                .where(pv.c.id == old.id)
                .values(
                    uncertain=True,
                    note=f"Another page ({upload.display_name} p{page_no}) shows {_cell_text(c)}; "
                    f"this cell has {_cell_text(old)}. Check which is right."[:300],
                )
            )
    for slot, (value, raw, ids) in reading.totals.items():
        stmt = pg_insert(pt).values(
            id=uuid.uuid4(),
            tenant_id=upload.tenant_id,
            register_id=row.id,
            shift=shift,
            slot=slot,
            written=value,
            raw=raw[:200],
            source="ai" if reader.startswith("claude") else "read",
            evidence=[{"upload_id": str(upload.id), "span_id": i} for i in ids],
        )
        conn.execute(stmt.on_conflict_do_nothing(index_elements=["register_id", "shift", "slot"]))
    seen_notes = {n["text"] for n in row.notes or []}
    new_notes = [{"label": n["label"], "text": n["text"]} for n in reading.notes if n["text"] not in seen_notes]
    values: dict[str, Any] = {"notes": ((row.notes or []) + new_notes)[:50], "version": pr.c.version + 1}
    if confirmed and not row.date_confirmed:
        values["date_confirmed"] = True
    if row.state == "APPROVED" and (added or conflicts):
        values["state"] = "DRAFT"  # new or different values need a person's check again
    conn.execute(update(pr).where(pr.c.id == row.id).values(**values))
    if unverified:  # read from the image only: confirmed when the meter readings agree with the picks
        res = _calculate(conn, row)
        ok = [(m, k) for m, k in unverified if (shift, m, k) in res.vouched and not res.issues.get((shift, m, k))]
        for m, k in ok:
            conn.execute(
                update(pv)
                .where(pv.c.register_id == row.id, pv.c.shift == shift, pv.c.machine == m, pv.c.slot == k)
                .values(uncertain=False, note=None)
            )
    stmt = pg_insert(ps).values(
        id=uuid.uuid4(),
        tenant_id=upload.tenant_id,
        register_id=row.id,
        upload_id=upload.id,
        batch_id=batch.id,
        page_no=page_no,
        shift=shift,
        reader=reader,
        values_read=len(reading.cells),
        conflicts=conflicts,
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["register_id", "upload_id", "page_no"],
            set_={"reader": reader, "values_read": len(reading.cells), "conflicts": conflicts, "shift": shift},
        )
    )
    audit.record(
        conn,
        tenant_id=upload.tenant_id,
        actor=SERVICE,
        action="REGISTER_PAGE_READ",
        object_type="pick_register",
        object_id=row.id,
        after={
            "upload_id": str(upload.id),
            "page": page_no,
            "shift": shift,
            "reader": reader,
            "values": len(reading.cells),
            "added": added,
            "conflicts": conflicts,
        },
    )
    return row.id


def _same_photo(
    conn: Connection, row: Any, upload: Any, batch: Any, page_no: int, shift: str, reader: str, first: str
) -> uuid.UUID:
    text = f"{upload.display_name} is the same photo as {first}: it was not read again."
    notes = row.notes or []
    if not any(n.get("text") == text for n in notes):
        conn.execute(
            update(pr)
            .where(pr.c.id == row.id)
            .values(notes=(notes + [{"label": "Same photo", "text": text}])[:50], version=pr.c.version + 1)
        )
    stmt = pg_insert(ps).values(
        id=uuid.uuid4(), tenant_id=upload.tenant_id, register_id=row.id, upload_id=upload.id, batch_id=batch.id,
        page_no=page_no, shift=shift, reader=f"{reader} (same photo)", values_read=0, conflicts=0,
    )  # fmt: skip
    conn.execute(stmt.on_conflict_do_nothing(index_elements=["register_id", "upload_id", "page_no"]))
    audit.record(
        conn,
        tenant_id=upload.tenant_id,
        actor=SERVICE,
        action="REGISTER_PAGE_SKIPPED",
        object_type="pick_register",
        object_id=row.id,
        after={"upload_id": str(upload.id), "page": page_no, "reason": "same photo", "first": first},
    )
    return row.id


def _cell_text(c: Any) -> str:
    parts = [p for p in (_s(c.reading), f"({_s(c.picks)})" if c.picks is not None else None, c.status) if p]
    return " ".join(parts) or "nothing"


# --- access and views ------------------------------------------------------------------------


def _load(conn: Connection, principal: Principal, register_id: uuid.UUID, lock: bool = False) -> Any:
    if not principal.has_any(*READ_ROLES):
        raise forbidden()
    q = select(pr).where(pr.c.id == register_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None or not principal.can_access_department(row.department_id):
        raise not_found()
    return row


def _values(conn: Connection, register_id: uuid.UUID) -> dict[tuple[str, str, int], Any]:
    return {(v.shift, v.machine, v.slot): v for v in conn.execute(select(pv).where(pv.c.register_id == register_id))}


def _written(conn: Connection, register_id: uuid.UUID) -> dict[tuple[str, int], Any]:
    return {(w.shift, w.slot): w for w in conn.execute(select(pt).where(pt.c.register_id == register_id))}


def _previous_end(conn: Connection, row: Any) -> dict[str, Decimal]:
    """Last reading of each machine in the previous day's shift III (start of this day's shift I)."""
    prev = conn.execute(
        select(pr.c.id).where(
            pr.c.department_id == row.department_id, pr.c.register_date == row.register_date - timedelta(days=1)
        )
    ).scalar_one_or_none()
    if prev is None:
        return {}
    out: dict[str, tuple[int, Decimal]] = {}
    for v in conn.execute(select(pv).where(pv.c.register_id == prev, pv.c.shift == "III", pv.c.reading.isnot(None))):
        if v.machine not in out or v.slot > out[v.machine][0]:
            out[v.machine] = (v.slot, v.reading)
    return {m: r for m, (_, r) in out.items()}


def _calculate(conn: Connection, row: Any, values: dict | None = None, written: dict | None = None) -> compute.Result:
    values = values if values is not None else _values(conn, row.id)
    written = written if written is not None else _written(conn, row.id)
    return compute.calculate(values, {k: w.written for k, w in written.items()}, _previous_end(conn, row))


def _open_totals(written: dict, res: compute.Result) -> dict[tuple[str, int], compute.Issue]:
    """Written column totals that do not match and that nobody has accepted yet."""
    return {
        key: issue
        for key, issue in res.total_issues.items()
        if compute.open_issues([issue], written[key].accepted if key in written else None)
    }


def _state(values: dict, res: compute.Result, written: dict) -> tuple[int, int]:
    """(cells a person must still check: unclear readings, open arithmetic checks incl. column totals)."""
    uncertain = sum(1 for v in values.values() if v.uncertain)
    checks = sum(
        1
        for key, issues in res.issues.items()
        if not (values.get(key) is not None and values[key].uncertain)
        and compute.open_issues(issues, values[key].accepted if values.get(key) is not None else None)
    )
    return uncertain, checks + len(_open_totals(written, res))


def register_view(conn: Connection, principal: Principal, register_id: uuid.UUID) -> dict[str, Any]:
    row = _load(conn, principal, register_id)
    values, written = _values(conn, row.id), _written(conn, row.id)
    res = _calculate(conn, row, values, written)
    shifts = []
    for sh in SHIFTS:
        machines = []
        for m in res.machines[sh]:
            cells = []
            for k in SLOTS:
                v = values.get((sh, m, k))
                issues = res.issues.get((sh, m, k), [])
                open_ = compute.open_issues(issues, v.accepted if v is not None else None)
                cells.append(
                    {
                        "slot": k,
                        "reading": _s(v.reading) if v is not None else None,
                        "picks": _s(v.picks) if v is not None else None,
                        "status": v.status if v is not None else None,
                        "status_label": status_label(v.status) if v is not None else "",
                        "source": v.source if v is not None else None,
                        "uncertain": bool(v.uncertain) if v is not None else False,
                        "note": v.note if v is not None else None,
                        "raw": v.raw if v is not None else None,
                        "evidence": v.evidence if v is not None else [],
                        "checks": [{"code": i.code, "text": i.text, "suggestion": i.suggestion} for i in open_],
                        "accepted": [i.text for i in issues if i not in open_],
                    }
                )
            machines.append({"machine": m, "cells": cells, "total": _s(res.machine_total[(sh, m)])})
        columns = []
        open_totals = _open_totals(written, res)
        for k in SLOTS:
            w = written.get((sh, k))
            issue = res.total_issues.get((sh, k))
            columns.append(
                {
                    "slot": k,
                    "time": TIMES[sh][k],
                    "calculated": _s(res.column_total[(sh, k)]),
                    "written": _s(w.written) if w is not None else None,
                    "match": res.total_match.get((sh, k)),
                    "check": open_totals[(sh, k)].text if (sh, k) in open_totals else None,
                    "accepted": issue is not None and (sh, k) not in open_totals,
                    "stopped": res.stopped[(sh, k)],
                }
            )
        shifts.append(
            {
                "shift": sh,
                "hours": SHIFT_HOURS[sh],
                "times": list(TIMES[sh]),
                "machines": machines,
                "columns": columns,
                "total": _s(res.shift_total[sh]),
                "written_total": _s(written[(sh, 0)].written) if (sh, 0) in written else None,
            }
        )
    uncertain, checks = _state(values, res, written)
    dept = conn.execute(select(t.department.c.name).where(t.department.c.id == row.department_id)).scalar_one()
    names = dict(conn.execute(select(t.membership.c.id, t.membership.c.display_name)).all())
    sources = conn.execute(
        select(ps, t.upload.c.display_name)
        .join(t.upload, t.upload.c.id == ps.c.upload_id)
        .where(ps.c.register_id == row.id)
        .order_by(ps.c.created_at)
    ).all()
    changes = conn.execute(
        select(pc).where(pc.c.register_id == row.id).order_by(pc.c.created_at.desc()).limit(200)
    ).all()
    emails = conn.execute(select(re_).where(re_.c.register_id == row.id).order_by(re_.c.created_at.desc())).all()
    return {
        "id": str(row.id),
        "department": dept,
        "department_id": str(row.department_id),
        "register_date": row.register_date.isoformat(),
        "date_confirmed": row.date_confirmed,
        "state": row.state,
        "notes": row.notes or [],
        "findings": res.notes,
        "shifts": shifts,
        "day_total": _s(res.day_total),
        "uncertain": uncertain,
        "checks": checks,
        "approvable": row.state == "DRAFT" and uncertain == 0 and checks == 0 and row.date_confirmed and bool(values),
        "approved_version": row.approved_version,
        "approved_by": names.get(row.approved_by),
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "sources": [
            {
                "upload_id": str(x.upload_id),
                "batch_id": str(x.batch_id),
                "file": x.display_name,
                "page_no": x.page_no,
                "shift": x.shift,
                "reader": x.reader,
                "values_read": x.values_read,
                "conflicts": x.conflicts,
            }
            for x in sources
        ],
        "changes": [
            {
                "shift": c.shift,
                "machine": c.machine,
                "slot": c.slot,
                "time": TIMES[c.shift][c.slot] if c.shift in TIMES else "",
                "field": c.field,
                "old": c.old_value,
                "new": c.new_value,
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


def list_registers(
    conn: Connection,
    principal: Principal,
    date_from: date | None,
    date_to: date | None,
    state: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not principal.has_any(*READ_ROLES):
        raise forbidden()
    q = (
        select(pr, t.department.c.name.label("dept"))
        .join(t.department, t.department.c.id == pr.c.department_id)
        .where(pr.c.department_id.in_(list(principal.department_ids)))
    )
    if date_from:
        q = q.where(pr.c.register_date >= date_from)
    if date_to:
        q = q.where(pr.c.register_date <= date_to)
    if state:
        q = q.where(pr.c.state == state)
    out = []
    for r in conn.execute(q.order_by(pr.c.register_date.desc()).limit(limit)).all():
        values, written = _values(conn, r.id), _written(conn, r.id)
        res = _calculate(conn, r, values, written)
        uncertain, checks = _state(values, res, written)
        last_email = conn.execute(
            select(re_.c.state).where(re_.c.register_id == r.id).order_by(re_.c.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        out.append(
            {
                "id": str(r.id),
                "register_date": r.register_date.isoformat(),
                "department": r.dept,
                "state": r.state,
                "shifts": {sh: _s(res.shift_total[sh]) for sh in SHIFTS},
                "present": [sh for sh in SHIFTS if res.machines[sh]],
                "machines": len({m for sh in SHIFTS for m in res.machines[sh]}),
                "day_total": _s(res.day_total),
                "uncertain": uncertain,
                "checks": checks,
                "last_email_state": last_email,
                "updated_at": r.updated_at.isoformat(),
                "version": r.version,
            }
        )
    return out


# --- editing and approval --------------------------------------------------------------------


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
    if v < 0 or v >= Decimal("1e12") or v != v.quantize(Decimal("0.0001")):
        raise ApiError(
            422, "VALIDATION_FAILED", f'"{text}" is not a valid reading.',
            [Issue("INVALID_NUMBER", "Zero or more, at most 4 decimal places.", where)],
        )  # fmt: skip
    return v


def _where(shift: str, machine: str, slot: int) -> str:
    return f"{shift}.{machine}.{slot}"


def _cell_key(c: dict[str, Any]) -> tuple[str, str, int]:
    shift, slot = c.get("shift"), c.get("slot")
    machine = machine_key(str(c.get("machine", "")))
    if shift not in SHIFTS or machine is None or not isinstance(slot, int) or slot not in SLOTS:
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            "Unknown cell of the register.",
            [
                Issue(
                    "UNKNOWN_CELL", "Shift I/II/III, a machine number and a time.", f"{shift}.{c.get('machine')}.{slot}"
                )
            ],
        )
    return shift, machine, slot


def patch_register(
    conn: Connection, principal: Principal, register_id: uuid.UUID, expected_version: int, body: dict[str, Any]
) -> dict[str, Any]:
    if not principal.has_any(*EDIT_ROLES):
        raise forbidden("Only Uploaders and Reviewers edit registers.")
    row = _load(conn, principal, register_id, lock=True)
    if row.version != expected_version:
        raise precondition_failed(row.version)
    approved = row.state == "APPROVED"
    reason = (body.get("reason") or "").strip() or None
    if approved:
        if not principal.has_any(Role.REVIEWER):
            raise forbidden("An approved register is changed by a Reviewer only.")
        if not reason or len(reason) < 5:
            raise ApiError(
                422,
                "VALIDATION_FAILED",
                "Give a reason for changing an approved register (5+ characters).",
                [Issue("REASON_REQUIRED", "A reason is required.", "reason")],
            )
    source = "reviewer" if principal.has_any(Role.REVIEWER) else "manual"
    values = _values(conn, row.id)
    changed = 0

    def log(key: tuple[str, str, int], field: str, old: Any, new: Any) -> None:
        conn.execute(
            insert(pc).values(
                id=uuid.uuid4(),
                tenant_id=row.tenant_id,
                register_id=row.id,
                shift=key[0],
                machine=key[1],
                slot=key[2],
                field=field,
                old_value=(_s(old) or None) and _s(old)[:60],
                new_value=(_s(new) or None) and _s(new)[:60],
                reason=reason,
                actor_id=principal.membership_id,
            )
        )

    for c in body.get("values", []):
        key = _cell_key(c)
        new: dict[str, Any] = {}
        if "reading" in c:
            new["reading"] = _number(c["reading"], _where(*key) + ".reading")
        if "picks" in c:
            new["picks"] = _number(c["picks"], _where(*key) + ".picks")
            if key[2] == 0 and new["picks"] is not None:
                raise ApiError(
                    422, "VALIDATION_FAILED", "The start reading has no picks.",
                    [Issue("NO_PICKS_AT_START", "The first time of a shift is the start reading only.", _where(*key))],
                )  # fmt: skip
        if "status" in c:
            new["status"] = (str(c["status"] or "").strip().upper()[:40]) or None
        old = values.get(key)
        before = {f: getattr(old, f) if old is not None else None for f in new}
        diff = {f: v for f, v in new.items() if before[f] != v}
        if not diff and not (old is not None and old.uncertain and new):
            continue
        if old is None:
            if all(v is None for v in new.values()):
                continue
            conn.execute(
                insert(pv).values(
                    id=uuid.uuid4(),
                    tenant_id=row.tenant_id,
                    register_id=row.id,
                    shift=key[0],
                    machine=key[1],
                    slot=key[2],
                    reading=new.get("reading"),
                    picks=new.get("picks"),
                    status=new.get("status"),
                    source=source,
                    updated_by=principal.membership_id,
                )
            )
        else:
            conn.execute(
                update(pv)
                .where(pv.c.id == old.id)
                .values(
                    **diff,
                    source=source,
                    uncertain=False,
                    note=None,
                    updated_by=principal.membership_id,
                    updated_at=func.now(),
                )
            )
        for f, v in diff.items():
            log(key, f, before[f], v)
        changed += 1
    for c in body.get("totals", []):
        shift, slot = c.get("shift"), c.get("slot")
        if shift not in SHIFTS or slot not in SLOTS:
            raise ApiError(422, "VALIDATION_FAILED", "Unknown total of the register.")
        value = _number(c.get("value"), f"{shift}.total.{slot}")
        old = conn.execute(select(pt).where(pt.c.register_id == row.id, pt.c.shift == shift, pt.c.slot == slot)).first()
        if (old.written if old else None) == value:
            continue
        stmt = pg_insert(pt).values(
            id=uuid.uuid4(), tenant_id=row.tenant_id, register_id=row.id, shift=shift, slot=slot, written=value,
            source=source, updated_by=principal.membership_id,
        )  # fmt: skip
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["register_id", "shift", "slot"],
                set_={
                    "written": value,
                    "source": source,
                    "updated_by": principal.membership_id,
                    "updated_at": func.now(),
                },
            )
        )
        log((shift, "TOTAL", slot), "total", old.written if old else None, value)
        changed += 1
    confirms = body.get("confirm", [])
    if confirms:  # the cell is right as it is: unclear reading confirmed, and its current checks accepted
        values = _values(conn, row.id)
        res = _calculate(conn, row, values)
        written = _written(conn, row.id)
        for c in confirms:
            if c.get("total"):  # a written column total that does not match: accepted as it is
                tkey = (c.get("shift"), c.get("slot"))
                issue, w = res.total_issues.get(tkey), written.get(tkey)
                if issue is not None and w is not None and w.accepted != issue.signature:
                    conn.execute(update(pt).where(pt.c.id == w.id).values(accepted=issue.signature))
                    changed += 1
                continue
            key = _cell_key(c)
            v = values.get(key)
            if v is None:
                continue
            accepted = compute.signatures(res.issues.get(key, []))
            if v.uncertain or (accepted and accepted != (v.accepted or "")):
                conn.execute(
                    update(pv)
                    .where(pv.c.id == v.id)
                    .values(
                        uncertain=False, note=None, accepted=accepted or v.accepted, updated_by=principal.membership_id
                    )
                )
                changed += 1
    values_upd: dict[str, Any] = {"version": pr.c.version + 1}
    if "notes" in body:
        values_upd["notes"] = [
            {"label": str(n.get("label", "Other"))[:80], "text": str(n.get("text", ""))[:300]}
            for n in body["notes"]
            if str(n.get("text", "")).strip()
        ][:50]
    if body.get("register_date"):
        new_day = date.fromisoformat(body["register_date"])
        if new_day != row.register_date:
            clash = conn.execute(
                select(pr.c.id).where(pr.c.department_id == row.department_id, pr.c.register_date == new_day)
            ).first()
            if clash:
                raise conflict("DATE_TAKEN", f"There is already a register for {new_day:%d %b %Y}. Open that register.")
            values_upd["register_date"] = new_day
        values_upd["date_confirmed"] = True
    conn.execute(update(pr).where(pr.c.id == row.id).values(**values_upd))
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="REGISTER_EDITED",
        object_type="pick_register",
        object_id=row.id,
        object_revision=row.version + 1,
        reason=reason,
        after={"cells": changed, "approved_register": approved},
    )
    return register_view(conn, principal, row.id)


def approve(conn: Connection, principal: Principal, register_id: uuid.UUID, expected_version: int) -> dict[str, Any]:
    if not principal.has_any(Role.REVIEWER):
        raise forbidden("Only Reviewers approve registers.")
    row = _load(conn, principal, register_id, lock=True)
    if row.version != expected_version:
        raise precondition_failed(row.version)
    if row.state == "APPROVED":
        raise conflict("ALREADY_APPROVED", "This register is already approved.")
    values = _values(conn, row.id)
    res = _calculate(conn, row, values)
    problems = []
    if not row.date_confirmed:
        problems.append(
            Issue("DATE_NOT_CONFIRMED", "The date was not found on the page. Confirm the date.", "register_date")
        )
    if not values:
        problems.append(Issue("EMPTY", "Nothing is entered in this register.", None))
    for key in sorted(values, key=lambda k: (SHIFTS.index(k[0]), machine_sort(k[1]), k[2])):
        v = values[key]
        sh, m, k = key
        where = f"Shift {sh}, machine {m}, {TIMES[sh][k]}"
        if v.uncertain:
            problems.append(Issue("CONFIRM_VALUE", f"{where}: {v.note or 'check against the photo'}", _where(*key)))
        else:
            for i in compute.open_issues(res.issues.get(key, []), v.accepted):
                problems.append(Issue(i.code, f"{where}: {i.text}", _where(*key)))
    for (sh, k), i in sorted(
        _open_totals(_written(conn, row.id), res).items(), key=lambda x: (SHIFTS.index(x[0][0]), x[0][1])
    ):
        problems.append(Issue(i.code, f"Shift {sh}, total {TIMES[sh][k]}: {i.text}", f"{sh}.TOTAL.{k}"))
    if problems:
        raise ApiError(422, "VALIDATION_FAILED", f"{len(problems)} value(s) must be checked before approval.", problems)
    conn.execute(
        update(pr)
        .where(pr.c.id == row.id)
        .values(
            state="APPROVED",
            approved_version=pr.c.approved_version + 1,
            approved_by=principal.membership_id,
            approved_at=func.now(),
            version=pr.c.version + 1,
        )
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="REGISTER_APPROVED",
        object_type="pick_register",
        object_id=row.id,
        object_revision=row.approved_version + 1,
    )
    return register_view(conn, principal, row.id)


def for_batch(conn: Connection, principal: Principal, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    """Registers that pages of this upload batch were read into (listed with the batch's daily sheets)."""
    rows = conn.execute(
        select(
            pr,
            func.sum(ps.c.values_read).label("n"),
            func.array_agg(func.distinct(ps.c.upload_id)).label("uploads"),
        )
        .join(ps, ps.c.register_id == pr.c.id)
        .where(ps.c.batch_id == batch_id)
        .group_by(pr.c.id)
        .order_by(pr.c.register_date)
    ).all()
    out = []
    for r in rows:
        if not principal.can_access_department(r.department_id):
            continue
        values, written = _values(conn, r.id), _written(conn, r.id)
        uncertain, checks = _state(values, _calculate(conn, r, values, written), written)
        out.append(
            {
                "id": str(r.id),
                "kind": "register",
                "report_date": r.register_date.isoformat(),
                "state": r.state,
                "values_read": int(r.n or 0),
                "upload_ids": sorted(str(u) for u in r.uploads),
                "uncertain": uncertain + checks,
            }
        )
    return out


def batch_counts(conn: Connection, batch_id: uuid.UUID) -> dict[str, int]:
    return dict(
        conn.execute(
            select(pr.c.state, func.count(func.distinct(pr.c.id)))
            .join(ps, ps.c.register_id == pr.c.id)
            .where(ps.c.batch_id == batch_id)
            .group_by(pr.c.state)
        ).all()
    )


# --- files and email -------------------------------------------------------------------------


def file_for(conn: Connection, principal: Principal, register_id: uuid.UUID, fmt: str) -> tuple[bytes, str, str]:
    from app.pick_registers import export

    row = _load(conn, principal, register_id)
    return export.render(conn, row, fmt)


def start_email(
    conn: Connection, principal: Principal, register_id: uuid.UUID, to_email: str, fmt: str, version: int
) -> dict[str, Any]:
    from app.owner_reports import settings as owner_settings
    from app.pick_registers import export

    if not principal.has_any(*SEND_ROLES):
        raise forbidden("Reviewers, Senders and administrators email registers.")
    address = to_email.strip()
    if not _EMAIL.match(address):
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            "Enter one valid email address.",
            [Issue("INVALID_EMAIL", f'"{address}" is not a valid email address.', "to_email")],
        )
    if fmt not in FORMATS:
        raise ApiError(422, "VALIDATION_FAILED", "Choose xlsx, pdf, csv or sql.")
    row = _load(conn, principal, register_id, lock=True)
    if row.version != version:
        raise conflict("REGISTER_CHANGED", "This register changed since you opened it. Reload it and send again.")
    if owner_settings.missing(owner_settings.load(conn, row.tenant_id)):
        raise conflict(
            "EMAIL_NOT_CONFIGURED",
            "Email is not set up yet. An administrator completes Settings -> Owner report & email.",
        )
    busy = conn.execute(
        select(re_.c.id).where(
            re_.c.register_id == row.id,
            func.lower(re_.c.to_email) == address.lower(),
            re_.c.format == fmt,
            re_.c.state.in_(("QUEUED", "SENDING")),
        )
    ).first()
    if busy:
        raise conflict("ALREADY_SENDING", f"This register is already being sent to {address}.")
    data, name, mime = export.render(conn, row, fmt)
    email_id = uuid.uuid4()
    # The exact file is stored now and attached later, so the recipient gets precisely what was queued.
    get_storage().put_bytes(object_key_for("exports", row.tenant_id, email_id, f".{fmt}"), data, mime)
    conn.execute(
        insert(re_).values(
            id=email_id,
            tenant_id=row.tenant_id,
            register_id=row.id,
            register_version=row.version,
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
        action="REGISTER_EMAIL_QUEUED",
        object_type="pick_register",
        object_id=row.id,
        after={"email_id": str(email_id), "format": fmt},
    )
    return email_view(conn.execute(select(re_).where(re_.c.id == email_id)).one())


def email_params(conn: Connection, e: Any) -> tuple[dict[str, str], bytes]:
    """EmailJS variables for one register email: picks per shift in the message and as a table, the file attached."""
    import base64

    from app.owner_reports.settings import company_name
    from app.pick_registers import export

    row = conn.execute(select(pr).where(pr.c.id == e.register_id)).one()
    data = get_storage().get_bytes(object_key_for("exports", e.tenant_id, e.id, f".{e.format}"), 20_000_000)
    if hashlib.sha256(data).hexdigest() != e.attachment_sha256:
        raise RuntimeError("the stored file does not match the one recorded for this email")
    name, mime = e.attachment_name, export.MIME[e.format]
    res = _calculate(conn, row)
    company = company_name(conn, row.tenant_id)
    dept = conn.execute(select(t.department.c.name).where(t.department.c.id == row.department_id)).scalar_one()
    lines, html_rows = [], []
    for sh in SHIFTS:
        if not res.machines[sh]:
            continue
        cols = [_s(res.column_total[(sh, k)]) or "-" for k in SLOTS[1:]]
        stopped = max(res.stopped[(sh, k)] for k in SLOTS)
        lines.append(
            f"Shift {sh} ({SHIFT_HOURS[sh]}): picks {_s(res.shift_total[sh]) or '-'} "
            f"({', '.join(f'{TIMES[sh][k]} {c}' for k, c in zip(SLOTS[1:], cols, strict=True))}); "
            f"{len(res.machines[sh])} machines, up to {stopped} stopped"
        )
        html_rows.append(
            "<tr>"
            + "".join(
                f'<td style="border:1px solid #ccc;padding:4px">{x}</td>'
                for x in (f"Shift {sh}", *cols, _s(res.shift_total[sh]) or "-", len(res.machines[sh]), stopped)
            )
            + "</tr>"
        )
    title = f"Pick reading register {row.register_date:%d %b %Y} - {dept}"
    message = (
        f"Hello,\n\nPlease find attached the hourly production reading register (pick reading) for "
        f"{row.register_date:%d %b %Y} ({dept}, {'approved' if row.state == 'APPROVED' else 'not yet approved'}).\n\n"
        + "\n".join(lines)
        + f"\nDay total picks: {_s(res.day_total) or '-'}\n\nThe full register is attached ({name}).\n\n{company}"
    )
    head = "".join(
        f'<th style="border:1px solid #ccc;padding:4px;background:#eef2f7">{h}</th>'
        for h in ("", "Slot 1", "Slot 2", "Slot 3", "Slot 4", "Picks", "Machines", "Stopped")
    )
    encoded = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    params = {
        "to_email": e.to_email,
        "subject": title,
        "title": title,
        "message": message,
        "name": company,
        "email": e.to_email,
        "record_reference": f"{row.register_date:%Y-%m-%d} {dept} WGS-02",
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
        # The template's variable attachment "pdf_file" is a PDF slot; Excel / CSV / SQL go in "sheet_file".
        "pdf_file": encoded if e.format == "pdf" else "",
        "sheet_file": encoded if e.format != "pdf" else "",
        "sheet_file_name": name,
    }
    return params, data


def export_context(conn: Connection, row: Any) -> dict[str, Any]:
    """Everything the files need: values, written totals, calculated results, names."""
    from app.owner_reports.settings import company_name

    values, written = _values(conn, row.id), _written(conn, row.id)
    dept = conn.execute(select(t.department.c.name).where(t.department.c.id == row.department_id)).scalar_one()
    return {
        "values": values,
        "written": written,
        "res": _calculate(conn, row, values, written),
        "dept": dept,
        "company": company_name(conn, row.tenant_id),
    }
