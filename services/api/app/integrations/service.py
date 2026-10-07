"""Integration connections and the downstream sync ledger (FR13, FR15, FR22, addendum A5).

- A company has at most one live connection per provider. Configuration is validated per provider;
  credentials are write-only (encrypted with app.core.crypto, never returned, never logged or audited).
- Saving a connection puts it in NEEDS_TEST and queues a connection test; only a passed test makes it
  CONNECTED, and only CONNECTED destinations receive data.
- record_sync holds, per destination and record, the revision the destination should have and the one
  it is known to have. A newer revision always wins; an older one never overwrites a newer one.
"""

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.company import DEFAULTS
from app.core.errors import Issue, not_found, validation_failed
from app.db import tables as t
from app.jobs import ledger

c, rs, pr = t.integration_connection, t.record_sync, t.powerbi_refresh
PROVIDERS = ("google_sheets", "power_bi", "ms_graph_mail", "erp")
RECORD_TARGETS = ("google_sheets", "erp")  # destinations that receive individual records
SYNC_KIND = {"google_sheets": "sheets.sync", "erp": "erp.sync"}
TEST_KIND, REFRESH_KIND = "integration.test", "powerbi.refresh"
MAX_SYNC_ATTEMPTS = 5

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_ENTRA_TENANT = r"^([0-9a-fA-F-]{36}|[A-Za-z0-9.-]{3,253})$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SheetsConfig(_Strict):
    spreadsheet_id: str = Field(pattern=r"^[A-Za-z0-9_-]{20,100}$")
    tab: str = Field(default="Production_Data", min_length=1, max_length=100)

    @field_validator("tab")
    @classmethod
    def _tab(cls, v: str) -> str:
        if "'" in v or "!" in v:
            raise ValueError("tab names cannot contain ' or !")
        return v


class SheetsSecret(_Strict):
    service_account_json: str = Field(min_length=20, max_length=20_000)

    def stored(self) -> dict[str, Any]:
        try:
            data = json.loads(self.service_account_json)
        except json.JSONDecodeError as exc:
            raise ValueError("paste the service account key file (JSON)") from exc
        if not isinstance(data, dict) or data.get("type") != "service_account":
            raise ValueError("this is not a Google service account key")
        if not data.get("client_email") or "PRIVATE KEY" not in str(data.get("private_key", "")):
            raise ValueError("the key file has no client_email or private_key")
        keep = ("client_email", "private_key", "private_key_id", "token_uri", "project_id")
        return {"service_account": {k: data[k] for k in keep if k in data}}


class PowerBiConfig(_Strict):
    tenant: str = Field(pattern=_ENTRA_TENANT)
    workspace_id: str = Field(pattern=_GUID)
    dataset_id: str = Field(pattern=_GUID)
    min_interval_minutes: int = Field(default=30, ge=5, le=1440)
    stale_after_minutes: int = Field(default=180, ge=15, le=10_080)


class EntraSecret(_Strict):
    client_id: str = Field(pattern=_GUID)
    client_secret: str = Field(min_length=8, max_length=500)

    def stored(self) -> dict[str, Any]:
        return self.model_dump()


class GraphMailConfig(_Strict):
    tenant: str = Field(pattern=_ENTRA_TENANT)
    sender_mailbox: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)


class ErpConfig(_Strict):
    adapter: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    direction: str = "OUTBOUND"


class ErpSecret(_Strict):
    values: dict[str, str] = Field(default_factory=dict, max_length=20)

    def stored(self) -> dict[str, Any]:
        return self.values


SCHEMAS: dict[str, tuple[type[_Strict], type[_Strict]]] = {
    "google_sheets": (SheetsConfig, SheetsSecret),
    "power_bi": (PowerBiConfig, EntraSecret),
    "ms_graph_mail": (GraphMailConfig, EntraSecret),
    "erp": (ErpConfig, ErpSecret),
}


def validate(provider: str, config: dict[str, Any], secret: dict[str, Any] | None) -> tuple[dict, dict | None]:
    """Returns (config, secret to store or None). Raises 422 with per-field issues."""
    if provider not in SCHEMAS:
        raise validation_failed([Issue("UNKNOWN_PROVIDER", "Choose a supported destination.", "provider")])
    config_model, secret_model = SCHEMAS[provider]
    issues: list[Issue] = []
    clean_config: dict[str, Any] = {}
    clean_secret: dict[str, Any] | None = None
    try:
        clean_config = config_model.model_validate(config).model_dump()
    except ValidationError as exc:
        issues += [Issue("INVALID_CONFIG", e["msg"], "config." + ".".join(map(str, e["loc"]))) for e in exc.errors()]
    if secret is not None:
        try:
            clean_secret = secret_model.model_validate(secret).stored()  # type: ignore[attr-defined]
        except ValidationError as exc:
            # Never echo submitted secret values back in the error.
            issues += [
                Issue("INVALID_SECRET", _secret_msg(e), "secret." + ".".join(map(str, e["loc"]))) for e in exc.errors()
            ]
        except ValueError as exc:
            # Content checks (e.g. "not a service account key") belong to the provider's credential field.
            issues.append(Issue("INVALID_SECRET", str(exc), "secret." + next(iter(secret_model.model_fields))))
    if issues:
        raise validation_failed(issues)
    return clean_config, clean_secret


