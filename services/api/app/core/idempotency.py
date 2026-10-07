"""Idempotency-Key handling for mutating commands (spec §9).

The key row is inserted in the SAME transaction as the effect. A concurrent request with the same
key blocks on the primary key until the first commits, then replays its stored response. If the
effect fails, the transaction rolls back and the key is free to retry. Same key with a different
payload returns 409.
"""

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db import tables as t


def _hash(payload: Any) -> bytes:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).digest()


def run_idempotent(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    route: str,
    key: str | None,
    payload: Any,
    effect: Callable[[], tuple[int, dict[str, Any]]],
) -> tuple[int, dict[str, Any]]:
    if not key or len(key) > 200:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header (1–200 characters).")

    now = datetime.now(UTC)
    ident = and_(
        t.idempotency_record.c.tenant_id == tenant_id,
        t.idempotency_record.c.actor_id == actor_id,
        t.idempotency_record.c.route == route,
        t.idempotency_record.c.idem_key == key,
    )
    # An expired key no longer protects anything; clear it so the new request proceeds.
    conn.execute(delete(t.idempotency_record).where(ident, t.idempotency_record.c.expires_at < now))

    request_hash = _hash(payload)
    inserted = conn.execute(
        insert(t.idempotency_record)
        .values(
            tenant_id=tenant_id,
            actor_id=actor_id,
            route=route,
            idem_key=key,
            request_hash=request_hash,
            expires_at=now + timedelta(hours=get_settings().idempotency_ttl_hours),
        )
        .on_conflict_do_nothing()
        .returning(t.idempotency_record.c.idem_key)
    ).scalar_one_or_none()

    if inserted is None:
        row = conn.execute(select(t.idempotency_record).where(ident)).one()
        if bytes(row.request_hash) != request_hash:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used with a different request.")
        if row.status_code is None:
            raise ApiError(409, "REQUEST_IN_PROGRESS", "The original request is still being processed.")
        return row.status_code, row.response_body

    status, body = effect()
    conn.execute(update(t.idempotency_record).where(ident).values(status_code=status, response_body=body))
    return status, body
