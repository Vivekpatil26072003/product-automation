"""Batch reports for the company owner: one consolidated PDF of what was approved from one upload batch, emailed
to the configured owner address by the worker (EmailJS REST API).

Automatic flow (Settings -> Owner report & email, "Email the owner automatically"):
  every entry of the batch decided (no order form or production entry waiting) and nothing still being read
  -> a report is created (its order and record list fixed now) -> batch_report.render makes the PDF from the
  saved database values -> batch_report.email sends it to the owner -> the delivery status is recorded and the
  uploader, reviewers and administrators get a notification.
The same report can be created and sent by hand from the batch screen. A report is never marked sent unless
EmailJS accepted it; a failure keeps the PDF and can be retried; at most one send of a report is in flight, and
sending an already-sent report again needs an explicit "send again".
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import conflict, forbidden, not_found
from app.db import tables as t
from app.domain.enums import Role
from app.ingestion import service as ingestion
from app.jobs import ledger
from app.mail import template_vars
from app.orders import fields as of
from app.owner_reports import settings as owner_settings

br, rd = t.batch_report, t.report_delivery
RENDER_KIND, EMAIL_KIND = "batch_report.render", "batch_report.email"
RUNNING = ("QUEUED", "RUNNING", "RETRY_WAIT")
SERVICE = audit.Actor("service", None)


# --- access ----------------------------------------------------------------------------------


def load_batch(conn: Connection, principal: Principal, batch_id: uuid.UUID) -> Any:
    """Uploader of the batch and reviewers (as for the batch itself); senders and administrators may read."""
    row = conn.execute(select(t.batch).where(t.batch.c.id == batch_id)).one_or_none()
    if row is None or not principal.can_access_department(row.department_id):
        raise not_found()
    if not (ingestion.can_access_batch(principal, row) or principal.has_any(Role.SENDER, Role.ADMIN)):
        raise not_found()
    return row


def _load_report(conn: Connection, principal: Principal, report_id: uuid.UUID, lock: bool = False) -> tuple:
    q = select(br).where(br.c.id == report_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None:
        raise not_found()
    return row, load_batch(conn, principal, row.batch_id)


def _can_send(principal: Principal) -> bool:
    return principal.has_any(Role.REVIEWER, Role.SENDER, Role.ADMIN)


# --- batch state -----------------------------------------------------------------------------


def _approved(conn: Connection, batch_id: uuid.UUID) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Orders and production records approved from this batch (distinct, stable order)."""
    order_ids = (
        conn.execute(
            select(t.order_draft.c.order_id)
            .where(t.order_draft.c.batch_id == batch_id, t.order_draft.c.state == "APPROVED")
            .group_by(t.order_draft.c.order_id)
            .order_by(func.min(t.order_draft.c.decided_at))
        )
        .scalars()
        .all()
    )
    record_ids = (
        conn.execute(
            select(t.candidate.c.record_id)
            .where(
                t.candidate.c.batch_id == batch_id,
                t.candidate.c.state == "APPROVED",
                t.candidate.c.record_id.is_not(None),
            )
            .order_by(t.candidate.c.decided_at)
        )
        .scalars()
        .all()
    )
    return list(order_ids), list(record_ids)


def _counts(conn: Connection, batch_id: uuid.UUID) -> dict[str, int]:
    drafts = dict(
        conn.execute(
            select(t.order_draft.c.state, func.count())
            .where(t.order_draft.c.batch_id == batch_id)
            .group_by(t.order_draft.c.state)
        ).all()
    )
    cands = dict(
        conn.execute(
            select(t.candidate.c.state, func.count())
            .where(t.candidate.c.batch_id == batch_id)
            .group_by(t.candidate.c.state)
        ).all()
    )
    jobs = conn.execute(
        select(func.count())
        .select_from(t.job)
        .join(t.upload, t.upload.c.id == t.job.c.object_id)
        .where(t.upload.c.batch_id == batch_id, t.job.c.kind.like("upload.%"), t.job.c.state.in_(RUNNING))
    ).scalar_one()
    return {
        "orders_waiting": drafts.get("NEEDS_REVIEW", 0),
        "orders_approved": drafts.get("APPROVED", 0),
        "orders_rejected": drafts.get("REJECTED", 0),
        "entries_waiting": cands.get("NEEDS_REVIEW", 0),
        "entries_approved": cands.get("APPROVED", 0),
        "entries_rejected": cands.get("REJECTED", 0),
        "jobs_running": jobs,
        **_sheet_counts(conn, batch_id),
    }


