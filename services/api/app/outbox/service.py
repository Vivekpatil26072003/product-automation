"""Transactional outbox (FR27). Events are written in the same transaction as the business change;
network calls never happen inside that transaction. The dispatcher (services/workers) turns
committed events into jobs, so a crash between commit and enqueue is recovered by the outbox scan.

Payloads carry IDs only, never document content.
"""

import uuid
from typing import Any

from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import insert

from app.core.context import current_request_id
from app.db import tables as t


def enqueue(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    event_key: str,
    payload: dict[str, Any],
) -> bool:
    """Insert once per event_key. Returns False when the event already exists (idempotent replay)."""
    inserted = conn.execute(
        insert(t.outbox)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            event_key=event_key,
            event_type=event_type,
            payload=payload,
            correlation_id=current_request_id(),
        )
        .on_conflict_do_nothing(index_elements=["event_key"])
        .returning(t.outbox.c.id)
    ).scalar_one_or_none()
    return inserted is not None
