"""export.render (object = export): snapshot -> typed XLSX in object storage (FR14).

Renders only from the immutable snapshot stored with the export. The object key is deterministic, so a
re-run after a crash overwrites the same object; the export row becomes READY only once, with its checksum.
A render failure leaves no downloadable file.
"""

import hashlib

from sqlalchemy import func, select, update

from app.db import tables as t
from app.exports import xlsx
from app.storage.objects import get_storage, object_key_for
from workers.registry import Outcome, handler
from workers.runtime import JobContext

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@handler("export.render")
def render_export(ctx: JobContext) -> Outcome:
    with ctx.transaction() as conn:
        row = conn.execute(select(t.export).where(t.export.c.id == ctx.claim.object_id)).one()
        if row.state == "READY":
            return Outcome(result={"skipped": "READY"})
        conn.execute(update(t.export).where(t.export.c.id == row.id).values(state="GENERATING"))

    data = xlsx.render(
        {
            "id": str(row.id),
            "filter": row.filter_json,
            "rows": row.snapshot_json,
            "metrics": row.metrics_json,
            "data_version": row.data_version,
            "timezone": row.timezone,
            "row_count": row.row_count,
            "created_at": xlsx.created_at_text(row.created_at),
        }
    )
    key = object_key_for("exports", row.tenant_id, row.id, ".xlsx")
    get_storage().put_bytes(key, data, XLSX)
    digest = hashlib.sha256(data).hexdigest()

    with ctx.transaction() as conn:
        conn.execute(
            update(t.export)
            .where(t.export.c.id == row.id)
            .values(state="READY", file_key=key, sha256=digest, bytes=len(data), finished_at=func.now())
        )
    return Outcome(result={"rows": row.row_count, "bytes": len(data), "sha256": digest})