def _sheet_counts(conn: Connection, batch_id: uuid.UUID) -> dict[str, int]:
    """Daily production sheets fed by this batch (one per department and day)."""
    sr, ss = t.shift_report, t.shift_report_source
    states = dict(
        conn.execute(
            select(sr.c.state, func.count(func.distinct(sr.c.id)))
            .join(ss, ss.c.report_id == sr.c.id)
            .where(ss.c.batch_id == batch_id)
            .group_by(sr.c.state)
        ).all()
    )
    from app.pick_registers.service import batch_counts

    for state, n in batch_counts(conn, batch_id).items():  # pick registers count as sheets of the batch
        states[state] = states.get(state, 0) + n
    return {"sheets_waiting": states.get("DRAFT", 0), "sheets_approved": states.get("APPROVED", 0)}


def _latest(conn: Connection, batch_id: uuid.UUID) -> Any:
    return conn.execute(select(br).where(br.c.batch_id == batch_id).order_by(br.c.version.desc()).limit(1)).first()


def after_decision(conn: Connection, principal: Principal | None, batch_id: uuid.UUID) -> uuid.UUID | None:
    """Called in the transaction of every approve / reject. Creates the automatic report once the batch is done."""
    batch = conn.execute(select(t.batch).where(t.batch.c.id == batch_id)).one()
    cfg = owner_settings.load(conn, batch.tenant_id)
    if cfg is None or not cfg.auto_send or not cfg.owner_email:
        return None
    c = _counts(conn, batch_id)
    if c["orders_waiting"] or c["entries_waiting"] or c["jobs_running"]:
        return None
    order_ids, record_ids = _approved(conn, batch_id)
    if not order_ids and not record_ids:
        return None
    latest = _latest(conn, batch_id)
    if (
        latest is not None
        and latest.state != "FAILED"
        and (sorted(latest.order_ids) == sorted(order_ids) and sorted(latest.record_ids) == sorted(record_ids))
    ):
        return None  # this content was already reported
    return _create(conn, batch, principal, "AUTO", True, order_ids, record_ids)


def _create(
    conn: Connection,
    batch: Any,
    principal: Principal | None,
    trigger: str,
    email_owner: bool,
    order_ids: list[uuid.UUID],
    record_ids: list[uuid.UUID],
) -> uuid.UUID:
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"batch_report:{batch.id}", 0))))
    version = (conn.execute(select(func.max(br.c.version)).where(br.c.batch_id == batch.id)).scalar_one() or 0) + 1
    report_id = uuid.uuid4()
    conn.execute(
        insert(br).values(
            id=report_id,
            tenant_id=batch.tenant_id,
            batch_id=batch.id,
            version=version,
            trigger=trigger,
            email_owner=email_owner,
            order_ids=order_ids,
            record_ids=record_ids,
            created_by=principal.membership_id if principal else None,
        )
    )
    ledger.create_job(
        conn,
        tenant_id=batch.tenant_id,
        kind=RENDER_KIND,
        object_id=report_id,
        created_by=principal.membership_id if principal else None,
    )
    audit.record(
        conn,
        tenant_id=batch.tenant_id,
        actor=principal.actor if principal else SERVICE,
        action="BATCH_REPORT_REQUESTED",
        object_type="batch",
        object_id=batch.id,
        after={
            "report_id": str(report_id),
            "version": version,
            "trigger": trigger,
            "orders": len(order_ids),
            "records": len(record_ids),
            "email_owner": email_owner,
        },
    )
    return report_id


