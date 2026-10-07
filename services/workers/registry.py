"""Job handlers and outbox routing.

Handlers must be idempotent: a job can run more than once (at-least-once delivery), so every
external or durable effect needs a unique effect key. Handlers never receive document content in
the queue message, only IDs, and they load data under the job's tenant.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from workers.runtime import JobContext


@dataclass
class Outcome:
    state: str = "SUCCEEDED"  # SUCCEEDED | PARTIAL | FAILED
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


Handler = Callable[[JobContext], Outcome]

HANDLERS: dict[str, Handler] = {}

# outbox event_type -> [(job kind, payload field holding the job's object_id, coalesce)]
# coalesce=True: several events for one object share a single not-yet-started job (ledger.ensure_job).
EVENT_ROUTES: dict[str, list[tuple[str, str, bool]]] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if kind in HANDLERS:
            raise ValueError(f"duplicate handler for {kind}")
        HANDLERS[kind] = fn
        return fn

    return register


def route(event_type: str, kind: str, object_field: str, coalesce: bool = False) -> None:
    EVENT_ROUTES.setdefault(event_type, []).append((kind, object_field, coalesce))


# --- M1 handlers ----------------------------------------------------------------------------
# system.noop proves the outbox -> job -> worker path end to end (health/smoke checks).
route("system.ping", "system.noop", "object_id")


@handler("system.noop")
def _noop(ctx: JobContext) -> Outcome:
    return Outcome(result={"echo": str(ctx.claim.object_id)})


# M2+ handlers register themselves on import.
from workers import (  # noqa: E402,F401,E501
    automation,
    exports,
    extraction,
    ingestion,
    integrations,
    mail,
    owner_reports,
    reports,
)
