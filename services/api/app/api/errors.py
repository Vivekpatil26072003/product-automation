"""Maps every failure to the stable error envelope. Internal details never reach the client."""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import current_request_id, set_request_id
from app.core.errors import ApiError, Issue

log = logging.getLogger("app.api")

# PostgreSQL SQLSTATEs raised by constraints and triggers in migrations/.
_PG_CONFLICT = {"23505": ("CONFLICT_DUPLICATE", "An item with the same identifier already exists.")}
_PG_UNPROCESSABLE = {
    "23503": ("INVALID_REFERENCE", "A referenced item does not exist or is still in use."),
    "23514": ("CONSTRAINT_VIOLATION", "The values break a data rule."),
    "23502": ("REQUIRED", "A required value is missing."),
}


def _body(code: str, message: str, fields: list[Issue] | None = None, extra: dict | None = None) -> dict:
    err = {
        "code": code,
        "message": message,
        "fields": [f.as_dict() for f in fields or []],
        "request_id": str(current_request_id()),
    }
    if extra:
        err.update(extra)
    return {"error": err}


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(request: Request, call_next):
        raw = request.headers.get("X-Request-ID", "")
        try:
            rid = uuid.UUID(raw)
        except ValueError:
            rid = uuid.uuid4()
        set_request_id(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(rid)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        return JSONResponse(
            _body(exc.code, exc.message, exc.fields, exc.extra), status_code=exc.status, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation(_: Request, exc: RequestValidationError):
        issues = []
        for e in exc.errors():
            loc = [str(p) for p in e.get("loc", []) if p not in ("body", "query", "path", "header")]
            issues.append(Issue(code=str(e.get("type", "invalid")).upper(), message=e.get("msg", "Invalid value."),
                                field=".".join(loc) or None))  # fmt: skip
        n = len(issues)
        return JSONResponse(
            _body("VALIDATION_FAILED", f"Review {n} field{'s' if n != 1 else ''}.", issues), status_code=422
        )

    @app.exception_handler(IntegrityError)
    async def integrity(_: Request, exc: IntegrityError):
        state = getattr(exc.orig, "sqlstate", None)
        if state in _PG_CONFLICT:
            return JSONResponse(_body(*_PG_CONFLICT[state]), status_code=409)
        code, message = _PG_UNPROCESSABLE.get(state, ("CONSTRAINT_VIOLATION", "The values break a data rule."))
        return JSONResponse(_body(code, message), status_code=409 if state == "23503" else 422)

    @app.exception_handler(DBAPIError)
    async def db_error(_: Request, exc: DBAPIError):
        state = getattr(exc.orig, "sqlstate", None)
        if state == "42501":  # insufficient privilege / immutable-row trigger
            return JSONResponse(_body("IMMUTABLE", "This item can no longer be changed."), status_code=409)
        log.error("database error sqlstate=%s", state)
        return JSONResponse(_body("SERVICE_UNAVAILABLE", "The service is temporarily unavailable."), status_code=503)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        codes = {404: ("NOT_FOUND", "The requested item was not found."), 405: ("METHOD_NOT_ALLOWED", "Not allowed.")}
        code, message = codes.get(exc.status_code, ("HTTP_ERROR", "The request could not be completed."))
        return JSONResponse(_body(code, message), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error type=%s", type(exc).__name__)
        return JSONResponse(_body("INTERNAL_ERROR", "Something went wrong. Quote the request ID to support."),
                            status_code=500)  # fmt: skip
