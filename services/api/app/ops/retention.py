"""Retention purge (FR25, spec §13 "Proposed retention defaults"; TC48).

Categories (days from company settings `retention_days`; defaults need business approval):
  REJECTED_UPLOAD  rejected files: 7 days after upload
  SOURCE           original and derived files (page images, extracted text): `source` days after upload
  REPORT_FILE      report PDFs: `business` days after the last relevant event (creation or an email sent with it)
  EXPORT_FILE      Excel exports: `business` days after creation
Unfinished uploads (24 h) are already expired by the ingestion maintenance task.

Only bytes are deleted. Metadata stays: hashes, provenance, report facts and metrics, and a *_purged_at marker so
the UI can say "source no longer available". Active holds (upload, its batch, or report) block a purge. A dry run
only counts. A failed deletion is recorded and retried on the next run because the object stays eligible.
Every deletion is a PURGED retention_event with the exact keys: the deletion manifest that is replayed after a
backup restore (replay_manifest) so deleted content is never resurrected.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, and_, func, insert, or_, select, update

from app.audit import service as audit
from app.core.company import DEFAULTS
from app.db import tables as t
from app.db.engine import tenant_tx
from app.storage.objects import get_storage, object_key_for

REJECTED_DAYS = 7
CATEGORIES = ("REJECTED_UPLOAD", "SOURCE", "REPORT_FILE", "EXPORT_FILE")
h, ev, rr = t.retention_hold, t.retention_event, t.retention_run


@dataclass
class Item:
    category: str
    object_type: str
    object_id: uuid.UUID
    keys: list[str] = field(default_factory=list)
    held: bool = False


def _held(conn: Connection, object_type: str) -> set[uuid.UUID]:
    return set(
        conn.execute(select(h.c.object_id).where(h.c.object_type == object_type, h.c.released_at.is_(None))).scalars()
    )


def _upload_keys(tenant_id: uuid.UUID, upload_id: uuid.UUID) -> list[str]:
    keys = [object_key_for("quarantine", tenant_id, upload_id), object_key_for("originals", tenant_id, upload_id)]
    return keys + get_storage().list_keys(f"derived/{tenant_id}/{upload_id}")


def eligible(conn: Connection, tenant_id: uuid.UUID, now: datetime, with_keys: bool = False) -> list[Item]:
    stored = conn.execute(select(t.tenant.c.settings).where(t.tenant.c.id == tenant_id)).scalar_one() or {}
    days = {**DEFAULTS["retention_days"], **(stored.get("retention_days") or {})}
    held_uploads, held_batches, held_reports = _held(conn, "upload"), _held(conn, "batch"), _held(conn, "report")
    u = t.upload
    items: list[Item] = []
    rows = conn.execute(
        select(u.c.id, u.c.batch_id, u.c.state).where(
            u.c.source_purged_at.is_(None),
            or_(
                and_(u.c.state == "REJECTED", u.c.created_at < now - timedelta(days=REJECTED_DAYS)),
                and_(
                    u.c.state.notin_(("REJECTED", "UPLOADING")),
                    u.c.created_at < now - timedelta(days=int(days["source"])),
                ),
            ),
        )
    ).all()
    for r in rows:
        items.append(
            Item(
                "REJECTED_UPLOAD" if r.state == "REJECTED" else "SOURCE",
                "upload",
                r.id,
                held=r.id in held_uploads or r.batch_id in held_batches,
            )
        )

    business = now - timedelta(days=int(days["business"]))
    em = t.email_message
    last_email = select(func.max(em.c.created_at)).where(em.c.report_id == t.report.c.id).scalar_subquery()
    for r in conn.execute(
        select(t.report.c.id, t.report.c.file_key).where(
            t.report.c.file_key.isnot(None),
            t.report.c.file_purged_at.is_(None),
            func.greatest(t.report.c.created_at, func.coalesce(last_email, t.report.c.created_at)) < business,
        )
    ).all():
        items.append(Item("REPORT_FILE", "report", r.id, [r.file_key], held=r.id in held_reports))
    x = t.export
    for r in conn.execute(
        select(x.c.id, x.c.file_key, x.c.report_id).where(
            x.c.file_key.isnot(None), x.c.file_purged_at.is_(None), x.c.created_at < business
        )
    ).all():
        items.append(Item("EXPORT_FILE", "export", r.id, [r.file_key], held=r.report_id in held_reports))
    if with_keys:
        for it in items:
            if it.object_type == "upload" and not it.held:
                it.keys = _upload_keys(tenant_id, it.object_id)
    return items


def _counts(items: list[Item]) -> dict[str, dict[str, int]]:
    return {
        c: {
            "eligible": sum(1 for i in items if i.category == c and not i.held),
            "held": sum(1 for i in items if i.category == c and i.held),
        }
        for c in CATEGORIES
    }


def _mark(conn: Connection, it: Item) -> None:
    table = {"upload": t.upload, "report": t.report, "export": t.export}[it.object_type]
    column = "source_purged_at" if it.object_type == "upload" else "file_purged_at"
    conn.execute(update(table).where(table.c.id == it.object_id).values({column: func.now()}))


def run(
    tenant_id: uuid.UUID, *, dry_run: bool, requested_by: uuid.UUID | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Purge (or count) eligible bytes for one company. Deletions happen outside any transaction."""
    with tenant_tx(tenant_id) as conn:
        now = now or conn.execute(select(func.now())).scalar_one()
        items = eligible(conn, tenant_id, now, with_keys=not dry_run)
        run_id = conn.execute(
            insert(rr)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                dry_run=dry_run,
                eligible=_counts(items),
                requested_by=requested_by,
            )
            .returning(rr.c.id)
        ).scalar_one()
    purged = held = failed = 0
    if not dry_run:
        storage = get_storage()
        for it in items:
            if it.held:
                held += 1
                with tenant_tx(tenant_id) as conn:
                    conn.execute(
                        insert(ev).values(
                            id=uuid.uuid4(),
                            tenant_id=tenant_id,
                            run_id=run_id,
                            action="SKIPPED_HOLD",
                            category=it.category,
                            object_type=it.object_type,
                            object_id=it.object_id,
                        )
                    )
                continue
            try:
                for key in it.keys:
                    storage.delete(key)
            except Exception as exc:  # noqa: BLE001 - retried on the next run; the object stays eligible
                failed += 1
                with tenant_tx(tenant_id) as conn:
                    conn.execute(
                        insert(ev).values(
                            id=uuid.uuid4(),
                            tenant_id=tenant_id,
                            run_id=run_id,
                            action="FAILED",
                            category=it.category,
                            object_type=it.object_type,
                            object_id=it.object_id,
                            object_keys=it.keys,
                            error=type(exc).__name__,
                        )
                    )
                continue
            purged += 1
            with tenant_tx(tenant_id) as conn:
                _mark(conn, it)
                conn.execute(
                    insert(ev).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        run_id=run_id,
                        action="PURGED",
                        category=it.category,
                        object_type=it.object_type,
                        object_id=it.object_id,
                        object_keys=it.keys,
                    )
                )
    with tenant_tx(tenant_id) as conn:
        conn.execute(
            update(rr).where(rr.c.id == run_id).values(purged=purged, held=held, failed=failed, finished_at=func.now())
        )
        audit.record(
            conn,
            tenant_id=tenant_id,
            actor=audit.Actor("user", requested_by) if requested_by else audit.Actor("service", None),
            action="RETENTION_DRY_RUN" if dry_run else "RETENTION_PURGE",
            object_type="retention_run",
            object_id=run_id,
            after={"eligible": _counts(items), "purged": purged, "held": held, "failed": failed},
        )
    return {
        "run_id": str(run_id),
        "dry_run": dry_run,
        "eligible": _counts(items),
        "purged": purged,
        "held": held,
        "failed": failed,
    }


def replay_manifest(conn: Connection) -> int:
    """After restoring a backup: delete again every object recorded as PURGED (owner connection, all companies).
    Idempotent; deleting an object that is already gone is not an error."""
    storage = get_storage()
    n = 0
    for keys in conn.execute(select(ev.c.object_keys).where(ev.c.action == "PURGED")).scalars():
        for key in keys:
            storage.delete(key)
            n += 1
    for table, col in ((t.upload, "source_purged_at"), (t.report, "file_purged_at"), (t.export, "file_purged_at")):
        ids = select(ev.c.object_id).where(ev.c.action == "PURGED")
        conn.execute(
            update(table).where(table.c.id.in_(ids), getattr(table.c, col).is_(None)).values({col: func.now()})
        )
    return n


def due(conn: Connection, now: datetime) -> bool:
    """A daily purge is due when no non-dry run started in the last 23 hours."""
    last = conn.execute(select(func.max(rr.c.started_at)).where(rr.c.dry_run.is_(False))).scalar()
    return last is None or now - last > timedelta(hours=23)
