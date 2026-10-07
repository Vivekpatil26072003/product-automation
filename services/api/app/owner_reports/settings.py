"""Owner report settings (administrators): owner email, automatic report on/off, company name and the EmailJS
account used to send PDFs from the server (service ID, one reusable template, public key, private key).

Where the EmailJS account comes from, per value:
1. the backend environment (EMAILJS_SERVICE_ID, EMAILJS_TEMPLATE_ID, EMAILJS_PUBLIC_KEY, EMAILJS_PRIVATE_KEY in
   the root .env read by the API and the worker): the recommended place; shown read-only in the settings page;
2. otherwise the value an administrator stored in Settings -> Owner report & email (the private key encrypted at
   rest with INTEGRATION_KEYS, AES-GCM bound to the tenant).
The private key is never returned by the API, written to the audit log or sent to a browser: the view only says
whether one is available and where it comes from.
"""

import re
import uuid
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from app.audit import service as audit
from app.auth.principal import Principal
from app.core import crypto
from app.core.config import get_settings
from app.core.errors import ApiError, Issue, precondition_failed
from app.db import tables as t
from app.mail.emailjs_api import Config

s = t.owner_report_settings
EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;.]{2,}$")
TEXT_FIELDS = ("emailjs_service_id", "emailjs_template_id", "emailjs_public_key")
DEFAULT_MAX_KB = 50


def load(conn: Connection, tenant_id: uuid.UUID) -> Any:
    return conn.execute(select(s).where(s.c.tenant_id == tenant_id)).one_or_none()


def company_name(conn: Connection, tenant_id: uuid.UUID, row: Any = None) -> str:
    row = row if row is not None else load(conn, tenant_id)
    if row is not None and row.company_name:
        return row.company_name
    return conn.execute(select(t.tenant.c.name).where(t.tenant.c.id == tenant_id)).scalar_one()


def env_values() -> dict[str, Any]:
    """EmailJS values set in the backend environment (the private key stays a SecretStr)."""
    cfg = get_settings()
    out: dict[str, Any] = {f: (getattr(cfg, f) or "").strip() or None for f in TEXT_FIELDS}
    key = cfg.emailjs_private_key.get_secret_value().strip() if cfg.emailjs_private_key else ""
    out["emailjs_private_key"] = cfg.emailjs_private_key if key else None
    return out


def sources(row: Any) -> dict[str, str | None]:
    """Where each EmailJS value comes from: "environment", "settings" or None (missing)."""
    env = env_values()
    out: dict[str, str | None] = {}
    for f in TEXT_FIELDS:
        out[f] = "environment" if env[f] else "settings" if row is not None and getattr(row, f) else None
    stored_key = row is not None and row.emailjs_private_key is not None
    out["emailjs_private_key"] = "environment" if env["emailjs_private_key"] else "settings" if stored_key else None
    return out


def missing(row: Any) -> list[str]:
    """EmailJS values still missing before PDFs can be emailed from the server (environment or settings)."""
    return [f for f, src in sources(row).items() if src is None]


def max_request_kb(row: Any) -> int:
    return row.max_request_kb if row is not None else DEFAULT_MAX_KB


def view(conn: Connection, tenant_id: uuid.UUID) -> dict[str, Any]:
    row = load(conn, tenant_id)
    env, src = env_values(), sources(row)
    gaps = missing(row)

    def value(f: str) -> str | None:
        return env[f] or (getattr(row, f) if row is not None else None)

    return {
        "owner_email": row.owner_email if row else None,
        "auto_send": bool(row and row.auto_send),
        "company_name": company_name(conn, tenant_id, row),
        "emailjs": {
            "service_id": value("emailjs_service_id"),
            "template_id": value("emailjs_template_id"),
            "public_key": value("emailjs_public_key"),  # public by design; the private key is never returned
            "private_key_set": src["emailjs_private_key"] is not None,
            "max_request_kb": max_request_kb(row),
            "sources": src,
        },
        "email_ready": not gaps,
        "missing": ([] if row is not None and row.owner_email else ["owner_email"]) + gaps,
        "encryption_ready": crypto.configured(),
        "version": row.version if row else 0,
        "updated_at": row.updated_at.isoformat() if row else None,
    }


