"""ROI measurement (addendum A10).

The "before" figures are measured by the company during a baseline week and entered by an administrator; the
system never assumes them. Pilot figures are computed from what actually happened in the chosen period.
Savings are shown only when both exist and the pilot sample is large enough; otherwise the reason is stated.
"""

import statistics
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, func, select

from app.db import tables as t

MIN_REPORTS, MIN_DAYS = 10, 14


def _pct(n: int, d: int) -> float | None:
    return None if not d else round(100.0 * n / d, 1)


def _median_minutes(values: list[float]) -> float | None:
    return None if not values else round(statistics.median(values) / 60.0, 2)


def measure(conn: Connection, tenant_id: Any, date_from: date, date_to: date) -> dict[str, Any]:
    tz = ZoneInfo(conn.execute(select(t.tenant.c.timezone).where(t.tenant.c.id == tenant_id)).scalar_one())
    start = datetime.combine(date_from, time.min, tz)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tz)

    def within(col):
        return (col >= start) & (col < end)

    c, x = t.candidate, t.extraction
    decided = conn.execute(
        select(c.c.id, c.c.state, x.c.extractor)
        .join(x, x.c.id == c.c.extraction_id)
        .where(within(c.c.decided_at), c.c.state.in_(("APPROVED", "REJECTED")))
    ).all()
    approved = [r for r in decided if r.state == "APPROVED"]
    extracted = [r for r in approved if r.extractor not in ("manual", "none")]
    changed = (
        set(
            conn.execute(
                select(t.candidate_change.c.candidate_id).where(
                    t.candidate_change.c.candidate_id.in_([r.id for r in approved])
                )
            ).scalars()
        )
        if approved
        else set()
    )

    r = t.report
    report_secs = [
        row.s
        for row in conn.execute(
            select(func.extract("epoch", r.c.ready_at - r.c.created_at).label("s")).where(
                within(r.c.created_at), r.c.state == "READY"
            )
        ).all()
    ]
    em, d = t.email_message, t.email_draft
    email_secs = [
        row.s
        for row in conn.execute(
            select(func.extract("epoch", em.c.created_at - d.c.created_at).label("s"))
            .join(d, d.c.id == em.c.draft_id)
            .where(within(em.c.created_at))
        ).all()
    ]
    u = t.upload
    extract_secs = [
        row.s
        for row in conn.execute(
            select(func.extract("epoch", func.min(x.c.created_at) - u.c.completed_at).label("s"))
            .join(x, x.c.upload_id == u.c.id)
            .where(within(u.c.completed_at))
            .group_by(u.c.id, u.c.completed_at)
        ).all()
        if row.s is not None
    ]
    records = conn.execute(select(func.count()).where(within(t.production_record.c.created_at))).scalar_one()
    exceptions = conn.execute(select(func.count()).where(within(t.exception_item.c.first_seen_at))).scalar_one()
    followups = conn.execute(select(func.count()).where(within(t.notification.c.created_at))).scalar_one()
    weeks = max(1.0, ((date_to - date_from).days + 1) / 7.0)

    pilot = {
        "reports_ready": len(report_secs),
        "report_generation_minutes_median": _median_minutes([float(v) for v in report_secs]),
        "email_preparation_minutes_median": _median_minutes([float(v) for v in email_secs]),
        "extraction_minutes_median": _median_minutes([float(v) for v in extract_secs]),
        "entries_approved": len(approved),
        "data_entry_rows_avoided": len(extracted),
        "correction_rate_pct": _pct(len(changed), len(approved)),
        "exception_rate_pct": _pct(exceptions, records),
        "followups_per_week": round(followups / weeks, 2),
    }
    base = conn.execute(select(t.roi_baseline).where(t.roi_baseline.c.tenant_id == tenant_id)).one_or_none()
    baseline = (
        None
        if base is None
        else {
            k: (float(getattr(base, k)) if isinstance(getattr(base, k), Decimal) else getattr(base, k))
            for k in (
                "manual_minutes_per_report",
                "manual_minutes_per_entry",
                "manual_minutes_per_email",
                "manual_followups_per_week",
                "manual_correction_rate_pct",
            )
        }
        | {
            "measured_from": base.measured_from.isoformat() if base.measured_from else None,
            "measured_to": base.measured_to.isoformat() if base.measured_to else None,
            "notes": base.notes,
            "version": base.version,
        }
    )

    reasons = []
    if baseline is None or baseline["manual_minutes_per_report"] is None:
        reasons.append("No measured baseline has been entered yet.")
    if pilot["reports_ready"] < MIN_REPORTS:
        reasons.append(f"Fewer than {MIN_REPORTS} reports in the period ({pilot['reports_ready']}).")
    if (date_to - date_from).days + 1 < MIN_DAYS:
        reasons.append(f"The period is shorter than {MIN_DAYS} days.")
    comparison = None
    if not reasons and baseline:
        auto = (pilot["report_generation_minutes_median"] or 0) + (pilot["email_preparation_minutes_median"] or 0)
        per_report = round(baseline["manual_minutes_per_report"] - auto, 2)
        entries = (baseline["manual_minutes_per_entry"] or 0) * pilot["data_entry_rows_avoided"]
        comparison = {
            "minutes_saved_per_report": per_report,
            "report_minutes_saved_in_period": round(per_report * pilot["reports_ready"], 1),
            "entry_minutes_saved_in_period": round(entries, 1),
            "note": "Measured medians against the entered baseline; manual review time is not included on either side.",
        }
    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "pilot": pilot,
        "baseline": baseline,
        "comparison": comparison,
        "comparison_unavailable": reasons,
    }