def create_manual(conn: Connection, principal: Principal, batch_id: uuid.UUID, email_owner: bool) -> dict[str, Any]:
    if not _can_send(principal):
        raise forbidden("Reviewers, Senders and administrators create reports.")
    batch = load_batch(conn, principal, batch_id)
    order_ids, record_ids = _approved(conn, batch.id)
    if not order_ids and not record_ids:
        raise conflict(
            "NOTHING_APPROVED", "Nothing from this batch has been approved yet, so there is nothing to report."
        )
    if email_owner:
        _require_email(conn, batch.tenant_id)
    report_id = _create(conn, batch, principal, "MANUAL", email_owner, order_ids, record_ids)
    return report_view(conn, conn.execute(select(br).where(br.c.id == report_id)).one())


def _require_email(conn: Connection, tenant_id: uuid.UUID) -> Any:
    cfg = owner_settings.load(conn, tenant_id)
    if cfg is None or not cfg.owner_email:
        raise conflict(
            "OWNER_EMAIL_MISSING",
            "No owner email is set. An administrator adds it in Settings -> Owner report & email.",
        )
    if owner_settings.missing(cfg):
        raise conflict(
            "EMAIL_NOT_CONFIGURED",
            "Email is not set up yet. An administrator completes Settings -> "
            "Owner report & email (EmailJS service, template, public and private key).",
        )
    return cfg


# --- deliveries ------------------------------------------------------------------------------


def queue_delivery(
    conn: Connection, report: Any, to_email: str, trigger: str, created_by: uuid.UUID | None
) -> uuid.UUID:
    """One email of a READY report. The unique in-flight index makes a second concurrent request fail."""
    delivery_id = uuid.uuid4()
    inserted = conn.execute(
        pg_insert(rd)
        .values(
            id=delivery_id,
            tenant_id=report.tenant_id,
            report_id=report.id,
            to_email=to_email,
            trigger=trigger,
            attachment_sha256=report.sha256,
            created_by=created_by,
        )
        .on_conflict_do_nothing()
        .returning(rd.c.id)
    ).scalar_one_or_none()
    if inserted is None:
        raise conflict("ALREADY_SENDING", "This report is already being emailed.")
    ledger.create_job(conn, tenant_id=report.tenant_id, kind=EMAIL_KIND, object_id=delivery_id, created_by=created_by)
    return delivery_id


def request_delivery(conn: Connection, principal: Principal, report_id: uuid.UUID, resend: bool) -> dict[str, Any]:
    """Manual "Email to owner" / retry / send again."""
    if not _can_send(principal):
        raise forbidden("Reviewers, Senders and administrators email reports.")
    report, _ = _load_report(conn, principal, report_id, lock=True)
    cfg = _require_email(conn, report.tenant_id)
    if report.state != "READY":
        raise conflict(
            "REPORT_NOT_READY",
            "The PDF is not ready yet."
            if report.state != "FAILED"
            else "The PDF could not be created. Create the report again.",
        )
    previous = conn.execute(select(rd.c.state).where(rd.c.report_id == report.id)).scalars().all()
    if any(s in ("QUEUED", "SENDING") for s in previous):
        raise conflict("ALREADY_SENDING", "This report is already being emailed.")
    if not resend and any(s in ("ACCEPTED", "UNKNOWN") for s in previous):
        raise conflict(
            "ALREADY_SENT",
            "This report was already emailed (or may have been). Choose 'Send again' to email it once more.",
        )
    trigger = "RESEND" if any(s in ("ACCEPTED", "UNKNOWN") for s in previous) else "MANUAL"
    delivery_id = queue_delivery(conn, report, cfg.owner_email, trigger, principal.membership_id)
    audit.record(
        conn,
        tenant_id=report.tenant_id,
        actor=principal.actor,
        action="BATCH_REPORT_EMAIL_REQUESTED",
        object_type="batch_report",
        object_id=report.id,
        after={"delivery_id": str(delivery_id), "trigger": trigger},
    )
    return report_view(conn, report)