def emailjs_config(conn: Connection, tenant_id: uuid.UUID) -> Config | None:
    """The EmailJS account to send with (environment first, then settings), or None while anything is missing."""
    row = load(conn, tenant_id)
    if missing(row):
        return None
    env = env_values()
    if env["emailjs_private_key"]:
        private = env["emailjs_private_key"].get_secret_value().strip()
    else:
        private = crypto.decrypt(tenant_id, row.emailjs_private_key, row.emailjs_key_id)["private_key"]
    ids = [env[f] or getattr(row, f) for f in TEXT_FIELDS]
    return Config(ids[0], ids[1], ids[2], private, max_request_kb(row))


def update(conn: Connection, principal: Principal, body: dict[str, Any], expected_version: int) -> dict[str, Any]:
    row = load(conn, principal.tenant_id)
    if (row.version if row else 0) != expected_version:
        raise precondition_failed(row.version if row else 0)
    env = env_values()
    problems: list[Issue] = []
    values: dict[str, Any] = {}
    if "owner_email" in body:
        email = (body["owner_email"] or "").strip()
        if email and not EMAIL.match(email):
            problems.append(Issue("INVALID_EMAIL", f'"{email}" is not a valid email address.', "owner_email"))
        values["owner_email"] = email.lower() or None
    if "company_name" in body:
        values["company_name"] = (body["company_name"] or "").strip()[:200] or None
    for f in TEXT_FIELDS:
        if f not in body:
            continue
        given = (body[f] or "").strip() or None
        if env[f]:  # set in the server environment: the page shows it read-only
            if given not in (None, env[f]):
                problems.append(
                    Issue(
                        "SET_IN_ENVIRONMENT",
                        "This value comes from the server environment (.env); "
                        "change it there and restart the API and worker.",
                        f,
                    )
                )
            continue
        values[f] = given
    if "max_request_kb" in body and body["max_request_kb"] is not None:
        values["max_request_kb"] = int(body["max_request_kb"])
    key = (body.get("emailjs_private_key") or "").strip()
    if key and env["emailjs_private_key"]:
        problems.append(
            Issue(
                "SET_IN_ENVIRONMENT", "The private key comes from the server environment (.env).", "emailjs_private_key"
            )
        )
    elif body.get("clear_private_key"):
        values["emailjs_private_key"], values["emailjs_key_id"] = None, None
    elif key:
        if not crypto.configured():
            raise ApiError(
                409,
                "SECRETS_UNAVAILABLE",
                "INTEGRATION_KEYS is not set on the server, so the private "
                "key cannot be stored safely. Put EMAILJS_PRIVATE_KEY in the server .env instead.",
            )
        values["emailjs_private_key"], values["emailjs_key_id"] = crypto.encrypt(
            principal.tenant_id, {"private_key": key}
        )
    if "auto_send" in body:
        values["auto_send"] = bool(body["auto_send"])
    merged = {**(row._mapping if row else {}), **values}
    if merged.get("auto_send"):
        if not merged.get("owner_email"):
            problems.append(
                Issue(
                    "OWNER_EMAIL_REQUIRED", "Enter the owner's email before turning on automatic reports.", "auto_send"
                )
            )
        has_key = env["emailjs_private_key"] or merged.get("emailjs_private_key")
        if any(not (env[f] or merged.get(f)) for f in TEXT_FIELDS) or not has_key:
            problems.append(
                Issue(
                    "EMAIL_NOT_CONFIGURED",
                    "Complete the EmailJS settings before turning on automatic reports.",
                    "auto_send",
                )
            )
    if problems:
        raise ApiError(422, "VALIDATION_FAILED", f"{len(problems)} problem(s) must be fixed.", problems)
    stmt = insert(s).values(tenant_id=principal.tenant_id, updated_by=principal.membership_id, **values)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["tenant_id"],
            set_={**values, "updated_by": principal.membership_id, "version": s.c.version + 1},
        )
    )
    changed = sorted(k for k in values if k not in ("emailjs_key_id",))  # names only, never values
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="OWNER_REPORT_SETTINGS_CHANGED",
        object_type="tenant",
        object_id=principal.tenant_id,
        after={"fields": changed},
    )
    return view(conn, principal.tenant_id)
