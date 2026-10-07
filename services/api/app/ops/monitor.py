"""Operational metrics and alert conditions (FR29, spec §13 "Backup and incident recovery").

Alert thresholds from the specification:
  oldest due job waiting > 10 minutes            QUEUE_STALLED
  job failure rate > 5 % over 15 min, >= 20 jobs   JOB_FAILURE_RATE
  oldest pending sync > 1 hour                    SYNC_LAGGING
  any email with an UNKNOWN outcome               EMAIL_UNKNOWN (immediately)
  connection needing reconnection                  CREDENTIAL_FAILURE
Metrics are counts and ages only: no document content, names or identifiers leave this module.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, func, select

from app.db import tables as t
from app.db.engine import dispatcher_tx, tenant_tx

j, o = t.job, t.outbox


@dataclass(frozen=True)
class Alert:
    code: str
    severity: str
    message: str
    value: float


def _age(now: datetime, then: datetime | None) -> float:
    return 0.0 if then is None else max(0.0, (now - then).total_seconds())


def queue_metrics(conn: Connection) -> dict[str, float]:
    """Dispatcher scope: job and outbox health across companies (no business data)."""
    now = conn.execute(select(func.now())).scalar_one()
    since = now - timedelta(minutes=15)
    oldest_due = conn.execute(
        select(func.min(j.c.next_attempt_at)).where(j.c.state.in_(("QUEUED", "RETRY_WAIT")), j.c.next_attempt_at <= now)
    ).scalar()
    finished = conn.execute(select(j.c.state, func.count()).where(j.c.finished_at >= since).group_by(j.c.state)).all()
    total = sum(n for _, n in finished)
    failed = sum(n for s, n in finished if s == "FAILED")
    oldest_outbox = conn.execute(select(func.min(o.c.created_at)).where(o.c.dispatched_at.is_(None))).scalar()
    return {
        "jobs_oldest_due_seconds": _age(now, oldest_due),
        "jobs_due": float(
            conn.execute(
                select(func.count()).where(j.c.state.in_(("QUEUED", "RETRY_WAIT")), j.c.next_attempt_at <= now)
            ).scalar_one()
        ),
        "jobs_running": float(conn.execute(select(func.count()).where(j.c.state == "RUNNING")).scalar_one()),
        "jobs_finished_15m": float(total),
        "jobs_failed_15m": float(failed),
        "jobs_failure_ratio_15m": failed / total if total else 0.0,
        "outbox_pending": float(conn.execute(select(func.count()).where(o.c.dispatched_at.is_(None))).scalar_one()),
        "outbox_oldest_pending_seconds": _age(now, oldest_outbox),
    }


def tenant_metrics(conn: Connection) -> dict[str, float]:
    """Tenant scope: delivery and integration health for one company."""
    now = conn.execute(select(func.now())).scalar_one()
    oldest_sync = conn.execute(
        select(func.min(t.record_sync.c.updated_at)).where(t.record_sync.c.state == "PENDING")
    ).scalar()
    return {
        "email_unknown": float(
            conn.execute(select(func.count()).where(t.email_message.c.state == "UNKNOWN")).scalar_one()
        ),
        "email_queued": float(
            conn.execute(select(func.count()).where(t.email_message.c.state.in_(("QUEUED", "SENDING")))).scalar_one()
        ),
        "sync_oldest_pending_seconds": _age(now, oldest_sync),
        "connections_reconnect_required": float(
            conn.execute(
                select(func.count()).where(t.integration_connection.c.state == "RECONNECT_REQUIRED")
            ).scalar_one()
        ),
        "exceptions_critical_open": float(
            conn.execute(
                select(func.count()).where(
                    t.exception_item.c.severity == "CRITICAL", t.exception_item.c.status.in_(("OPEN", "ACKNOWLEDGED"))
                )
            ).scalar_one()
        ),
    }


def alerts(queue: dict[str, float], tenant: dict[str, float]) -> list[Alert]:
    out: list[Alert] = []
    if queue["jobs_oldest_due_seconds"] > 600:
        out.append(
            Alert(
                "QUEUE_STALLED",
                "critical",
                "A due job has waited more than 10 minutes.",
                queue["jobs_oldest_due_seconds"],
            )
        )
    if queue["jobs_finished_15m"] >= 20 and queue["jobs_failure_ratio_15m"] > 0.05:
        out.append(
            Alert(
                "JOB_FAILURE_RATE",
                "warning",
                "More than 5% of jobs failed in the last 15 minutes.",
                queue["jobs_failure_ratio_15m"],
            )
        )
    if tenant.get("sync_oldest_pending_seconds", 0) > 3600:
        out.append(
            Alert(
                "SYNC_LAGGING",
                "warning",
                "A record has waited more than an hour to sync.",
                tenant["sync_oldest_pending_seconds"],
            )
        )
    if tenant.get("email_unknown", 0) > 0:
        out.append(
            Alert(
                "EMAIL_UNKNOWN",
                "critical",
                "An email outcome is unknown; reconcile before any resend.",
                tenant["email_unknown"],
            )
        )
    if tenant.get("connections_reconnect_required", 0) > 0:
        out.append(
            Alert(
                "CREDENTIAL_FAILURE",
                "warning",
                "A connection needs its credentials renewed.",
                tenant["connections_reconnect_required"],
            )
        )
    return out


def collect() -> tuple[dict[str, float], dict[str, float]]:
    """All companies: queue metrics plus tenant metrics summed (for the infrastructure scrape)."""
    with dispatcher_tx() as conn:
        queue = queue_metrics(conn)
        tenants = conn.execute(select(t.tenant.c.id)).scalars().all()
    total: dict[str, float] = {}
    for tenant_id in tenants:
        with tenant_tx(tenant_id) as conn:
            for k, v in tenant_metrics(conn).items():
                total[k] = max(total.get(k, 0.0), v) if k.endswith("_seconds") else total.get(k, 0.0) + v
    return queue, total


def prometheus(queue: dict[str, float], tenant: dict[str, float], extra: dict[str, Any] | None = None) -> str:
    lines = []
    for name, value in sorted({**queue, **tenant, **(extra or {})}.items()):
        metric = f"prodauto_{name}"
        lines += [f"# TYPE {metric} gauge", f"{metric} {float(value):.3f}"]
    for a in alerts(queue, tenant):
        lines.append(f'prodauto_alert{{code="{a.code}",severity="{a.severity}"}} 1')
    return "\n".join(lines) + "\n"