def reconcile_delivery(
    conn: Connection, principal: Principal, delivery_id: uuid.UUID, outcome: str, note: str
) -> dict[str, Any]:
    if not _can_send(principal):
        raise forbidden()
    d = conn.execute(select(rd).where(rd.c.id == delivery_id).with_for_update()).one_or_none()
    if d is None:
        raise not_found()
    report, _ = _load_report(conn, principal, d.report_id)
    if d.state != "UNKNOWN":
        raise conflict("NOT_UNKNOWN", "Only an email with an unknown result can be reconciled.")
    conn.execute(update(rd).where(rd.c.id == d.id).values(state=outcome, reconcile_note=note[:300]))
    audit.record(
        conn,
        tenant_id=d.tenant_id,
        actor=principal.actor,
        action=f"BATCH_REPORT_EMAIL_RECONCILED_{outcome}",
        object_type="batch_report",
        object_id=d.report_id,
        reason=note,
    )
    return report_view(conn, report)


# --- views -----------------------------------------------------------------------------------


def delivery_view(d: Any) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "to_email": d.to_email,
        "trigger": d.trigger,
        "state": d.state,
        "http_status": d.http_status,
        "error": {"code": d.error_code, "message": d.error_message} if d.error_code else None,
        "reconcile_note": d.reconcile_note,
        "at": d.created_at.isoformat(),
        "finished_at": d.finished_at.isoformat() if d.finished_at else None,
    }


def report_view(conn: Connection, r: Any) -> dict[str, Any]:
    deliveries = conn.execute(select(rd).where(rd.c.report_id == r.id).order_by(rd.c.created_at.desc())).all()
    return {
        "id": str(r.id),
        "batch_id": str(r.batch_id),
        "version": r.version,
        "trigger": r.trigger,
        "email_owner": r.email_owner,
        "state": r.state,
        "orders": len(r.order_ids),
        "records": len(r.record_ids),
        "summary": r.summary,
        "file_name": r.file_name,
        "bytes": r.bytes,
        "sha256": r.sha256,
        "error": {"code": r.error_code, "message": r.error_message} if r.error_code else None,
        "created_at": r.created_at.isoformat(),
        "ready_at": r.ready_at.isoformat() if r.ready_at else None,
        "deliveries": [delivery_view(d) for d in deliveries],
    }


