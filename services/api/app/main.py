"""FastAPI application factory. Base path /api/v1 (spec §10)."""

from fastapi import FastAPI

from app.api import errors
from app.api.routes import (
    auth,
    automation,
    health,
    ingestion,
    insights,
    integrations,
    masters,
    ops,
    orders,
    owner_reports,
    pick_registers,
    reports,
    review,
    settings,
    shift_reports,
    users,
)
from app.core.config import get_settings
from app.core.logging import configure_logging

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    configure_logging()
    dev = get_settings().app_env in ("development", "test")
    app = FastAPI(
        title="AI Production Automation API",
        version="0.1.0",
        description="Production notes to verified records, reports and controlled email (spec v1.1).",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/api/docs" if dev else None,
        redoc_url=None,
    )
    errors.install(app)

    @app.middleware("http")
    async def security_headers(request, call_next):  # FR25: API responses are never framed, sniffed or cached
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    for module in (
        auth,
        masters,
        users,
        settings,
        ingestion,
        review,
        orders,
        owner_reports,
        shift_reports,
        pick_registers,
        insights,
        reports,
        automation,
        integrations,
        ops,
        health,
    ):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


app = create_app()
