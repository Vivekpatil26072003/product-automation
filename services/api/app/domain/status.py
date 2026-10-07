"""End-of-period status normalization. Status is never inferred from downtime or remarks."""

from dataclasses import dataclass, field

from app.domain.enums import Status
from app.domain.issues import FieldIssue

_ALIASES = {
    "running": Status.RUNNING,
    "run": Status.RUNNING,
    "in progress": Status.RUNNING,
    "completed": Status.COMPLETED,
    "complete": Status.COMPLETED,
    "done": Status.COMPLETED,
    "finished": Status.COMPLETED,
    "pending": Status.PENDING,
    "hold": Status.HOLD,
    "on hold": Status.HOLD,
}


@dataclass
class StatusResult:
    value: Status | None
    issues: list[FieldIssue] = field(default_factory=list)


def normalize_status(raw: str | None) -> StatusResult:
    if raw is None or not raw.strip():
        return StatusResult(None, [FieldIssue("status", "MISSING_VALUE", "Status is required.")])
    key = " ".join(raw.strip().lower().replace("-", " ").replace("_", " ").split())
    hit = _ALIASES.get(key)
    if hit is None:
        return StatusResult(
            None,
            [FieldIssue("status", "UNKNOWN_STATUS", f'"{raw}" is not RUNNING, COMPLETED, PENDING or HOLD.')],
        )
    return StatusResult(hit)