def list_reports(conn: Connection, principal: Principal, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    load_batch(conn, principal, batch_id)
    rows = conn.execute(select(br).where(br.c.batch_id == batch_id).order_by(br.c.version.desc())).all()
    return [report_view(conn, r) for r in rows]


def report_file(conn: Connection, principal: Principal, report_id: uuid.UUID) -> Any:
    report, _ = _load_report(conn, principal, report_id)
    if report.state != "READY":
        raise conflict("REPORT_NOT_READY", "The PDF is not ready.")
    return report


def pipeline(conn: Connection, principal: Principal, batch_id: uuid.UUID) -> dict[str, Any]:
    """Uploaded -> Reading -> Review -> Approved and saved -> PDF report -> Emailed to owner, with real states."""
    batch = load_batch(conn, principal, batch_id)
    ups = conn.execute(
        select(t.upload.c.id, t.upload.c.state, t.upload.c.display_name).where(t.upload.c.batch_id == batch.id)
    ).all()
    jobs = conn.execute(
        select(t.job.c.kind, t.job.c.state, t.job.c.error_code, t.job.c.error_message, t.job.c.object_id)
        .join(t.upload, t.upload.c.id == t.job.c.object_id)
        .where(t.upload.c.batch_id == batch.id, t.job.c.kind.like("upload.%"))
        .order_by(t.job.c.created_at)
    ).all()
    latest_job = {}
    for j in jobs:
        latest_job[(j.object_id, j.kind)] = j
    c = _counts(conn, batch.id)
    report = _latest(conn, batch.id)
    delivery = (
        conn.execute(select(rd).where(rd.c.report_id == report.id).order_by(rd.c.created_at.desc()).limit(1)).first()
        if report
        else None
    )
    cfg = owner_settings.load(conn, batch.tenant_id)

    def stage(key: str, label: str, state: str, detail: str = "") -> dict[str, str]:
        return {"key": key, "label": label, "state": state, "detail": detail}

    waiting_upload = [u for u in ups if u.state in ("UPLOADING", "QUARANTINED")]
    rejected = [u for u in ups if u.state == "REJECTED"]
    stages = [
        stage(
            "uploaded",
            "Uploaded",
            "current" if waiting_upload else "failed" if len(rejected) == len(ups) else "done",
            f"{len(ups)} file(s)" + (f", {len(rejected)} not accepted" if rejected else ""),
        )
    ]
    failed = [j for j in latest_job.values() if j.state in ("FAILED", "PARTIAL")]
    if c["jobs_running"] or waiting_upload:
        stages.append(stage("reading", "Reading", "current", "Reading the pages…"))
    elif failed:
        msg = failed[0].error_message or failed[0].error_code or "A page could not be read."
        stages.append(stage("reading", "Reading", "failed", msg))
    else:
        stages.append(stage("reading", "Reading", "done" if ups and not rejected else "waiting"))
    waiting = c["orders_waiting"] + c["entries_waiting"] + c["sheets_waiting"]
    approved = c["orders_approved"] + c["entries_approved"] + c["sheets_approved"]
    decided = approved + c["orders_rejected"] + c["entries_rejected"]
    if waiting:
        stages.append(stage("review", "Review", "current", f"{waiting} waiting for review"))
    elif failed and not decided:
        stages.append(stage("review", "Review", "current", "Enter what could not be read"))
    else:
        stages.append(stage("review", "Review", "done" if decided else "waiting"))
    stages.append(
        stage(
            "saved",
            "Approved and saved",
            "done" if approved and not waiting else "current" if approved else "waiting",
            ", ".join(
                x
                for x in (
                    f"{c['orders_approved']} order(s)" if c["orders_approved"] else "",
                    f"{c['entries_approved']} production entr{'y' if c['entries_approved'] == 1 else 'ies'}"
                    if c["entries_approved"]
                    else "",
                    f"{c['sheets_approved']} daily sheet(s)" if c["sheets_approved"] else "",
                )
                if x
            )
            if approved
            else "",
        )
    )
    if report is None:
        stages.append(stage("report", "PDF report", "waiting"))
    else:
        rs = {"READY": "done", "FAILED": "failed"}.get(report.state, "current")
        stages.append(stage("report", "PDF report", rs, report.error_message or f"Version {report.version}"))
    if delivery is not None:
        ds = {"ACCEPTED": "done", "FAILED": "failed", "UNKNOWN": "failed"}.get(delivery.state, "current")
        detail = {"ACCEPTED": f"Sent to {delivery.to_email}", "UNKNOWN": "Result unknown: check EmailJS history"}.get(
            delivery.state, delivery.error_message or "Sending…"
        )
        stages.append(stage("email", "Emailed to owner", ds, detail))
    elif cfg is None or not cfg.auto_send:
        stages.append(stage("email", "Emailed to owner", "skipped", "Automatic owner email is off"))
    else:
        stages.append(stage("email", "Emailed to owner", "waiting"))
    return {"batch_id": str(batch.id), "stages": stages, "counts": c, "report_id": str(report.id) if report else None}


# --- report content (worker) -----------------------------------------------------------------


def content(conn: Connection, report: Any) -> dict[str, Any]:
    """Everything the PDF shows, read from the saved database rows (current revisions at render time)."""
    tenant = conn.execute(select(t.tenant).where(t.tenant.c.id == report.tenant_id)).one()
    tz = ZoneInfo(tenant.timezone)
    batch = conn.execute(select(t.batch).where(t.batch.c.id == report.batch_id)).one()
    owner = conn.execute(select(t.membership.c.display_name).where(t.membership.c.id == batch.owner_id)).scalar()
    dept = conn.execute(select(t.department.c.name).where(t.department.c.id == batch.department_id)).scalar_one()
    files = conn.execute(
        select(func.count(), func.coalesce(func.sum(t.upload.c.page_count), 0)).where(
            t.upload.c.batch_id == batch.id, t.upload.c.state == "READY"
        )
    ).one()
    o, ov, cu = t.customer_order, t.order_revision, t.customer
    orders = conn.execute(
        select(o.c.id.label("order_key"), o.c.order_ref, o.c.customer_id, ov, cu.c.name.label("customer"))
        .join(ov, ov.c.id == o.c.current_revision_id)
        .outerjoin(cu, cu.c.id == o.c.customer_id)
        .where(o.c.id.in_(report.order_ids))
        .order_by(func.lower(func.coalesce(cu.c.name, ov.c.customer_name)), ov.c.order_date, o.c.order_ref)
    ).all()
    groups: dict[str, dict[str, Any]] = {}
    total, advance, remaining, missing_total = Decimal(0), Decimal(0), Decimal(0), 0
    attention = []
    for r in orders:
        key = str(r.customer_id or r.customer_name)
        g = groups.setdefault(
            key,
            {
                "name": r.customer or r.customer_name,
                "mobile": r.mobile,
                "orders": [],
                "total": Decimal(0),
                "has_total": False,
            },
        )
        g["orders"].append(
            {
                "ref": r.order_ref,
                "revision": r.number,
                "order_date": r.order_date,
                "delivery_date": r.delivery_date,
                "package": " ".join(x for x in (r.package, r.size, r.material) if x),
                "quantity": r.quantity,
                "rate": r.rate,
                "total": r.total,
                "status": " / ".join(
                    x for x in (r.production_status, r.payment_status, r.delivery_status, r.priority) if x
                ),
                "extra": r.extra or [],
            }
        )
        if r.total is not None:
            g["total"] += r.total
            g["has_total"] = True
            total += r.total
        else:
            missing_total += 1
        advance += r.advance or 0
        remaining += r.remaining or 0
        for a in r.attention or []:
            attention.append({"ref": r.order_ref, "customer": r.customer or r.customer_name, "text": a["message"]})
    rec, rev = t.production_record, t.record_revision
    records = conn.execute(
        select(
            rev.c.production_date,
            rev.c.production_qty,
            rev.c.target_qty,
            rev.c.unit,
            rev.c.status,
            rev.c.operator_name,
            t.department.c.name.label("department"),
            t.machine.c.code.label("machine"),
        )
        .select_from(rec)
        .join(rev, rev.c.id == rec.c.current_revision_id)
        .join(t.department, t.department.c.id == rev.c.department_id)
        .join(t.machine, t.machine.c.id == rev.c.machine_id)
        .where(rec.c.id.in_(report.record_ids))
        .order_by(rev.c.production_date, t.department.c.name)
    ).all()
    drafts = _counts(conn, batch.id)
    cfg = owner_settings.load(conn, report.tenant_id)
    return {
        "company": owner_settings.company_name(conn, report.tenant_id, cfg),
        "generated_at": datetime.now(tz),
        "timezone": tenant.timezone,
        "batch_ref": f"B-{str(batch.id)[:8].upper()}",
        "version": report.version,
        "department": dept,
        "uploaded_by": owner,
        "uploaded_at": batch.created_at.astimezone(tz),
        "files": files[0],
        "pages": int(files[1]),
        "counts": drafts,
        "customers": list(groups.values()),
        "order_count": len(orders),
        "customer_count": len(groups),
        "totals": {
            "total": total if orders and missing_total < len(orders) else None,
            "advance": advance,
            "remaining": remaining,
            "orders_without_total": missing_total,
        },
        "attention": attention,
        "records": [dict(r._mapping) for r in records],
        "revision_ids": [r.id for r in orders],
        "missing_labels": of.LABEL,
    }


def summary_json(c: dict[str, Any]) -> dict[str, Any]:
    total = c["totals"]["total"]
    return {
        "orders": c["order_count"],
        "customers": c["customer_count"],
        "records": len(c["records"]),
        "total": format(total, "f") if total is not None else None,
        "orders_without_total": c["totals"]["orders_without_total"],
        "attention": len(c["attention"]),
        "pages": c["pages"],
    }


def email_params(conn: Connection, report: Any, delivery: Any, data: bytes) -> dict[str, str]:
    import base64

    s = report.summary or {}
    company = owner_settings.company_name(conn, report.tenant_id)
    batch_ref = f"B-{str(report.batch_id)[:8].upper()}"
    lines = [
        "Hello,",
        "",
        f"The diary pages uploaded in batch {batch_ref} have been reviewed and saved.",
        f"Orders: {s.get('orders', 0)} from {s.get('customers', 0)} customer(s); production entries: "
        f"{s.get('records', 0)}.",
    ]
    if s.get("total") is not None:
        lines.append(
            f"Total order value: {Decimal(s['total']):,.2f}"
            + (f" ({s['orders_without_total']} order(s) without a total)" if s.get("orders_without_total") else "")
        )
    if s.get("attention"):
        lines.append(f"{s['attention']} item(s) need attention; see the last section of the PDF.")
    rows = report_rows(conn, report)
    subject = f"Diary report {batch_ref} - {s.get('orders', 0)} order(s) - {company}"
    table = template_vars.variables(
        rows,
        title=subject,
        reference=f"{batch_ref} v{report.version}",
        to_email=delivery.to_email,
        company=company,
        totals=Decimal(s["total"]) if s.get("total") is not None else None,
    )
    if rows:
        lines += ["", "Orders:", table["orders_text"]]
    lines += ["", "The full report is attached.", "", company]
    return table | {
        "to_email": delivery.to_email,
        "subject": subject,
        "message": "\n".join(lines),
        "record_reference": f"{batch_ref} v{report.version}",
        "company_name": company,
        "from_name": company,
        "reply_to": "",
        "attachment_name": report.file_name,
        "pdf_file": f"data:application/pdf;base64,{base64.b64encode(data).decode()}",
        "email_reference": str(delivery.id),
    }


def report_rows(conn: Connection, report: Any) -> list[dict[str, str]]:
    """The exact order revisions the report's PDF was made from, as table rows."""
    o, ov = t.customer_order, t.order_revision
    revs = conn.execute(
        select(o.c.order_ref, ov)
        .join(ov, ov.c.order_id == o.c.id)
        .where(ov.c.id.in_(report.revision_ids))
        .order_by(func.lower(ov.c.customer_name), ov.c.order_date, o.c.order_ref)
    ).all()
    return [template_vars.row(r.order_ref, r) for r in revs]


def notify(conn: Connection, report: Any, title: str, body: str, key: str) -> None:
    """In-app notification for the uploader of the batch, its department's reviewers and administrators."""
    batch = conn.execute(select(t.batch).where(t.batch.c.id == report.batch_id)).one()
    m, md = t.membership, t.membership_department
    reviewers = (
        conn.execute(
            select(m.c.id)
            .join(md, and_(md.c.membership_id == m.c.id, md.c.department_id == batch.department_id))
            .where(m.c.active, m.c.roles.contains(["REVIEWER"]))
        )
        .scalars()
        .all()
    )
    admins = conn.execute(select(m.c.id).where(m.c.active, m.c.roles.contains(["ADMIN"]))).scalars().all()
    for member_id in {batch.owner_id, *reviewers, *admins}:
        conn.execute(
            pg_insert(t.notification)
            .values(
                id=uuid.uuid4(),
                tenant_id=report.tenant_id,
                recipient_id=member_id,
                kind="REPORT",
                dedupe_key=f"report:{key}:{member_id}",
                title=title[:200],
                body=body[:1000],
                link=f"/batches/{report.batch_id}",
                department_id=batch.department_id,
            )
            .on_conflict_do_nothing()
        )


# --- diary data preview (table + PDF, right after reading) ------------------------------------


def diary_data(conn: Connection, principal: Principal, batch_id: uuid.UUID) -> dict[str, Any]:
    """Every order read from a batch as one table: saved orders with their latest saved values, orders still
    waiting for review with the values as read (marked, never presented as checked)."""
    from types import SimpleNamespace

    batch = load_batch(conn, principal, batch_id)
    d, o, ov = t.order_draft, t.customer_order, t.order_revision
    drafts = conn.execute(
        select(d, t.upload.c.display_name)
        .join(t.upload, t.upload.c.id == d.c.upload_id)
        .where(d.c.batch_id == batch.id, d.c.state.in_(("NEEDS_REVIEW", "APPROVED")))
        .order_by(d.c.page_no, func.coalesce(d.c.reading["position"].as_integer(), 0), d.c.created_at)
    ).all()
    rows = []
    for r in drafts:
        if r.state == "APPROVED" and r.order_id:
            saved = conn.execute(
                select(o.c.order_ref, ov).join(ov, ov.c.id == o.c.current_revision_id).where(o.c.id == r.order_id)
            ).one()
            data, ref, status = template_vars.row(saved.order_ref, saved), saved.order_ref, "Saved"
        else:
            values = of.revision_values(r.fields)
            rev = SimpleNamespace(**values, extra=r.extra or [])
            data, ref = template_vars.row(f"p{r.page_no}", rev), None
            errors = sum(1 for i in r.issues if i["severity"] == "error")
            status = f"To review ({errors} to check)" if errors else "To review"
        rows.append(
            data
            | {
                "status": status,
                "draft_id": str(r.id),
                "order_id": str(r.order_id) if r.order_id else None,
                "order_ref": ref,
                "file": r.display_name,
                "page": r.page_no,
            }
        )
    return {
        "batch_id": str(batch.id),
        "batch_ref": f"B-{str(batch.id)[:8].upper()}",
        "rows": rows,
        "reviewed": all(r["status"] == "Saved" for r in rows) if rows else False,
    }


def diary_pdf(conn: Connection, principal: Principal, batch_id: uuid.UUID) -> tuple[bytes, str]:
    from app.owner_reports.pdf import render_preview

    data = diary_data(conn, principal, batch_id)
    if not data["rows"]:
        raise conflict("NO_DIARY_DATA", "No orders have been read from this batch yet.")
    batch = conn.execute(select(t.batch).where(t.batch.c.id == batch_id)).one()
    tenant = conn.execute(select(t.tenant).where(t.tenant.c.id == batch.tenant_id)).one()
    pdf = render_preview(
        {
            "company": owner_settings.company_name(conn, batch.tenant_id),
            "batch_ref": data["batch_ref"],
            "generated_at": datetime.now(ZoneInfo(tenant.timezone)),
            "timezone": tenant.timezone,
            "rows": data["rows"],
            "reviewed": data["reviewed"],
        }
    )
    return pdf, f"Diary_Data_{data['batch_ref']}.pdf"
