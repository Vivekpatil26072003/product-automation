"""Customer orders: review drafts, approve into orders, correct with revisions, render PDFs, queue email sends.

Access (same model as production records):
- Uploader: edits order drafts of their own batches. Reviewer: edits, confirms, approves, rejects drafts and
  corrects orders in granted departments. Reviewer and Sender: email an order's PDF. Viewer: read only.
- Every read and write is limited to the caller's departments (and the tenant, by row-level security).
Saving never overwrites: approval creates revision 1 (or, when the reviewer chooses to update an existing
order, that order's next revision), each correction appends a revision with a reason, and the order's current
revision always points at the latest saved values. A draft that looks like a saved order cannot be approved
until the reviewer decides "new order" or "update that order", so duplicates are never created by accident.
"""

import base64
import hashlib
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, and_, func, insert, or_, select, update

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, Issue, conflict, forbidden, not_found, precondition_failed
from app.db import tables as t
from app.domain.enums import Role
from app.ingestion import service as ingestion
from app.jobs import ledger
from app.mail import template_vars
from app.orders import customers
from app.orders import fields as of
from app.orders import pdf as order_pdf
from app.review.service import evidence_for

od, o, ov, oe = t.order_draft, t.customer_order, t.order_revision, t.order_email
READ_ROLES = (Role.REVIEWER, Role.SENDER, Role.VIEWER)
SEND_ROLES = (Role.REVIEWER, Role.SENDER)
EMAIL_KIND = "order.email"
STALE_MINUTES = 15
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;.]{2,}$")
FIELD_KEYS = ("value", "display", "raw", "source", "confidence", "uncertain", "note", "corrected_from")
MAX_EXTRA = 30


def _date_order(conn: Connection, tenant_id: uuid.UUID) -> str:
    return conn.execute(select(t.tenant.c.date_order).where(t.tenant.c.id == tenant_id)).scalar_one()


def _can_edit(principal: Principal, batch: Any) -> bool:
    return principal.has_any(Role.REVIEWER) or (
        principal.has_any(Role.UPLOADER) and batch.owner_id == principal.membership_id
    )


def _issue(field: str | None, code: str, message: str, severity: str) -> dict[str, Any]:
    return {"field": field, "code": code, "message": message, "severity": severity}


# --- drafts (review form) --------------------------------------------------------------------


def _matching_orders(conn: Connection, values: dict[str, Any], department_id: uuid.UUID) -> list[dict[str, Any]]:
    """Saved orders this draft may repeat: same order number, or same customer + order date + quantity."""
    cur = ov.alias("cur")
    same_content = and_(
        func.lower(cur.c.customer_name) == (values.get("customer_name") or "").lower(),
        cur.c.order_date == values.get("order_date"),
        cur.c.quantity == values.get("quantity"),
    )
    cond = (
        or_(same_content, cur.c.order_number == values["order_number"]) if values.get("order_number") else same_content
    )
    rows = conn.execute(
        select(o.c.id, o.c.order_ref, cur.c.customer_name, cur.c.order_date, cur.c.quantity, cur.c.total)
        .join(cur, cur.c.id == o.c.current_revision_id)
        .where(o.c.state == "ACTIVE", o.c.department_id == department_id, cond)
        .order_by(o.c.updated_at.desc())
        .limit(3)
    ).all()
    return [
        {
            "order_id": str(r.id),
            "order_ref": r.order_ref,
            "customer_name": r.customer_name,
            "order_date": _fmt(r.order_date),
            "quantity": _fmt(r.quantity),
            "total": _fmt(r.total),
        }
        for r in rows
    ]


def _live_issues(conn: Connection, row: Any, stored: list[dict[str, Any]] | None = None) -> tuple[list, list]:
    """Stored field issues + checks that can change after the draft was saved (duplicates, model release)."""
    issues = list(row.issues if stored is None else stored)
    matches: list[dict[str, Any]] = []
    if row.state == "NEEDS_REVIEW":
        matches = _matching_orders(conn, of.revision_values(row.fields), row.department_id)
        decision = row.decision or {}
        if matches and not decision.get("mode"):
            refs = ", ".join(m["order_ref"] for m in matches)
            issues.append(
                _issue(
                    None,
                    "DUPLICATE_DECISION",
                    f"This looks like an order already saved ({refs}). Choose "
                    "whether to update that order or save a new one.",
                    "error",
                )
            )
        if (row.reading or {}).get("release") == "UNEVALUATED":
            issues.append(
                _issue(
                    None,
                    "UNEVALUATED_MODEL",
                    "Read by an AI model version that has not passed evaluation. Check every value against the page.",
                    "warning",
                )
            )
    return issues, matches