def _secret_msg(error: dict[str, Any]) -> str:
    return {"missing": "Required.", "extra_forbidden": "Unknown field."}.get(error["type"], "Invalid value.")


def feature_enabled(conn: Connection, tenant_id: uuid.UUID, provider: str) -> bool:
    if provider != "erp":
        return True
    settings = conn.execute(select(t.tenant.c.settings).where(t.tenant.c.id == tenant_id)).scalar_one() or {}
    return bool({**DEFAULTS["feature_flags"], **settings.get("feature_flags", {})}.get("erp_integration"))


# --- views ------------------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def sync_counts(conn: Connection, connection_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    out: dict[uuid.UUID, dict[str, int]] = {cid: {} for cid in connection_ids}
    if connection_ids:
        for cid, state, n in conn.execute(
            select(rs.c.connection_id, rs.c.state, func.count())
            .where(rs.c.connection_id.in_(connection_ids))
            .group_by(rs.c.connection_id, rs.c.state)
        ).all():
            out[cid][state] = n
    return out


def view(row: Any, counts: dict[str, int] | None = None) -> dict[str, Any]:
    """Connection as returned by the API. Credentials are reported only as present/absent."""
    config = dict(row.config or {})
    data = {
        "id": str(row.id),
        "provider": row.provider,
        "name": row.name,
        "state": row.state,
        "config": config,
        "has_secret": row.secret_ciphertext is not None,
        "config_version": row.config_version,
        "mapping_version": row.mapping_version,
        "last_test": {"at": _iso(row.last_test_at), "ok": row.last_test_ok},
        "last_error": {"code": row.last_error_code, "message": row.last_error_message} if row.last_error_code else None,
        "last_sync_at": _iso(row.last_sync_at),
        "created_at": _iso(row.created_at),
        "disconnected_at": _iso(row.disconnected_at),
        "version": row.version,
    }
    if row.provider in RECORD_TARGETS:
        counts = counts or {}
        data["sync"] = {s: counts.get(s, 0) for s in ("PENDING", "SYNCED", "FAILED", "CONFLICT")}
    if row.provider == "erp" and config.get("adapter") == "mock":
        data["mock"] = True  # always labelled in the UI
    return data


def load(conn: Connection, connection_id: uuid.UUID, lock: bool = False) -> Any:
    q = select(c).where(c.c.id == connection_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None:
        raise not_found()
    return row


def live(conn: Connection, provider: str) -> Any | None:
    return conn.execute(select(c).where(c.c.provider == provider, c.c.state != "DISCONNECTED")).one_or_none()


# --- record payloads and the sync ledger -------------------------------------------------------


def record_payloads(conn: Connection, record_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    """Current approved revision of each record (ACTIVE or ARCHIVED), as sent to Sheets and the ERP."""
    r, rev, d, m = t.production_record, t.record_revision, t.department, t.machine
    rows = conn.execute(
        select(
            r.c.id,
            r.c.state,
            r.c.updated_at,
            rev.c.number,
            rev.c.production_date,
            rev.c.department_id,
            d.c.name.label("department_name"),
            rev.c.machine_id,
            m.c.code.label("machine_code"),
            rev.c.operator_name,
            rev.c.production_qty,
            rev.c.target_qty,
            rev.c.unit,
            rev.c.status,
            rev.c.stop_minutes,
            rev.c.remarks,
            rev.c.approved_at,
        )
        .join(rev, rev.c.id == r.c.current_revision_id)
        .join(d, d.c.id == rev.c.department_id)
        .join(m, m.c.id == rev.c.machine_id)
        .where(r.c.id.in_(record_ids))
    ).all()
    return [
        {
            "record_id": str(x.id),
            "revision": x.number,
            "production_date": x.production_date.isoformat(),
            "department_id": str(x.department_id),
            "department_name": x.department_name,
            "machine_id": str(x.machine_id),
            "machine_code": x.machine_code,
            "operator_name": x.operator_name,
            "production_qty": format(x.production_qty, "f"),
            "target_qty": format(x.target_qty, "f"),
            "unit": x.unit,
            "status": x.status,
            "stop_minutes": x.stop_minutes,
            "remarks": x.remarks or "",
            "state": x.state,
            "approved_at": _iso(x.approved_at),
            "updated_at": _iso(x.updated_at),
        }
        for x in rows
    ]


def mark_pending(
    conn: Connection, tenant_id: uuid.UUID, connection_id: uuid.UUID, record_ids: list[uuid.UUID] | None = None
) -> int:
    """Queue records (all of the company's approved records when None) for a destination.

    Upsert: target_revision only ever grows (GREATEST), so a late event cannot roll a destination back.
    """
    r, rev = t.production_record, t.record_revision
    q = select(r.c.id, rev.c.number).join(rev, rev.c.id == r.c.current_revision_id)
    if record_ids is not None:
        q = q.where(r.c.id.in_(record_ids))
    rows = conn.execute(q).all()
    if not rows:
        return 0
    stmt = insert(rs).values(
        [
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "record_id": rid,
                "target_revision": number,
                "state": "PENDING",
            }
            for rid, number in rows
        ]
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["connection_id", "record_id"],
            set_={
                "target_revision": func.greatest(rs.c.target_revision, stmt.excluded.target_revision),
                "state": "PENDING",
                "attempts": 0,
                "error_code": None,
                "error_message": None,
            },
        )
    )
    return len(rows)


