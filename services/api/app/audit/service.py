"""Append-only audit trail (spec §13). Callers pass safe metadata only: never secrets or raw documents."""

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Connection, insert

from app.core.context import current_request_id
from app.db import tables as t

ActorType = Literal["user", "service", "system"]


@dataclass(frozen=True)
class Actor:
    type: ActorType
    id: uuid.UUID | None


SYSTEM = Actor("system", None)


def record(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    action: str,
    object_type: str,
    object_id: uuid.UUID | None = None,
    object_revision: int | None = None,
    reason: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        insert(t.audit_event).values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            object_revision=object_revision,
            reason=reason,
            before=before,
            after=after,
            correlation_id=current_request_id(),
        )
    )


def security_event(conn: Connection, event: str, detail: str | None = None) -> None:
    conn.execute(
        insert(t.security_event).values(
            id=uuid.uuid4(), event=event, detail=detail, correlation_id=current_request_id()
        )
    )
