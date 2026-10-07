"""Ingestion commands and views used by the API (FR02, FR21, FR27; API operations 4–9, 15).

Access rules (spec §2): an Uploader sees and acts on their own batches; a Reviewer sees batches of
their granted departments. Both require a current grant for the batch's department, so removing a
grant removes access on the next request. Inaccessible objects are reported as not found.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, and_, func, select, update

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, conflict, forbidden, not_found, validation_failed
from app.db import tables as t
from app.domain.enums import Role
from app.ingestion import limits
from app.jobs import ledger
from app.storage.objects import get_storage, object_key_for

UPLOAD_ROLES = (Role.UPLOADER, Role.REVIEWER)


# --- access ----------------------------------------------------------------------------------


def can_access_batch(principal: Principal, batch: Any) -> bool:
    if not principal.can_access_department(batch.department_id):
        return False
    return batch.owner_id == principal.membership_id or principal.has_any(Role.REVIEWER)


def load_batch(conn: Connection, principal: Principal, batch_id: uuid.UUID, lock: bool = False) -> Any:
    q = select(t.batch).where(t.batch.c.id == batch_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None or not can_access_batch(principal, row):
        raise not_found()
    return row


def load_upload(conn: Connection, principal: Principal, upload_id: uuid.UUID, lock: bool = False) -> tuple[Any, Any]:
    q = select(t.upload).where(t.upload.c.id == upload_id)
    up = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if up is None:
        raise not_found()
    return up, load_batch(conn, principal, up.batch_id)


# --- commands --------------------------------------------------------------------------------


def create_batch(
    conn: Connection, principal: Principal, department_id: uuid.UUID, files: list[limits.DeclaredFile]
) -> dict[str, Any]:
    if not principal.has_any(*UPLOAD_ROLES):
        raise forbidden()
    if not principal.can_access_department(department_id):
        raise forbidden("You cannot upload for this department.")
    issues = limits.validate_manifest(files)
    if issues:
        raise validation_failed(issues)

    batch_id = uuid.uuid4()
    conn.execute(t.batch.insert().values(
        id=batch_id, tenant_id=principal.tenant_id, department_id=department_id, owner_id=principal.membership_id,
        file_count=len(files), total_bytes=sum(f.bytes for f in files)))  # fmt: skip
    expires = datetime.now(UTC) + timedelta(hours=limits.UPLOAD_TTL_HOURS)
    for slot, f in enumerate(files, start=1):
        upload_id = uuid.uuid4()
        name = limits.display_name(f.name)
        conn.execute(t.upload.insert().values(
            id=upload_id, tenant_id=principal.tenant_id, batch_id=batch_id, slot_no=slot, display_name=name,
            extension=limits.extension_of(name), declared_mime=(f.mime or "")[:100], declared_bytes=f.bytes,
            declared_sha256=bytes.fromhex(f.sha256),
            object_key=object_key_for("quarantine", principal.tenant_id, upload_id), expires_at=expires))  # fmt: skip
    audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="BATCH_CREATED",
                 object_type="batch", object_id=batch_id,
                 after={"files": len(files), "department_id": str(department_id)})  # fmt: skip
    return {"batch_id": str(batch_id)}


def upload_slots(conn: Connection, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    """Fresh signed PUT URLs for uploads still waiting for bytes. Never stored or logged."""
    storage = get_storage()
    now = datetime.now(UTC)
    rows = conn.execute(select(t.upload).where(t.upload.c.batch_id == batch_id).order_by(t.upload.c.slot_no)).all()
    slots = []
    for up in rows:
        slot: dict[str, Any] = {"id": str(up.id), "slot_no": up.slot_no, "name": up.display_name,
                                "state": up.state}  # fmt: skip
        if up.state == "UPLOADING" and up.expires_at > now:
            checksum = _b64(up.declared_sha256)
            content_type = limits.CONTENT_TYPES[up.extension]
            slot |= {
                "put_url": storage.presign_put(up.object_key, content_type, checksum),
                "headers": {"Content-Type": content_type, "x-amz-checksum-sha256": checksum},
                "url_expires_at": (now + timedelta(seconds=storage_ttl())).isoformat(),
            }
        slots.append(slot)
    return slots


def storage_ttl() -> int:
    from app.core.config import get_settings

    return get_settings().signed_url_ttl_seconds


def _b64(digest: bytes) -> str:
    import base64

    return base64.b64encode(bytes(digest)).decode()


def complete_upload(conn: Connection, principal: Principal, upload_id: uuid.UUID, sha256: str, nbytes: int,
                    stored_size: int | None) -> dict[str, Any]:  # fmt: skip
    """UPLOADING -> QUARANTINED and queue the scan. `stored_size` comes from a storage HEAD done by the
    caller before this transaction (no network calls while rows are locked)."""
    up, batch = load_upload(conn, principal, upload_id, lock=True)
    if batch.owner_id != principal.membership_id and not principal.has_any(Role.REVIEWER):
        raise not_found()
    if up.state != "UPLOADING":
        raise conflict("INVALID_STATE", f"This file is already {up.state.lower()}.")
    if up.expires_at <= datetime.now(UTC):
        raise conflict("UPLOAD_EXPIRED", "The upload window expired. Start a new upload.")
    if sha256.lower() != bytes(up.declared_sha256).hex() or nbytes != up.declared_bytes:
        raise ApiError(422, "CHECKSUM_MISMATCH", "The file does not match what was declared. Upload it again.")
    if stored_size is None:
        raise conflict("UPLOAD_MISSING", "The file has not arrived yet. Upload it again.")
    if stored_size != up.declared_bytes:
        raise ApiError(422, "CHECKSUM_MISMATCH", "The stored file size does not match. Upload it again.")

    conn.execute(update(t.upload).where(t.upload.c.id == up.id).values(
        state="QUARANTINED", completed_at=func.now(), version=t.upload.c.version + 1))  # fmt: skip
    job_id = ledger.create_job(conn, tenant_id=principal.tenant_id, kind="upload.scan", object_id=up.id,
                               created_by=principal.membership_id)  # fmt: skip
    audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="UPLOAD_COMPLETED",
                 object_type="upload", object_id=up.id)  # fmt: skip
    return job_view(conn.execute(select(t.job).where(t.job.c.id == job_id)).one())


# --- views -----------------------------------------------------------------------------------


def job_view(job: Any) -> dict[str, Any]:
    error = None
    if job.error_code:
        error = {"field": None, "code": job.error_code, "message": job.error_message or "", "severity": "error"}
    return {
        "id": str(job.id), "kind": job.kind, "object_id": str(job.object_id), "generation": job.generation,
        "state": job.state, "processed": job.processed or 0, "total": job.total, "error": error,
        "attempt": job.attempts, "max_attempts": job.max_attempts,
        "retryable": bool(job.retryable) and job.state in ("FAILED", "PARTIAL"),
        "cancel_requested": job.cancel_requested,
        "next_attempt_at": job.next_attempt_at.isoformat() if job.state == "RETRY_WAIT" else None,
        "result": job.result, "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }  # fmt: skip


def _latest_jobs(conn: Connection, upload_ids: list[uuid.UUID]) -> dict[tuple[uuid.UUID, str], Any]:
    if not upload_ids:
        return {}
    j = t.job
    latest = (
        select(j.c.object_id, j.c.kind, func.max(j.c.generation).label("gen"))
        .where(j.c.object_id.in_(upload_ids), j.c.kind.in_(("upload.scan", "upload.parse", "upload.extract")))
        .group_by(j.c.object_id, j.c.kind)
        .subquery()
    )
    rows = conn.execute(
        select(j).join(
            latest, and_(j.c.object_id == latest.c.object_id, j.c.kind == latest.c.kind, j.c.generation == latest.c.gen)
        )  # fmt: skip
    ).all()
    return {(r.object_id, r.kind): r for r in rows}


def batch_view(conn: Connection, principal: Principal, batch_id: uuid.UUID) -> dict[str, Any]:
    batch = load_batch(conn, principal, batch_id)
    uploads = conn.execute(select(t.upload).where(t.upload.c.batch_id == batch_id).order_by(t.upload.c.slot_no)).all()
    ids = [u.id for u in uploads]
    jobs = _latest_jobs(conn, ids)
    pages: dict[uuid.UUID, dict[str, list[int]]] = {i: {"succeeded": [], "failed": []} for i in ids}
    if ids:
        for r in conn.execute(
            select(t.page_result.c.upload_id, t.page_result.c.page_no, t.page_result.c.state).where(
                t.page_result.c.upload_id.in_(ids), t.page_result.c.pipeline_version == limits.PIPELINE_VERSION
            )
        ):
            pages[r.upload_id]["succeeded" if r.state == "SUCCEEDED" else "failed"].append(r.page_no)
    dept = conn.execute(select(t.department.c.code, t.department.c.name)
                        .where(t.department.c.id == batch.department_id)).one()  # fmt: skip
    c = t.candidate
    to_review = dict(conn.execute(select(c.c.upload_id, func.count()).where(
        c.c.batch_id == batch.id, c.c.state == "NEEDS_REVIEW").group_by(c.c.upload_id)).all())  # fmt: skip

    files = []
    for u in uploads:
        p = pages[u.id]
        scan, parse = jobs.get((u.id, "upload.scan")), jobs.get((u.id, "upload.parse"))
        extract = jobs.get((u.id, "upload.extract"))
        files.append({
            "id": str(u.id), "slot_no": u.slot_no, "name": u.display_name, "extension": u.extension,
            "bytes": u.declared_bytes, "state": u.state, "scan_status": u.scan_status, "scanner": u.scanner,
            "detected_type": u.detected_type, "reject": {"code": u.reject_code, "message": u.reject_message}
            if u.reject_code else None,
            "duplicate_of": str(u.duplicate_of) if u.duplicate_of else None,
            "pages": {"total": u.page_count, "processed": len(p["succeeded"]) + len(p["failed"]),
                      "succeeded": sorted(p["succeeded"]), "failed": sorted(p["failed"])},
            "jobs": {"scan": job_view(scan) if scan else None, "parse": job_view(parse) if parse else None,
                     "extract": job_view(extract) if extract else None},
            "to_review": to_review.get(u.id, 0),
            "expires_at": u.expires_at.isoformat(),
        })  # fmt: skip
    return {
        "id": str(batch.id), "department": {"id": str(batch.department_id), "code": dept.code, "name": dept.name},
        "owner_id": str(batch.owner_id), "created_at": batch.created_at.isoformat(), "files": files,
        "summary": summarize(files),
    }  # fmt: skip


def summarize(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts only; the UI never shows invented percentages (spec U2)."""

    def parse_state(f):
        job = f["jobs"]["parse"]
        return job["state"] if job else None

    def extracting(f):
        job = f["jobs"].get("extract")
        return bool(job) and job["state"] in ("QUEUED", "RUNNING", "RETRY_WAIT")

    return {
        "files": len(files),
        "rejected": sum(f["state"] == "REJECTED" for f in files),
        "waiting_for_upload": sum(f["state"] == "UPLOADING" for f in files),
        "scanning": sum(f["state"] == "QUARANTINED" for f in files),
        "parsed": sum(parse_state(f) == "SUCCEEDED" for f in files),
        "partial": sum(parse_state(f) in ("PARTIAL", "FAILED") for f in files),
        "pages_processed": sum(f["pages"]["processed"] for f in files),
        "pages_total": sum(f["pages"]["total"] or 0 for f in files),
        "to_review": sum(f.get("to_review", 0) for f in files),
        "in_progress": any(
            f["state"] in ("QUARANTINED",)
            or (f["state"] == "READY" and parse_state(f) in (None, "QUEUED", "RUNNING", "RETRY_WAIT"))
            or (f["state"] == "READY" and extracting(f))
            for f in files
        ),
    }
