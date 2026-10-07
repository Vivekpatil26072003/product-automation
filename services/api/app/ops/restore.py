"""Operator procedures (FR25, FR29): log retention and restore verification. Run with the owner connection.

verify(url): checks a restored database against object storage and reports what must be reconciled before sends
are re-enabled. It reads only; it never changes the restored database or the object store.
"""

import hashlib
import time
from typing import Any

from sqlalchemy import Engine, func, select, text

from app.db import tables as t
from app.storage.objects import get_storage


def purge_logs(engine: Engine, days: int) -> dict[str, int]:
    """Operational records older than the log retention (default 30 days). No business data is touched."""
    with engine.begin() as conn:
        interval = text(f"now() - interval '{int(days)} days'")
        out = {
            "security_event": conn.execute(
                t.security_event.delete().where(t.security_event.c.created_at < interval)
            ).rowcount,
            "auth_session": conn.execute(
                t.auth_session.delete().where(t.auth_session.c.expires_at < interval)
            ).rowcount,
            "oidc_login_state": conn.execute(
                t.oidc_login_state.delete().where(t.oidc_login_state.c.expires_at < text("now() - interval '1 day'"))
            ).rowcount,
            "idempotency_record": conn.execute(
                t.idempotency_record.delete().where(t.idempotency_record.c.expires_at < text("now()"))
            ).rowcount,
        }
    return out


def verify(engine: Engine) -> dict[str, Any]:
    started = time.monotonic()
    storage = get_storage()
    with engine.connect() as conn:
        counts = {
            name: conn.execute(select(func.count()).select_from(table)).scalar_one()
            for name, table in (
                ("tenants", t.tenant),
                ("production_records", t.production_record),
                ("revisions", t.record_revision),
                ("audit_events", t.audit_event),
                ("reports", t.report),
                ("emails", t.email_message),
                ("purged_objects", t.retention_event),
            )
        }
        reports = conn.execute(
            select(t.report.c.id, t.report.c.file_key, t.report.c.sha256).where(
                t.report.c.state == "READY", t.report.c.file_purged_at.is_(None)
            )
        ).all()
        manifest = [
            k
            for keys in conn.execute(
                select(t.retention_event.c.object_keys).where(t.retention_event.c.action == "PURGED")
            ).scalars()
            for k in keys
        ]
        pending_sends = conn.execute(
            select(t.email_message.c.state, func.count())
            .where(t.email_message.c.state.in_(("QUEUED", "SENDING", "UNKNOWN")))
            .group_by(t.email_message.c.state)
        ).all()
        active_auto = conn.execute(
            select(func.count()).where(t.schedule.c.active, t.schedule.c.approval_state == "APPROVED")
        ).scalar_one()
    mismatched, missing = [], []
    for r in reports:
        try:
            data = storage.get_bytes(r.file_key, 50_000_000)
        except FileNotFoundError:
            missing.append(str(r.id))
            continue
        if hashlib.sha256(data).hexdigest() != r.sha256:
            mismatched.append(str(r.id))
    resurrected = [k for k in manifest if storage.head(k) is not None]
    return {
        "counts": counts,
        "report_files": {"checked": len(reports), "missing": missing, "checksum_mismatch": mismatched},
        "deletion_manifest": {"keys": len(manifest), "present_in_storage": len(resurrected)},
        "sends_to_reconcile": {s: n for s, n in pending_sends},
        "approved_auto_send_schedules": active_auto,
        "ok": not missing and not mismatched and not resurrected,
        "seconds": round(time.monotonic() - started, 2),
    }
