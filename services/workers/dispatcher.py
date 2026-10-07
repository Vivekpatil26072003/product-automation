"""Outbox dispatcher (FR27). Turns committed outbox events into jobs exactly once per (event, kind).

Runs in the dispatcher scope, which may read outbox/job rows of every tenant but no business tables.
A crash after the business commit but before dispatch is harmless: the next scan picks the event up.
"""

import logging
import uuid

from sqlalchemy import func, select, update

from app.db import tables as t
from app.db.engine import dispatcher_tx
from app.jobs import ledger
from workers.registry import EVENT_ROUTES

log = logging.getLogger("workers.dispatcher")


def dispatch_batch(limit: int = 100) -> set[str]:
    """Dispatch up to `limit` pending events. Returns the job kinds that received new work."""
    kinds: set[str] = set()
    o = t.outbox
    with dispatcher_tx() as conn:
        rows = conn.execute(
            # Only events with a consumer are dispatched; others stay pending, so no event is lost and
            # none blocks the queue.
            select(o).where(o.c.dispatched_at.is_(None), o.c.event_type.in_(list(EVENT_ROUTES)))
            .order_by(o.c.created_at).limit(limit)
            .with_for_update(skip_locked=True)
        ).all()  # fmt: skip
        for row in rows:
            routes = EVENT_ROUTES.get(row.event_type)
            if not routes:
                log.error("no route for outbox event type=%s key=%s", row.event_type, row.id)
                conn.execute(
                    update(o).where(o.c.id == row.id)
                    .values(
                        dispatched_at=func.now(), dispatch_attempts=o.c.dispatch_attempts + 1, last_error="NO_ROUTE"
                    )
                )  # fmt: skip
                continue
            for kind, field, coalesce in routes:
                enqueue = ledger.ensure_job if coalesce else ledger.create_job
                enqueue(
                    conn, tenant_id=row.tenant_id, kind=kind, object_id=uuid.UUID(str(row.payload[field])),
                    source_event_key=row.event_key,
                )  # fmt: skip
                kinds.add(kind)
            conn.execute(
                update(o).where(o.c.id == row.id)
                .values(dispatched_at=func.now(), dispatch_attempts=o.c.dispatch_attempts + 1)
            )  # fmt: skip
    return kinds
