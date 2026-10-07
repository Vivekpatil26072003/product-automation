"""API operation 58: liveness and readiness. External AI/email providers are reported separately
(later milestones) and never make the core service unready.
"""

import redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.db.engine import app_engine
from app.storage.objects import get_storage

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        with app_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001 - readiness reports, it does not raise
        checks["database"] = "unavailable"
    try:
        redis.Redis.from_url(get_settings().redis_url, socket_timeout=2).ping()
        checks["queue"] = "ok"
    except Exception:  # noqa: BLE001
        checks["queue"] = "unavailable"
    try:
        get_storage().ping()
        checks["storage"] = "ok"
    except Exception:  # noqa: BLE001
        checks["storage"] = "unavailable"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "unready", "checks": checks}, status_code=200 if ok else 503)