def schedule_sync(conn: Connection, row: Any, created_by: uuid.UUID | None = None) -> uuid.UUID | None:
    if row.provider in SYNC_KIND:
        return ledger.ensure_job(
            conn, tenant_id=row.tenant_id, kind=SYNC_KIND[row.provider], object_id=row.id, created_by=created_by
        )
    if row.provider == "power_bi":
        return ledger.ensure_job(
            conn, tenant_id=row.tenant_id, kind=REFRESH_KIND, object_id=row.id, created_by=created_by
        )
    return None


def record_sync_states(conn: Connection, record_ids: list[uuid.UUID]) -> dict[str, str]:
    """Google Sheets state per record for lists: SYNCED, PENDING, FAILED, CONFLICT or NOT_CONFIGURED."""
    sheets = conn.execute(
        select(c.c.id).where(c.c.provider == "google_sheets", c.c.state != "DISCONNECTED")
    ).scalar_one_or_none()
    if sheets is None or not record_ids:
        return {}
    return {
        str(rid): state
        for rid, state in conn.execute(
            select(rs.c.record_id, rs.c.state).where(rs.c.connection_id == sheets, rs.c.record_id.in_(record_ids))
        ).all()
    }


def sheets_configured(conn: Connection) -> bool:
    return live(conn, "google_sheets") is not None


# --- Power BI freshness (FR15, TC30/TC31) ------------------------------------------------------


def powerbi_status(conn: Connection, tenant_id: uuid.UUID) -> dict[str, Any]:
    """FRESH only when a completed refresh covers the current data version and is not older than the
    configured window. The native dashboard never depends on this."""
    row = live(conn, "power_bi")
    if row is None:
        return {"state": "NOT_CONFIGURED"}
    data_version = conn.execute(select(t.tenant.c.data_version).where(t.tenant.c.id == tenant_id)).scalar_one()
    base = {"connection_id": str(row.id), "connection_state": row.state, "data_version": data_version}
    if row.state != "CONNECTED":
        return base | {"state": "NOT_CONNECTED"}
    latest = conn.execute(
        select(pr).where(pr.c.connection_id == row.id).order_by(pr.c.requested_at.desc()).limit(1)
    ).one_or_none()
    done = conn.execute(
        select(pr)
        .where(pr.c.connection_id == row.id, pr.c.state == "COMPLETED")
        .order_by(pr.c.source_data_version.desc(), pr.c.completed_at.desc())
        .limit(1)
    ).one_or_none()
    stale_after = int(row.config.get("stale_after_minutes", 180))
    out = base | {
        "refreshed_data_version": done.source_data_version if done else None,
        "last_refreshed_at": _iso(done.completed_at) if done else None,
        "last_request": None
        if latest is None
        else {"state": latest.state, "requested_at": _iso(latest.requested_at), "error_code": latest.error_code},
        "stale_after_minutes": stale_after,
    }
    if latest is not None and latest.state in ("REQUESTED", "IN_PROGRESS"):
        return out | {"state": "REFRESHING", "stale": done is None or done.source_data_version < data_version}
    if done is None:
        return out | {"state": "FAILED" if latest is not None else "NEVER_REFRESHED", "stale": True}
    age_min = (datetime.now(UTC) - done.completed_at).total_seconds() / 60
    stale = done.source_data_version < data_version or age_min > stale_after
    if latest is not None and latest.state == "FAILED" and stale:
        return out | {"state": "FAILED", "stale": True}
    return out | {"state": "STALE" if stale else "FRESH", "stale": stale}


def safe_error(message: str) -> str:
    """Provider messages are ours (adapters never pass provider bodies), but trim defensively."""
    return re.sub(r"\s+", " ", message)[:300]
