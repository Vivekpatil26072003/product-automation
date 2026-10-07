"""Field-level validation issues produced by deterministic normalization (FR06)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldIssue:
    field: str
    code: str
    message: str
    severity: str = "error"  # "error" blocks approval; "warning" is advisory

    @property
    def blocking(self) -> bool:
        return self.severity == "error"
