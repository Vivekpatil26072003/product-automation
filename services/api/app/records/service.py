"""Approved production records: read, correct through revisions, archive (FR10; API operations 17-20).

Approved revisions never change (database trigger). A correction is a new PENDING revision; approving
it advances current_revision_id atomically, bumps the company data_version and queues a change event.
Archiving excludes a record from future aggregates without deleting any history.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, func, insert, select, update

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, Issue, conflict, forbidden, not_found, precondition_failed
from app.db import tables as t
from app.domain.enums import Role
from app.extraction import pipeline
from app.extraction.normalize import FIELDS, FieldInput, normalize, record_values
from app.outbox import service as outbox

r, rev = t.production_record, t.record_revision


def _revision_fields(row: Any) -> dict[str, Any]:
    return {
        "production_date": row.production_date.isoformat(),
        "department_id": str(row.department_id),
        "operator_name": row.operator_name,
        "machine_id": str(row.machine_id),
        "production_qty": format(Decimal(row.production_qty), "f"),
        "target_qty": format(Decimal(row.target_qty), "f"),
        "unit": row.unit,
        "status": row.status,
        "stop_minutes": row.stop_minutes,
        "remarks": row.remarks,
    }


def load_record(conn: Connection, principal: Principal, record_id: uuid.UUID, lock: bool = False) -> Any:
    q = select(r).where(r.c.id == record_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None or not principal.can_access_department(row.department_id):
        raise not_found()
    return row


def record_view(conn: Connection, principal: Principal, record_id: uuid.UUID) -> dict[str, Any]:
    row = load_record(conn, principal, record_id)
    revisions = conn.execute(select(rev).where(rev.c.record_id == row.id).order_by(rev.c.number)).all()
    current = next(x for x in revisions if x.id == row.current_revision_id)
    names = dict(conn.execute(select(t.department.c.id, t.department.c.name)).all())
    machines = dict(conn.execute(select(t.machine.c.id, t.machine.c.code)).all())
    can_see_source = principal.has_any(Role.REVIEWER, Role.UPLOADER)
    return {
        "id": str(row.id),
        "state": row.state,
        "version": row.version,
        "entry_key": str(row.entry_key),
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
        "archive_reason": row.archive_reason,
        "current_revision": current.number,
        "fields": _revision_fields(current),
        "display": {"department": names.get(current.department_id), "machine": machines.get(current.machine_id)},
        "revisions": [
            {
                "id": str(x.id),
                "number": x.number,
                "state": x.approval_state,
                "fields": _revision_fields(x),
                "reason": x.reason,
                "created_by": str(x.created_by),
                "created_at": x.created_at.isoformat(),
                "approved_by": str(x.approved_by) if x.approved_by else None,
                "approved_at": x.approved_at.isoformat() if x.approved_at else None,
                # Viewers see approved data but not source provenance by default (spec §2).
                "provenance": x.provenance if can_see_source else None,
            }
            for x in revisions
        ],
    }


def propose_revision(
    conn: Connection,
    principal: Principal,
    record_id: uuid.UUID,
    expected_version: int,
    changes: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    if not principal.has_any(Role.REVIEWER):
        raise forbidden()
    row = load_record(conn, principal, record_id, lock=True)
    if row.version != expected_version:
        raise precondition_failed(row.version)
    if row.state != "ACTIVE":
        raise conflict("ARCHIVED", "Archived records cannot be corrected.")
    if conn.execute(select(rev.c.id).where(rev.c.record_id == row.id, rev.c.approval_state == "PENDING")).first():
        raise conflict("PENDING_REVISION_EXISTS", "A correction is already waiting for approval.")
    current = conn.execute(select(rev).where(rev.c.id == row.current_revision_id)).one()
    merged = _revision_fields(current) | {k: v for k, v in changes.items() if k in FIELDS}
    inputs = {n: FieldInput(source="reviewer", value=merged[n], raw=None) for n in FIELDS}
    norm = normalize(inputs, pipeline.load_context(conn, row.tenant_id))
    if norm.blocking:
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            f"Review {len(norm.blocking)} field(s).",
            [Issue(i.code, i.message, i.field) for i in norm.blocking],
        )
    values = record_values(norm.fields)
    if uuid.UUID(str(values["department_id"])) not in principal.department_ids:
        raise forbidden("You cannot move a record into a department you are not granted.")
    number = conn.execute(select(func.max(rev.c.number)).where(rev.c.record_id == row.id)).scalar_one() + 1
    revision_id = uuid.uuid4()
    conn.execute(
        insert(rev).values(
            id=revision_id,
            tenant_id=row.tenant_id,
            record_id=row.id,
            number=number,
            **values,
            provenance={
                "corrects_revision": current.number,
                "changed_fields": sorted(k for k in changes if k in FIELDS),
            },
            approval_state="PENDING",
            reason=reason,
            created_by=principal.membership_id,
        )
    )
    conn.execute(update(r).where(r.c.id == row.id).values(version=r.c.version + 1))
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="REVISION_PROPOSED",
        object_type="production_record",
        object_id=row.id,
        object_revision=number,
        reason=reason,
        before=_revision_fields(current),
        after=_revision_fields(conn.execute(select(rev).where(rev.c.id == revision_id)).one()),
    )
    return {"revision_id": str(revision_id), "number": number, "state": "PENDING", "version": row.version + 1}


def decide_revision(
    conn: Connection,
    principal: Principal,
    record_id: uuid.UUID,
    revision_id: uuid.UUID,
    approve: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    if not principal.has_any(Role.REVIEWER):
        raise forbidden()
    row = load_record(conn, principal, record_id, lock=True)
    pending = conn.execute(
        select(rev).where(rev.c.id == revision_id, rev.c.record_id == row.id).with_for_update()
    ).one_or_none()
    if pending is None:
        raise not_found()
    if pending.approval_state != "PENDING":
        raise conflict("IMMUTABLE", f"This revision is already {pending.approval_state.lower()}.")
    if not principal.can_access_department(pending.department_id):
        raise forbidden()
    now = datetime.now(UTC)
    if not approve:
        conn.execute(update(rev).where(rev.c.id == pending.id).values(approval_state="REJECTED"))
        audit.record(
            conn,
            tenant_id=row.tenant_id,
            actor=principal.actor,
            action="REVISION_REJECTED",
            object_type="production_record",
            object_id=row.id,
            object_revision=pending.number,
            reason=reason,
        )
        return record_view(conn, principal, row.id)
    conn.execute(
        update(rev)
        .where(rev.c.id == pending.id)
        .values(approval_state="APPROVED", approved_by=principal.membership_id, approved_at=now)
    )
    conn.execute(
        update(r)
        .where(r.c.id == row.id)
        .values(
            current_revision_id=pending.id,
            production_date=pending.production_date,
            department_id=pending.department_id,
            version=r.c.version + 1,
        )
    )
    conn.execute(
        update(t.tenant).where(t.tenant.c.id == row.tenant_id).values(data_version=t.tenant.c.data_version + 1)
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="REVISION_APPROVED",
        object_type="production_record",
        object_id=row.id,
        object_revision=pending.number,
    )
    outbox.enqueue(
        conn,
        tenant_id=row.tenant_id,
        event_type="production_record.changed",
        event_key=f"record:{row.id}:{pending.number}",
        payload={"record_id": str(row.id), "revision": pending.number},
    )
    return record_view(conn, principal, row.id)


def archive(conn: Connection, principal: Principal, record_id: uuid.UUID, reason: str) -> dict[str, Any]:
    if not principal.has_any(Role.REVIEWER):
        raise forbidden()
    row = load_record(conn, principal, record_id, lock=True)
    if row.state == "ARCHIVED":
        return record_view(conn, principal, row.id)  # already archived: replay-safe
    conn.execute(
        update(r)
        .where(r.c.id == row.id)
        .values(
            state="ARCHIVED",
            archived_at=func.now(),
            archived_by=principal.membership_id,
            archive_reason=reason,
            version=r.c.version + 1,
        )
    )
    conn.execute(
        update(t.tenant).where(t.tenant.c.id == row.tenant_id).values(data_version=t.tenant.c.data_version + 1)
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="RECORD_ARCHIVED",
        object_type="production_record",
        object_id=row.id,
        reason=reason,
    )
    outbox.enqueue(
        conn,
        tenant_id=row.tenant_id,
        event_type="production_record.changed",
        event_key=f"record:{row.id}:archived",
        payload={"record_id": str(row.id), "archived": True},
    )
    return record_view(conn, principal, row.id)
