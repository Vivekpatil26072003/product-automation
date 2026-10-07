"""Stable API error contract: {error:{code,message,fields,request_id}} (spec §10)."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Issue:
    code: str
    message: str
    field: str | None = None
    severity: str = "error"

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class ApiError(Exception):
    status: int
    code: str
    message: str
    fields: list[Issue] = field(default_factory=list)
    headers: dict[str, str] | None = None
    extra: dict[str, Any] | None = None


def not_found() -> ApiError:
    # Neutral: never reveals whether an object exists in another tenant or department.
    return ApiError(404, "NOT_FOUND", "The requested item was not found.")


def forbidden(message: str = "You do not have permission to perform this action.") -> ApiError:
    return ApiError(403, "FORBIDDEN", message)


def unauthenticated() -> ApiError:
    return ApiError(401, "UNAUTHENTICATED", "Sign in to continue.")


def validation_failed(issues: list[Issue]) -> ApiError:
    n = len(issues)
    return ApiError(422, "VALIDATION_FAILED", f"Review {n} field{'s' if n != 1 else ''}.", issues)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


def precondition_failed(current_version: int) -> ApiError:
    return ApiError(
        412,
        "STALE_VERSION",
        "This item was changed by someone else. Review the current values before saving again.",
        extra={"current_version": current_version},
    )