def draft_view(conn: Connection, row: Any) -> dict[str, Any]:
    issues, matches = _live_issues(conn, row)
    ids = {e for f in row.fields.values() for e in f.get("evidence_ids", [])}
    ids |= {e for x in (row.extra or []) for e in x.get("evidence_ids", [])}
    evidence = evidence_for(conn, row.upload_id, ids)
    upload = conn.execute(select(t.upload.c.display_name).where(t.upload.c.id == row.upload_id)).scalar_one()
    reading = row.reading or {}
    return {
        "id": str(row.id),
        "batch_id": str(row.batch_id),
        "upload_id": str(row.upload_id),
        "file_name": upload,
        "page_no": row.page_no,
        "source": row.source,
        "state": row.state,
        "fields": {
            n: {k: (row.fields.get(n) or {}).get(k) for k in FIELD_KEYS}
            | {"evidence": [evidence[e] for e in (row.fields.get(n) or {}).get("evidence_ids", []) if e in evidence]}
            for n in of.FIELDS
        },
        "extra": [
            {
                "label": x["label"],
                "value": x["value"],
                "evidence": [evidence[e] for e in x.get("evidence_ids", []) if e in evidence],
            }
            for x in (row.extra or [])
        ],
        "reading": {k: reading.get(k) for k in ("reader", "model", "languages", "release", "warnings")},
        "issues": issues,
        "matches": matches,
        "decision": row.decision,
        "approvable": row.state == "NEEDS_REVIEW" and not any(i["severity"] == "error" for i in issues),
        "order_id": str(row.order_id) if row.order_id else None,
        "reject_reason": row.reject_reason,
        "version": row.version,
        "updated_at": row.updated_at.isoformat(),
    }


