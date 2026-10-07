"""Periodic ingestion housekeeping, run by the worker process."""

import logging

from sqlalchemy import func, select, update

from app.db import tables as t
from app.db.engine import auth_tx, tenant_tx
from app.storage.objects import get_storage

log = logging.getLogger("workers.maintenance")


def expire_uploads() -> int:
    """Uploads that never received their bytes expire after 24 hours (spec §3); orphans are removed."""
    with auth_tx() as conn:
        tenants = list(conn.execute(select(t.tenant.c.id)).scalars())
    keys: list[str] = []
    for tenant_id in tenants:
        with tenant_tx(tenant_id) as conn:
            keys += (
                conn.execute(
                    update(t.upload)
                    .where(t.upload.c.state == "UPLOADING", t.upload.c.expires_at < func.now())
                    .values(state="EXPIRED", version=t.upload.c.version + 1)
                    .returning(t.upload.c.object_key)
                )
                .scalars()
                .all()
            )
    for key in keys:
        try:
            get_storage().delete(key)
        except Exception:  # noqa: BLE001 - retried on the next sweep via retention
            log.warning("could not delete expired upload object")
    return len(keys)
