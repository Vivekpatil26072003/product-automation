"""API operations 4–9 and 15: batches, uploads, jobs and source access (FR02, FR04, FR21, FR27)."""

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import and_, func, or_, select

from app.api.deps import current_principal, idempotency_key, require
from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, conflict, not_found
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import tenant_tx
from app.domain.enums import Role
from app.ingestion import limits, service
from app.jobs import ledger
from app.storage.objects import get_storage

router = APIRouter(tags=["ingestion"])
Sha256 = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[0-9a-f]{64}$")]


class FileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=1000)
    bytes: int = Field(ge=0)
    sha256: str = Field(max_length=64)
    mime: str = Field(default="", max_length=200)


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    department_id: uuid.UUID
    files: list[FileIn] = Field(max_length=100)


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: Sha256
    bytes: int = Field(ge=1)


class RetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failed_only: Literal[True] = True


@router.get("/uploads/limits", summary="Exact upload limits shown in the upload screen")
def upload_limits(_: Principal = Depends(current_principal)) -> dict:
    return {"data": limits.LIMITS_PUBLIC}


@router.post("/batches", status_code=201, summary="Create a batch and get signed upload slots")
def create_batch(
    body: BatchIn,
    principal: Principal = Depends(require(Role.UPLOADER, Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    files = [limits.DeclaredFile(f.name, f.bytes, f.sha256.lower(), f.mime) for f in body.files]
    with tenant_tx(principal.tenant_id) as conn:
        _, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id, route="POST /batches",
            key=key, payload=body.model_dump(mode="json"),
            effect=lambda: (201, service.create_batch(conn, principal, body.department_id, files)),
        )  # fmt: skip
        batch_id = uuid.UUID(out["batch_id"])
        # Signed URLs are generated per response and never stored with the idempotent replay.
        slots = service.upload_slots(conn, batch_id)
    return JSONResponse({"data": {"batch_id": str(batch_id), "uploads": slots, "limits": limits.LIMITS_PUBLIC}},
                        status_code=201)  # fmt: skip


@router.get("/batches", summary="Batches visible to the caller, newest first")
def list_batches(
    cursor: str | None = Query(default=None, max_length=200),
    size: int = Query(default=25),
    principal: Principal = Depends(require(Role.UPLOADER, Role.REVIEWER)),
) -> dict:
    if size not in (25, 50, 100):
        raise ApiError(400, "BAD_PAGE_SIZE", "Page size must be 25, 50 or 100.")
    b = t.batch
    visible = b.c.department_id.in_(list(principal.department_ids))
    if not principal.has_any(Role.REVIEWER):
        visible = and_(visible, b.c.owner_id == principal.membership_id)
    q = select(b).where(visible).order_by(b.c.created_at.desc(), b.c.id.desc()).limit(size + 1)
    if cursor:
        try:
            c_at, c_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            c_at, c_id = datetime.fromisoformat(c_at), uuid.UUID(c_id)
        except (ValueError, TypeError) as exc:
            raise ApiError(400, "BAD_CURSOR", "The page cursor is invalid. Reload the list.") from exc
        q = q.where(or_(b.c.created_at < c_at, and_(b.c.created_at == c_at, b.c.id < c_id)))
    with tenant_tx(principal.tenant_id) as conn:
        rows = conn.execute(q).all()
        page = rows[:size]
        data = [service.batch_view(conn, principal, r.id) for r in page]
        total = conn.execute(select(func.count()).select_from(b).where(visible)).scalar_one()
    nxt = None
    if len(rows) > size:
        last = page[-1]
        nxt = base64.urlsafe_b64encode(json.dumps([last.created_at.isoformat(), str(last.id)]).encode()).decode()
    return {"data": data, "next_cursor": nxt, "total": total}


@router.get("/batches/{batch_id}", summary="Batch with per-file scan, parse and page status")
def get_batch(batch_id: uuid.UUID, principal: Principal = Depends(require(Role.UPLOADER, Role.REVIEWER))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": service.batch_view(conn, principal, batch_id)}


@router.get("/batches/{batch_id}/upload-slots", summary="Fresh signed URLs for files not yet uploaded")
def refresh_slots(batch_id: uuid.UUID, principal: Principal = Depends(require(Role.UPLOADER, Role.REVIEWER))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        batch = service.load_batch(conn, principal, batch_id)
        if batch.owner_id != principal.membership_id:
            raise not_found()
        return {"data": service.upload_slots(conn, batch_id)}


@router.post("/uploads/{upload_id}/complete", status_code=202, summary="Confirm upload; queue quarantine scan")
def complete_upload(
    upload_id: uuid.UUID,
    body: CompleteIn,
    principal: Principal = Depends(require(Role.UPLOADER, Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        up, _ = service.load_upload(conn, principal, upload_id)
        object_key = up.object_key
    info = get_storage().head(object_key)  # network call outside the locking transaction
    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id,
            route=f"POST /uploads/{upload_id}/complete", key=key, payload=body.model_dump(),
            effect=lambda: (202, {"data": service.complete_upload(
                conn, principal, upload_id, body.sha256, body.bytes, info.size if info else None)}),
        )  # fmt: skip
    return JSONResponse(out, status_code=status)


def _load_job(conn, principal: Principal, job_id: uuid.UUID, lock: bool = False):
    q = select(t.job).where(t.job.c.id == job_id)
    job = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if job is None:
        raise not_found()
    if job.kind.startswith("upload."):
        _, batch = service.load_upload(conn, principal, job.object_id)
        return job, batch
    if job.kind == "report.render":  # scope follows the report (Reviewer/Sender with every department)
        from app.reports.service import load_report

        load_report(conn, principal, job.object_id)
        return job, None
    if principal.has_any(Role.ADMIN):
        return job, None
    raise not_found()


@router.get("/jobs/{job_id}", summary="Job status")
def get_job(job_id: uuid.UUID, principal: Principal = Depends(current_principal)) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        job, _ = _load_job(conn, principal, job_id)
        return {"data": service.job_view(job)}


@router.post("/jobs/{job_id}/retry", status_code=202, summary="Retry a failed or partial job (failed pages only)")
def retry_job(
    job_id: uuid.UUID,
    body: RetryIn,
    principal: Principal = Depends(require(Role.UPLOADER, Role.REVIEWER, Role.ADMIN, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        job, _ = _load_job(conn, principal, job_id, lock=True)
        if job.kind.startswith("upload.") and not principal.has_any(Role.UPLOADER, Role.REVIEWER):
            raise not_found()
        newest = conn.execute(select(func.max(t.job.c.generation)).where(
            t.job.c.kind == job.kind, t.job.c.object_id == job.object_id)).scalar_one()  # fmt: skip
        if newest != job.generation:
            raise conflict("SUPERSEDED", "A newer attempt of this job exists.")
        if job.state in ("QUEUED", "RUNNING", "RETRY_WAIT"):
            raise conflict("JOB_RUNNING", "This job is still running.")
        if job.state not in ("FAILED", "PARTIAL") or not job.retryable:
            raise conflict("NOT_RETRYABLE", "This job cannot be retried. Upload a supported, unprotected file.")
        new_id = ledger.create_job(conn, tenant_id=principal.tenant_id, kind=job.kind, object_id=job.object_id,
                                   generation=job.generation + 1, created_by=principal.membership_id)  # fmt: skip
        audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="JOB_RETRIED",
                     object_type="job", object_id=new_id, after={"previous": str(job.id)})  # fmt: skip
        return 202, {"data": service.job_view(conn.execute(select(t.job).where(t.job.c.id == new_id)).one())}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id,
            route=f"POST /jobs/{job_id}/retry", key=key, payload=body.model_dump(), effect=effect,
        )  # fmt: skip
    return JSONResponse(out, status_code=status)


@router.post("/jobs/{job_id}/cancel", status_code=202, summary="Cooperatively cancel queued or running work")
def cancel_job(
    job_id: uuid.UUID,
    principal: Principal = Depends(require(mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    def effect() -> tuple[int, dict]:
        job, batch = _load_job(conn, principal, job_id, lock=True)
        owner = batch is not None and batch.owner_id == principal.membership_id
        if not (owner or principal.has_any(Role.ADMIN)):
            raise not_found()
        if job.state in ledger.TERMINAL:
            raise conflict("JOB_FINISHED", "This job already finished; completed results are kept.")
        ledger.request_cancel(conn, job_id)
        audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="JOB_CANCEL_REQUESTED",
                     object_type="job", object_id=job_id)  # fmt: skip
        return 202, {"data": service.job_view(conn.execute(select(t.job).where(t.job.c.id == job_id)).one())}

    with tenant_tx(principal.tenant_id) as conn:
        status, out = run_idempotent(
            conn, tenant_id=principal.tenant_id, actor_id=principal.membership_id,
            route=f"POST /jobs/{job_id}/cancel", key=key, payload=None, effect=effect,
        )  # fmt: skip
    return JSONResponse(out, status_code=status)


@router.get("/sources/{upload_id}/file", summary="Short-lived signed link to a scanned source file")
def source_file(
    upload_id: uuid.UUID,
    derivative: Literal["original"] = Query(default="original"),
    principal: Principal = Depends(require(Role.UPLOADER, Role.REVIEWER)),
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        up, batch = service.load_upload(conn, principal, upload_id)
        if batch.owner_id != principal.membership_id and not principal.has_any(Role.REVIEWER):
            raise not_found()
        if up.source_purged_at is not None:
            raise ApiError(410, "SOURCE_PURGED", "Source file deleted under the retention policy. The record, its "
                           "values and their provenance remain.")  # fmt: skip
        if up.state != "READY":
            raise conflict("SOURCE_NOT_SCANNED", "The file is not available until scanning has passed.")
        audit.record(conn, tenant_id=principal.tenant_id, actor=principal.actor, action="SOURCE_ACCESSED",
                     object_type="upload", object_id=up.id, after={"derivative": derivative})  # fmt: skip
        key_ = up.object_key
        name = up.display_name
    url = get_storage().presign_get(key_, download_name=name)
    return {"data": {"url": url, "expires_in_seconds": service.storage_ttl()}}