def load_draft(conn: Connection, principal: Principal, draft_id: uuid.UUID, lock: bool = False) -> tuple[Any, Any]:
    q = select(od).where(od.c.id == draft_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None:
        raise not_found()
    batch = ingestion.load_batch(conn, principal, row.batch_id)  # batch scope (department, owner) still granted
    if not _can_edit(principal, batch):
        raise not_found()
    return row, batch


def list_drafts(conn: Connection, principal: Principal, batch_id: uuid.UUID, include_closed: bool) -> list[dict]:
    batch = ingestion.load_batch(conn, principal, batch_id)
    if not _can_edit(principal, batch):
        raise not_found()
    q = select(od).where(od.c.batch_id == batch.id)
    if not include_closed:
        q = q.where(od.c.state == "NEEDS_REVIEW")
    position = func.coalesce(od.c.reading["position"].as_integer(), 0)  # order as written on the page
    return [draft_view(conn, r) for r in conn.execute(q.order_by(od.c.page_no, position, od.c.created_at, od.c.id))]


def _insert_draft(
    conn: Connection,
    *,
    upload: Any,
    batch: Any,
    source: str,
    inputs: dict,
    date_order: str,
    extraction_id: uuid.UUID | None,
    page_no: int,
    created_by: uuid.UUID | None,
    extra: list | None = None,
    reading: dict | None = None,
) -> uuid.UUID:
    norm = of.normalize(inputs, date_order)
    draft_id = uuid.uuid4()
    conn.execute(
        insert(od).values(
            id=draft_id,
            tenant_id=upload.tenant_id,
            batch_id=batch.id,
            upload_id=upload.id,
            department_id=batch.department_id,
            source=source,
            extraction_id=extraction_id,
            page_no=page_no,
            fields=norm.fields,
            issues=norm.issues,
            extra=(extra or [])[:MAX_EXTRA],
            reading=reading or {},
            created_by=created_by,
        )
    )
    return draft_id


def create_from_reading(
    conn: Connection,
    *,
    upload: Any,
    batch: Any,
    extraction_id: uuid.UUID,
    page_no: int,
    inputs: dict[str, dict[str, Any]],
    extra: list[dict[str, Any]],
    reading: dict[str, Any],
) -> uuid.UUID:
    """Called by the extraction worker for each order read from a page (label reader or AI)."""
    return _insert_draft(
        conn,
        upload=upload,
        batch=batch,
        source="extracted",
        inputs=inputs,
        date_order=_date_order(conn, upload.tenant_id),
        extraction_id=extraction_id,
        page_no=page_no,
        created_by=None,
        extra=extra,
        reading=reading,
    )


def create_manual(conn: Connection, principal: Principal, upload_id: uuid.UUID, page_no: int = 1) -> dict[str, Any]:
    """An empty order form for a file (for example a photo no reader could read), with the file beside it."""
    up, batch = ingestion.load_upload(conn, principal, upload_id)
    if not _can_edit(principal, batch):
        raise not_found()
    if up.state != "READY":
        raise conflict("SOURCE_NOT_SCANNED", "Manual entry is available once the file has passed scanning.")
    inputs = {n: {"raw": None, "evidence_ids": [], "source": "manual"} for n in of.FIELDS}
    draft_id = _insert_draft(
        conn,
        upload=up,
        batch=batch,
        source="manual",
        inputs=inputs,
        date_order=_date_order(conn, up.tenant_id),
        extraction_id=None,
        page_no=max(1, min(page_no, up.page_count or 1)),
        created_by=principal.membership_id,
        reading={"reader": "manual"},
    )
    audit.record(
        conn,
        tenant_id=up.tenant_id,
        actor=principal.actor,
        action="ORDER_DRAFT_MANUAL_CREATED",
        object_type="order_draft",
        object_id=draft_id,
    )
    return draft_view(conn, conn.execute(select(od).where(od.c.id == draft_id)).one())


def _clean_extra(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for x in items[:MAX_EXTRA]:
        label, value = str(x.get("label") or "").strip()[:100], str(x.get("value") or "").strip()[:500]
        if label and value:
            out.append({"label": label, "value": value, "evidence_ids": list(x.get("evidence_ids") or [])})
    return out


def patch_draft(
    conn: Connection,
    principal: Principal,
    draft_id: uuid.UUID,
    expected_version: int,
    changes: dict[str, str | None],
    confirm: list[str] | None = None,
    extra: list[dict[str, Any]] | None = None,
    decision: dict[str, Any] | None = None,
    decision_set: bool = False,
) -> dict[str, Any]:
    """Autosave: corrected values, values confirmed as read, other information, and the duplicate decision."""
    row, _ = load_draft(conn, principal, draft_id, lock=True)
    if row.state != "NEEDS_REVIEW":
        raise conflict("IMMUTABLE", f"This order draft is {row.state.lower()} and can no longer be edited.")
    if row.version != expected_version:
        raise precondition_failed(row.version)
    inputs = of.inputs_from_stored(row.fields)
    diff: dict[str, Any] = {}
    for name, text in changes.items():
        text = None if text is None else str(text)
        before = (inputs.get(name) or {}).get("raw")
        if (text or None) == (before or None):
            continue
        diff[name] = {"from": (row.fields.get(name) or {}).get("value"), "to_raw": text}
        # A corrected value is the reviewer's; the source line no longer vouches for it.
        inputs[name] = {"raw": text, "evidence_ids": [], "source": "reviewer"}
    confirmed = []
    for name in confirm or []:
        current = inputs.get(name) or {}
        if name in of.FIELDS and current.get("raw") and current.get("source") in ("extracted", "ai"):
            inputs[name] = {
                "raw": current["raw"],
                "evidence_ids": current.get("evidence_ids", []),
                "source": "reviewer",
            }
            confirmed.append(name)
    values: dict[str, Any] = {}
    if extra is not None:
        values["extra"] = _clean_extra(extra)
    if decision_set:
        values["decision"] = _check_decision(conn, principal, row, decision)
    norm = of.normalize(inputs, _date_order(conn, row.tenant_id))
    conn.execute(
        update(od)
        .where(od.c.id == row.id)
        .values(fields=norm.fields, issues=norm.issues, version=od.c.version + 1, **values)
    )
    if diff or confirmed or values:
        audit.record(
            conn,
            tenant_id=row.tenant_id,
            actor=principal.actor,
            action="ORDER_DRAFT_EDITED",
            object_type="order_draft",
            object_id=row.id,
            object_revision=row.version + 1,
            after={
                "fields": sorted(diff),
                "confirmed": sorted(confirmed),
                "decision": values.get("decision"),
                "extra": "extra" in values,
            },
        )
    return draft_view(conn, conn.execute(select(od).where(od.c.id == row.id)).one())


def _check_decision(conn: Connection, principal: Principal, row: Any, decision: dict | None) -> dict | None:
    if decision is None:
        return None
    if not principal.has_any(Role.REVIEWER):
        raise forbidden("Only Reviewers decide whether an order is new or an update.")
    if decision.get("mode") == "new":
        return {"mode": "new"}
    if decision.get("mode") == "update":
        target = _load_order(conn, principal, uuid.UUID(str(decision.get("order_id"))))
        if target.state != "ACTIVE" or target.department_id != row.department_id:
            raise conflict("ORDER_NOT_UPDATABLE", "That order cannot be updated from this page.")
        return {"mode": "update", "order_id": str(target.id), "order_ref": target.order_ref}
    raise ApiError(
        422,
        "VALIDATION_FAILED",
        "Choose a new order or an order to update.",
        [Issue("INVALID_DECISION", "mode must be new or update.", "decision.mode")],
    )


def reject_draft(conn: Connection, principal: Principal, draft_id: uuid.UUID, reason: str) -> dict[str, Any]:
    if not principal.has_any(Role.REVIEWER):
        raise forbidden()
    row, _ = load_draft(conn, principal, draft_id, lock=True)
    if row.state != "NEEDS_REVIEW":
        raise conflict("IMMUTABLE", f"This order draft is already {row.state.lower()}.")
    conn.execute(
        update(od)
        .where(od.c.id == row.id)
        .values(
            state="REJECTED",
            reject_reason=reason,
            decided_by=principal.membership_id,
            decided_at=func.now(),
            version=od.c.version + 1,
        )
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="ORDER_DRAFT_REJECTED",
        object_type="order_draft",
        object_id=row.id,
        reason=reason,
    )
    from app.owner_reports import service as owner_reports

    owner_reports.after_decision(conn, principal, row.batch_id)
    return draft_view(conn, conn.execute(select(od).where(od.c.id == row.id)).one())


def _problems(issues: list[dict[str, Any]], where: str = "fields") -> list[Issue]:
    return [
        Issue(i["code"], i["message"], f"{where}.{i['field'] or 'order'}") for i in issues if i["severity"] == "error"
    ]


def _attention(norm: of.Normalized) -> list[dict[str, Any]]:
    """What a reader of the saved order should know: unresolved warnings and missing key values."""
    out = [
        {"field": i["field"], "code": i["code"], "message": i["message"]}
        for i in norm.issues
        if i["severity"] == "warning"
    ]
    for name in ("mobile", "delivery_date", "rate", "total"):
        if norm.fields[name]["value"] is None:
            out.append({"field": name, "code": "MISSING", "message": f"{of.LABEL[name]} not recorded."})
    return out


def approve_draft(conn: Connection, principal: Principal, draft_id: uuid.UUID, expected_version: int) -> dict:
    if not principal.has_any(Role.REVIEWER):
        raise forbidden()
    row, _ = load_draft(conn, principal, draft_id, lock=True)
    if row.version != expected_version:
        raise precondition_failed(row.version)
    if row.state != "NEEDS_REVIEW":
        raise conflict("IMMUTABLE", f"This order draft is already {row.state.lower()}.")
    if not principal.can_access_department(row.department_id):
        raise forbidden("You cannot approve orders for this department.")
    norm = of.normalize(of.inputs_from_stored(row.fields), _date_order(conn, row.tenant_id))
    live, _ = _live_issues(conn, row, norm.issues)
    problems = _problems(live)
    if problems:
        code = "DUPLICATE_DECISION_REQUIRED" if all(p.code == "DUPLICATE_DECISION" for p in problems) else None
        raise ApiError(
            409 if code else 422,
            code or "VALIDATION_FAILED",
            f"{len(problems)} problem(s) must be fixed; nothing was saved.",
            problems,
        )
    values = of.revision_values(norm.fields)
    upload = conn.execute(select(t.upload.c.display_name).where(t.upload.c.id == row.upload_id)).scalar_one()
    provenance = {
        "draft_id": str(row.id),
        "upload_id": str(row.upload_id),
        "page_no": row.page_no,
        "reading": row.reading,
        "fields": {
            n: {k: norm.fields[n].get(k) for k in ("raw", "source", "evidence_ids", "confidence")} for n in of.FIELDS
        },
    }
    customer_id = customers.link(conn, row.tenant_id, values)
    extra = [{"label": x["label"], "value": x["value"]} for x in (row.extra or [])]
    decision = row.decision or {"mode": "new"}
    if decision["mode"] == "update":
        target = _load_order(conn, principal, uuid.UUID(decision["order_id"]), lock=True)
        order_id = target.id
        number = _append_revision(
            conn,
            principal,
            target,
            values,
            extra,
            _attention(norm),
            provenance,
            f"Updated from diary page ({upload}, page {row.page_no})",
            customer_id,
        )
        action, after = "ORDER_UPDATED_FROM_DIARY", {"draft_id": str(row.id), "revision": number}
    else:
        order_id, revision_id = uuid.uuid4(), uuid.uuid4()
        ref = values["order_number"] or f"ORD-{order_id.hex[:8].upper()}"  # the note's own number when it has one
        conn.execute(
            insert(o).values(
                id=order_id,
                tenant_id=row.tenant_id,
                department_id=row.department_id,
                draft_id=row.id,
                order_ref=ref,
                current_revision_id=revision_id,
                customer_id=customer_id,
                created_by=principal.membership_id,
            )
        )
        conn.execute(
            insert(ov).values(
                id=revision_id,
                tenant_id=row.tenant_id,
                order_id=order_id,
                number=1,
                provenance=provenance,
                extra=extra,
                attention=_attention(norm),
                created_by=principal.membership_id,
                **values,
            )
        )
        number, action, after = 1, "ORDER_APPROVED", {"draft_id": str(row.id), "order_ref": ref}
    conn.execute(
        update(od)
        .where(od.c.id == row.id)
        .values(
            state="APPROVED",
            order_id=order_id,
            decided_by=principal.membership_id,
            decided_at=func.now(),
            fields=norm.fields,
            issues=norm.issues,
            version=od.c.version + 1,
        )
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action=action,
        object_type="customer_order",
        object_id=order_id,
        object_revision=number,
        after=after,
    )
    from app.owner_reports import service as owner_reports

    owner_reports.after_decision(conn, principal, row.batch_id)
    return get_order(conn, principal, order_id)


def _append_revision(
    conn: Connection,
    principal: Principal,
    row: Any,
    values: dict[str, Any],
    extra: list,
    attention: list,
    provenance: dict,
    reason: str,
    customer_id: uuid.UUID,
) -> int:
    number, revision_id = row.current_number + 1, uuid.uuid4()
    conn.execute(
        insert(ov).values(
            id=revision_id,
            tenant_id=row.tenant_id,
            order_id=row.id,
            number=number,
            provenance=provenance,
            extra=extra,
            attention=attention,
            reason=reason,
            created_by=principal.membership_id,
            **values,
        )
    )
    ref = values["order_number"] or f"ORD-{row.id.hex[:8].upper()}"
    conn.execute(
        update(o)
        .where(o.c.id == row.id)
        .values(
            current_revision_id=revision_id,
            current_number=number,
            order_ref=ref,
            customer_id=customer_id,
            version=o.c.version + 1,
        )
    )
    return number


# --- orders ----------------------------------------------------------------------------------


def _scope(principal: Principal):
    if not principal.has_any(*READ_ROLES):
        raise forbidden()
    return o.c.department_id.in_(list(principal.department_ids))


def _fmt(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral() else format(v, "f")
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _values(rev: Any) -> dict[str, Any]:
    return {n: _fmt(getattr(rev, n)) for n in of.FIELDS}


def list_orders(
    conn: Connection, principal: Principal, q: str | None, limit: int, customer_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    last_email = (
        select(oe.c.state).where(oe.c.order_id == o.c.id).order_by(oe.c.created_at.desc()).limit(1).scalar_subquery()
    )
    stmt = (
        select(o, ov, t.department.c.name.label("department_name"), last_email.label("last_email_state"))
        .join(ov, ov.c.id == o.c.current_revision_id)
        .join(t.department, t.department.c.id == o.c.department_id)
        .where(_scope(principal), o.c.state == "ACTIVE")
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(ov.c.customer_name.ilike(like), o.c.order_ref.ilike(like), ov.c.mobile.ilike(like)))
    if customer_id:
        stmt = stmt.where(o.c.customer_id == customer_id)
    rows = conn.execute(stmt.order_by(o.c.updated_at.desc(), o.c.id.desc()).limit(limit)).all()
    return [
        {
            "id": str(r.id),
            "order_ref": r.order_ref,
            "department": r.department_name,
            "customer_id": str(r.customer_id) if r.customer_id else None,
            "revision": r.current_number,
            "values": _values(r),
            "attention": len(r.attention or []),
            "pdf_name": order_pdf.file_name(r.order_ref, r.current_number),
            "last_email_state": r.last_email_state,
            "updated_at": r.updated_at.isoformat(),
        }
        for r in rows
    ]


def _load_order(conn: Connection, principal: Principal, order_id: uuid.UUID, lock: bool = False) -> Any:
    q = select(o).where(o.c.id == order_id, _scope(principal))
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None:
        raise not_found()
    return row


def get_order(conn: Connection, principal: Principal, order_id: uuid.UUID) -> dict[str, Any]:
    row = _load_order(conn, principal, order_id)
    names = dict(conn.execute(select(t.membership.c.id, t.membership.c.display_name)).all())
    revisions = conn.execute(select(ov).where(ov.c.order_id == row.id).order_by(ov.c.number.desc())).all()
    current = next(r for r in revisions if r.id == row.current_revision_id)
    emails = conn.execute(select(oe).where(oe.c.order_id == row.id).order_by(oe.c.created_at.desc())).all()
    dept = conn.execute(select(t.department.c.name).where(t.department.c.id == row.department_id)).scalar_one()
    draft = conn.execute(select(od.c.upload_id, od.c.batch_id, od.c.page_no).where(od.c.id == row.draft_id)).one()
    customer = (
        conn.execute(select(t.customer.c.id, t.customer.c.name).where(t.customer.c.id == row.customer_id)).first()
        if row.customer_id
        else None
    )
    older = {r.number: _values(r) for r in revisions}
    return {
        "id": str(row.id),
        "order_ref": row.order_ref,
        "department": dept,
        "state": row.state,
        "customer": {"id": str(customer.id), "name": customer.name} if customer else None,
        "revision": row.current_number,
        "values": _values(current),
        "extra": current.extra or [],
        "attention": current.attention or [],
        "pdf_name": order_pdf.file_name(row.order_ref, row.current_number),
        "source": {"upload_id": str(draft.upload_id), "batch_id": str(draft.batch_id), "page_no": draft.page_no},
        "revisions": [
            {
                "number": r.number,
                "reason": r.reason,
                "by": names.get(r.created_by),
                "at": r.created_at.isoformat(),
                "changed": sorted(
                    n for n in of.FIELDS if r.number > 1 and older[r.number][n] != older[r.number - 1][n]
                ),
            }
            for r in revisions
        ],
        "emails": [email_view(e, names) for e in emails],
        "updated_at": row.updated_at.isoformat(),
        "version": row.current_number,
    }


def email_view(e: Any, names: dict | None = None) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "to_email": e.to_email,
        "revision": e.revision_number,
        "attachment": {"name": e.attachment_name, "bytes": e.attachment_bytes, "sha256": e.attachment_sha256},
        "state": e.state,
        "http_status": e.http_status,
        "error": {"code": e.error_code, "message": e.error_message} if e.error_code else None,
        "by": (names or {}).get(e.created_by),
        "at": e.created_at.isoformat(),
        "finished_at": e.finished_at.isoformat() if e.finished_at else None,
    }


def revise(
    conn: Connection,
    principal: Principal,
    order_id: uuid.UUID,
    expected_revision: int,
    changes: dict[str, str | None],
    reason: str,
    extra: list[dict[str, Any]] | None = None,
) -> dict:
    """Correct a saved order: a new revision with the reason; earlier revisions stay as they were."""
    if not principal.has_any(Role.REVIEWER):
        raise forbidden()
    row = _load_order(conn, principal, order_id, lock=True)
    if row.current_number != expected_revision:
        raise precondition_failed(row.current_number)
    if row.state != "ACTIVE":
        raise conflict("ARCHIVED", "This order is archived.")
    unknown = sorted(set(changes) - set(of.FIELDS))
    if unknown:
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            "Unknown fields.",
            [Issue("UNKNOWN_FIELD", f"{n} is not an order field.", f"fields.{n}") for n in unknown],
        )
    current = conn.execute(select(ov).where(ov.c.id == row.current_revision_id)).one()
    saved = _values(current)
    inputs = {n: {"raw": saved[n], "evidence_ids": [], "source": "saved"} for n in of.FIELDS}
    changed = []
    for n, text in changes.items():
        text = None if text is None else str(text)
        if (text or None) != (saved[n] or None):
            inputs[n] = {"raw": text, "evidence_ids": [], "source": "reviewer"}
            changed.append(n)
    new_extra = [{"label": x["label"], "value": x["value"]} for x in _clean_extra(extra)] if extra is not None else None
    extra_changed = new_extra is not None and new_extra != (current.extra or [])
    if not changed and not extra_changed:
        raise ApiError(422, "NO_CHANGES", "Nothing was changed.")
    norm = of.normalize(inputs, _date_order(conn, row.tenant_id))
    problems = _problems(norm.issues)
    if problems:
        raise ApiError(
            422, "VALIDATION_FAILED", f"{len(problems)} problem(s) must be fixed; nothing was saved.", problems
        )
    values = of.revision_values(norm.fields)
    customer_id = customers.link(conn, row.tenant_id, values)
    number = _append_revision(
        conn,
        principal,
        row,
        values,
        new_extra if extra_changed else (current.extra or []),
        _attention(norm),
        {"changed": changed + (["extra"] if extra_changed else []), "previous_revision": row.current_number},
        reason,
        customer_id,
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="ORDER_REVISED",
        object_type="customer_order",
        object_id=row.id,
        object_revision=number,
        reason=reason,
        after={"fields": changed, "extra": extra_changed},
    )
    return get_order(conn, principal, row.id)


def render_revision(conn: Connection, row: Any, number: int) -> tuple[bytes, str]:
    """PDF bytes of one saved revision, always rendered from the stored values (reproducible)."""
    rev = conn.execute(select(ov).where(ov.c.order_id == row.id, ov.c.number == number)).one_or_none()
    if rev is None:
        raise not_found()
    from app.owner_reports.settings import company_name

    dept = conn.execute(select(t.department.c.name).where(t.department.c.id == row.department_id)).scalar_one()
    data = order_pdf.render({"order_ref": row.order_ref, "department": dept}, rev, company_name(conn, row.tenant_id))
    return data, order_pdf.file_name(row.order_ref, number)


def render_pdf(
    conn: Connection, principal: Principal, order_id: uuid.UUID, number: int | None = None
) -> tuple[bytes, str, int]:
    """The PDF of one saved revision (default: the latest)."""
    row = _load_order(conn, principal, order_id)
    number = number or row.current_number
    data, name = render_revision(conn, row, number)
    return data, name, number


def summary(conn: Connection, principal: Principal, since: datetime) -> dict[str, Any]:
    """Dashboard figures: orders waiting for review, orders saved since a date and their value, emails sent."""
    scope = _scope(principal)
    waiting = conn.execute(
        select(func.count())
        .select_from(od)
        .where(od.c.state == "NEEDS_REVIEW", od.c.department_id.in_(list(principal.department_ids)))
    ).scalar_one()
    saved = conn.execute(
        select(func.count(), func.coalesce(func.sum(ov.c.total), 0), func.count(func.distinct(o.c.customer_id)))
        .select_from(o)
        .join(ov, ov.c.id == o.c.current_revision_id)
        .where(scope, o.c.state == "ACTIVE", o.c.created_at >= since)
    ).one()
    sent = conn.execute(
        select(func.count())
        .select_from(oe)
        .join(o, o.c.id == oe.c.order_id)
        .where(scope, oe.c.state == "ACCEPTED", oe.c.created_at >= since)
    ).scalar_one()
    return {
        "awaiting_review": waiting,
        "saved": saved[0],
        "saved_total": _fmt(Decimal(saved[1])),
        "customers": saved[2],
        "emails_accepted": sent,
        "since": since.isoformat(),
    }


# --- email (EmailJS REST API, sent by the worker) --------------------------------------------


def check_attachment(data: bytes, name: str) -> None:
    """The attachment must be a non-empty PDF with a PDF file name; anything else is never sent."""
    if not data or not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:] or not name.lower().endswith(".pdf"):
        raise ApiError(422, "ATTACHMENT_INVALID", "The order PDF could not be produced correctly, so nothing was sent.")


def start_email(
    conn: Connection, principal: Principal, order_id: uuid.UUID, to_email: str, revision: int
) -> dict[str, Any]:
    """Queue one send of the order's latest PDF to one address. The worker sends it and records the outcome."""
    from app.owner_reports import settings as owner_settings

    if not principal.has_any(*SEND_ROLES):
        raise forbidden("Only Reviewers and Senders can email orders.")
    address = to_email.strip()
    if not _EMAIL.match(address):
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            "Enter one valid email address.",
            [Issue("INVALID_EMAIL", f'"{address}" is not a valid email address.', "to_email")],
        )
    row = _load_order(conn, principal, order_id, lock=True)
    if row.current_number != revision:
        raise conflict(
            "ORDER_CHANGED",
            f"This order was changed; the latest PDF is revision {row.current_number}. Check it and send again.",
        )
    config_row = owner_settings.load(conn, row.tenant_id)
    if owner_settings.missing(config_row):
        raise conflict(
            "EMAIL_NOT_CONFIGURED",
            "Email is not set up yet: EMAILJS_SERVICE_ID, EMAILJS_TEMPLATE_ID, EMAILJS_PUBLIC_KEY and "
            "EMAILJS_PRIVATE_KEY must be set in the server .env (or in Settings -> Owner report & email).",
        )
    busy = conn.execute(
        select(oe.c.id).where(
            oe.c.order_id == row.id, func.lower(oe.c.to_email) == address.lower(), oe.c.state.in_(("QUEUED", "SENDING"))
        )
    ).first()
    if busy:
        raise conflict("ALREADY_SENDING", f"This PDF is already being sent to {address}.")
    data, name = render_revision(conn, row, row.current_number)
    check_attachment(data, name)
    limit_kb = owner_settings.max_request_kb(config_row)
    if len(base64.b64encode(data)) + 4000 > limit_kb * 1000:
        raise ApiError(
            422,
            "ATTACHMENT_TOO_LARGE",
            f"The PDF ({len(data) // 1000} KB) is too large for the EmailJS request limit ({limit_kb} KB).",
        )
    email_id = uuid.uuid4()
    conn.execute(
        insert(oe).values(
            id=email_id,
            tenant_id=row.tenant_id,
            order_id=row.id,
            revision_number=row.current_number,
            to_email=address,
            channel="emailjs_api",
            state="QUEUED",
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
        action="ORDER_EMAIL_QUEUED",
        object_type="customer_order",
        object_id=row.id,
        object_revision=row.current_number,
        after={"email_id": str(email_id)},
    )
    return email_view(conn.execute(select(oe).where(oe.c.id == email_id)).one())


def email_params(conn: Connection, e: Any) -> tuple[dict[str, str], bytes]:
    """EmailJS variables for one queued order email (shared document template) and the exact PDF recorded."""
    from app.owner_reports.settings import company_name

    row = conn.execute(select(o).where(o.c.id == e.order_id)).one()
    data, name = render_revision(conn, row, e.revision_number)
    check_attachment(data, name)
    if hashlib.sha256(data).hexdigest() != e.attachment_sha256:
        raise RuntimeError("the order PDF no longer matches the one recorded for this email")
    rev = conn.execute(select(ov).where(ov.c.order_id == row.id, ov.c.number == e.revision_number)).one()
    sender = conn.execute(select(t.membership.c.email).where(t.membership.c.id == e.created_by)).scalar_one_or_none()
    company = company_name(conn, row.tenant_id)
    subject = f"Your Order/Report - {row.order_ref}"
    table = template_vars.variables(
        [template_vars.row(row.order_ref, rev)],
        title=subject,
        reference=row.order_ref,
        to_email=e.to_email,
        company=company,
    )
    message = (
        f"Hello,\n\nPlease find attached your order/report PDF.\n\nRecord: {row.order_ref}\n"
        f"Customer: {rev.customer_name}\n\n{table['orders_text']}\n\nThank you.\n{company}"
    )
    params = table | {
        "to_email": e.to_email,
        "subject": subject,
        "message": message,
        "record_reference": row.order_ref,
        "customer_name": rev.customer_name,
        "company_name": company,
        "from_name": company,
        "reply_to": sender or "",
        "attachment_name": name,
        "pdf_file": f"data:application/pdf;base64,{base64.b64encode(data).decode()}",
        "email_reference": str(e.id),
    }
    return params, data


def sweep_stale(conn: Connection, now: datetime) -> int:
    """Browser sends (earlier channel) whose browser never reported back become UNKNOWN; never resent."""
    ids = (
        conn.execute(
            select(oe.c.id).where(
                oe.c.channel == "emailjs",
                oe.c.state == "SENDING",
                oe.c.created_at < now - timedelta(minutes=STALE_MINUTES),
            )
        )
        .scalars()
        .all()
    )
    if ids:
        conn.execute(
            update(oe)
            .where(oe.c.id.in_(ids))
            .values(
                state="UNKNOWN",
                error_code="EMAILJS_NO_REPORT",
                finished_at=func.now(),
                error_message="The browser did not report whether EmailJS accepted it. Check the EmailJS history.",
            )
        )
    return len(ids)


def reconcile_email(
    conn: Connection, principal: Principal, order_id: uuid.UUID, email_id: uuid.UUID, outcome: str, note: str
) -> dict[str, Any]:
    """An UNKNOWN send, checked by a person in EmailJS -> Email History: record what actually happened."""
    if not principal.has_any(*SEND_ROLES):
        raise forbidden()
    row = _load_order(conn, principal, order_id)
    e = conn.execute(select(oe).where(oe.c.id == email_id, oe.c.order_id == row.id).with_for_update()).one_or_none()
    if e is None:
        raise not_found()
    if e.state != "UNKNOWN":
        raise conflict("NOT_UNKNOWN", "Only a send with an unknown result can be reconciled.")
    conn.execute(
        update(oe)
        .where(oe.c.id == e.id)
        .values(state=outcome, error_code="RECONCILED", error_message=f"Checked by a person: {note}"[:300])
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action=f"ORDER_EMAIL_RECONCILED_{outcome}",
        object_type="customer_order",
        object_id=row.id,
        reason=note,
        after={"email_id": str(e.id)},
    )
    return email_view(conn.execute(select(oe).where(oe.c.id == e.id)).one())
