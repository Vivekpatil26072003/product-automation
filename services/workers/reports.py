"""report.render (object = report) and reports.invalidate (object = record); FR16-FR18.

report.render reads only the frozen snapshot. The summary is written (template, or a checked AI paraphrase),
the PDF rendered, stored under a deterministic key and only then is the report READY with its checksum, in
one fenced transaction. A render failure marks the report FAILED with no downloadable file; retry renders the
same snapshot again (POST /reports/{id}/retry).

reports.invalidate runs for every production_record.changed event and marks reports outdated when the
approved records matching their filter no longer equal their items.
"""

import hashlib
import logging

from sqlalchemy import func, select, update

from app.db import tables as t
from app.reports import narrative, pdf
from app.reports import service as reports
from app.storage.objects import get_storage, object_key_for
from workers.registry import Outcome, handler, route
from workers.runtime import JobContext

log = logging.getLogger("workers.reports")
PDF = "application/pdf"

route("production_record.changed", "reports.invalidate", "record_id", coalesce=True)


@handler(reports.RENDER_KIND)
def render_report(ctx: JobContext) -> Outcome:
    with ctx.transaction() as conn:
        row = conn.execute(select(t.report).where(t.report.c.id == ctx.claim.object_id)).one()
        if row.state == "READY":
            return Outcome(result={"skipped": "READY"})
        conn.execute(
            update(t.report)
            .where(t.report.c.id == row.id)
            .values(state="GENERATING", error_code=None, error_message=None)
        )

    summary = narrative.summarize(row.facts_json, narrative.default_writer())
    try:
        with ctx.transaction() as conn:
            data_in = reports.render_input(conn, row, summary.sentences, summary.source)
        data = pdf.render(data_in)
    except Exception as exc:  # noqa: BLE001 - any renderer fault: FAILED, nothing downloadable
        log.exception("report render failed report=%s", row.id)
        with ctx.transaction() as conn:
            conn.execute(
                update(t.report)
                .where(t.report.c.id == row.id)
                .values(
                    state="FAILED",
                    error_code="RENDER_FAILED",
                    error_message="The PDF could not be created. Retry uses the same snapshot.",
                )
            )
        return Outcome(state="FAILED", error_code="RENDER_FAILED", error_message=type(exc).__name__)

    key = object_key_for("reports", row.tenant_id, row.id, ".pdf")
    get_storage().put_bytes(key, data, PDF)
    digest = hashlib.sha256(data).hexdigest()
    with ctx.transaction() as conn:
        conn.execute(
            update(t.report)
            .where(t.report.c.id == row.id)
            .values(
                state="READY",
                file_key=key,
                sha256=digest,
                bytes=len(data),
                ready_at=func.now(),
                narrative_json={"sentences": summary.sentences},
                narrative_source=summary.source,
                narrative_model=summary.model,
                narrative_prompt_hash=summary.prompt_hash,
                narrative_fallback_reason=summary.fallback_reason,
            )
        )
        # Records may have changed while this rendered: flag it now rather than on the next change.
        current = conn.execute(select(t.report).where(t.report.c.id == row.id)).one()
        if not reports.is_current(conn, current):
            reports.mark_outdated(conn, current, "RECORDS_CHANGED")
    return Outcome(result={"bytes": len(data), "sha256": digest, "summary": summary.source})


@handler("reports.invalidate")
def invalidate(ctx: JobContext) -> Outcome:
    with ctx.transaction() as conn:
        n = reports.invalidate_for_record(conn, ctx.claim.object_id)
    return Outcome(result={"outdated": n})
