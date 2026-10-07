"""API operation 57: GET/PATCH /settings (company configuration)."""

import re
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from sqlalchemy import Connection, select, update

from app.api.deps import etag, idempotency_key, if_match, require
from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import precondition_failed
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import tenant_tx
from app.domain.enums import Role

router = APIRouter(prefix="/settings", tags=["settings"])

from app.core.company import DEFAULTS  # noqa: E402  (re-exported for callers)


def _no_float(v: object) -> object:
    if isinstance(v, float):
        raise ValueError("send as a decimal string")
    return v


DOMAIN = r"^[a-z0-9-]+(\.[a-z0-9-]+)+$"


class RetentionDays(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: int = Field(ge=1, le=3650)
    business: int = Field(ge=1, le=3650)
    logs: int = Field(ge=1, le=365)


class FeatureFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduling: bool
    auto_send: bool
    erp_integration: bool


class ExceptionRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    low_achievement_pct: int = Field(ge=0, le=100)
    high_achievement_pct: int = Field(ge=100, le=1000)
    stop_minutes: int = Field(ge=1, le=1440)
    lookback_days: int = Field(ge=1, le=31)


class Reminders(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    first_after_minutes: int = Field(ge=0, le=1440)
    second_after_minutes: int = Field(ge=0, le=1440)
    escalate_after_minutes: int = Field(ge=0, le=2880)
    email: bool

    @field_validator("escalate_after_minutes")
    @classmethod
    def _ordered(cls, v: int, info) -> int:
        first, second = info.data.get("first_after_minutes", 0), info.data.get("second_after_minutes", 0)
        if not first <= second <= v:
            raise ValueError("stages must be in order: first <= second <= escalation")
        return v


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = None
    date_order: Literal["DMY", "MDY"] | None = None
    retention_days: RetentionDays | None = None
    daily_spend_limit: (
        Annotated[Decimal, BeforeValidator(_no_float), Field(ge=0, max_digits=14, decimal_places=2)] | None
    ) = None
    submission_cutoff_local_time: str | None = None
    working_days: list[Annotated[int, Field(ge=1, le=7)]] | None = Field(default=None, min_length=1, max_length=7)
    feature_flags: FeatureFlags | None = None
    exception_rules: ExceptionRules | None = None
    reminders: Reminders | None = None
    internal_email_domains: list[Annotated[str, Field(min_length=3, max_length=253, pattern=DOMAIN)]] | None = Field(
        default=None, max_length=20
    )

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA name such as Asia/Kolkata") from exc
        return v

    @field_validator("submission_cutoff_local_time")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("use 24-hour HH:mm")
        return v


def _view(row: Any) -> dict[str, Any]:
    stored = {**DEFAULTS, **(row.settings or {})}
    return {
        "name": row.name,
        "timezone": row.timezone,
        "date_order": row.date_order,
        **stored,
        "data_version": row.data_version,
        "version": row.version,
    }


def _tenant_row(conn: Connection, tenant_id, lock: bool = False):
    q = select(t.tenant).where(t.tenant.c.id == tenant_id)
    return conn.execute(q.with_for_update() if lock else q).one()


@router.get("")
def get_settings_view(principal: Principal = Depends(require(Role.ADMIN))) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        data = _view(_tenant_row(conn, principal.tenant_id))
    return JSONResponse({"data": data}, headers={"ETag": etag(data["version"])})


@router.patch("")
def patch_settings(
    body: SettingsPatch,
    expected_version: int = Depends(if_match),
    principal: Principal = Depends(require(Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    changes = body.model_dump(mode="json", exclude_unset=True)

    def effect() -> tuple[int, dict]:
        row = _tenant_row(conn, principal.tenant_id, lock=True)
        if row.version != expected_version:
            raise precondition_failed(row.version)
        before = _view(row)
        columns = {k: changes.pop(k) for k in ("timezone", "date_order") if k in changes}
        conn.execute(
            update(t.tenant)
            .where(t.tenant.c.id == principal.tenant_id)
            .values(**columns, settings={**(row.settings or {}), **changes}, version=t.tenant.c.version + 1)
        )
        after = _view(_tenant_row(conn, principal.tenant_id))
        audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="SETTINGS_UPDATED",
                     object_type="tenant", object_id=principal.tenant_id, before=before, after=after)  # fmt: skip
        return 200, {"data": after}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id, route="PATCH /settings",
            key=key, payload=[expected_version, body.model_dump(mode="json", exclude_unset=True)], effect=effect,
        )  # fmt: skip
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})
