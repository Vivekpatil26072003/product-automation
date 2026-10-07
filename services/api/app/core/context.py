"""Per-request correlation ID, propagated to audit events and logs."""

import uuid
from contextvars import ContextVar

_request_id: ContextVar[uuid.UUID | None] = ContextVar("request_id", default=None)


def set_request_id(value: uuid.UUID) -> None:
    _request_id.set(value)


def current_request_id() -> uuid.UUID:
    rid = _request_id.get()
    if rid is None:
        rid = uuid.uuid4()
        _request_id.set(rid)
    return rid
