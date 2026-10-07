"""API operations 10-14 and 17-20: candidates, approvals, rejection, reprocess, source pages, records."""

import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import select

from app.api.deps import etag, idempotency_key, if_match, require
from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, Issue, conflict, not_found, validation_failed
from app.core.idempotency import run_idempotent
from app.db import tables as t
from app.db.engine import tenant_tx
from app.domain.enums import Role
from app.extraction.normalize import FIELDS
from app.ingestion import service as ingestion
from app.ingestion.limits import PIPELINE_VERSION
from app.records import service as records
from app.review import service as review
from app.storage.objects import get_storage, object_key_for

router = APIRouter(tags=["review"])
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)]
EDITORS = (Role.UPLOADER, Role.REVIEWER)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["KEEP", "SKIP"]
    reason: str | None = Field(default=None, max_length=500)


class CandidatePatch(BaseModel):
    """Reviewer-entered canonical values. Dates are YYYY-MM-DD, quantities decimal strings, IDs UUIDs."""

    model_config = ConfigDict(extra="forbid")
    fields: dict[str, Any] = Field(default_factory=dict)
    duplicate_decision: Decision | None = None


class ApprovalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: uuid.UUID
    version: int


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ApprovalItem] = Field(min_length=1, max_length=200)
    ack_partial: bool = False


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Reason


class ReprocessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    preserve_edits: Literal[True] = True


class RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, Any] = Field(min_length=1)
    reason: Reason


def _check_fields(fields: dict[str, Any]) -> None:
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise validation_failed(
            [Issue("UNKNOWN_FIELD", f"{name} is not a record field.", f"fields.{name}") for name in unknown]
        )
    for name, value in fields.items():
        if isinstance(value, float):
            raise validation_failed([Issue("NOT_A_NUMBER", "Send quantities as decimal strings.", f"fields.{name}")])


def _idem(conn, principal: Principal, route: str, key: str | None, payload: Any, effect) -> tuple[int, dict]:
    return run_idempotent(
        conn,
        tenant_id=principal.tenant_id,
        actor_id=principal.membership_id,
        route=route,
        key=key,
        payload=payload,
        effect=effect,
    )


@router.get("/batches/{batch_id}/candidates", summary="Extracted entries awaiting review in a batch")
def list_candidates(
    batch_id: uuid.UUID,
    include_closed: bool = Query(default=False),
    principal: Principal = Depends(require(*EDITORS)),
) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": review.list_candidates(conn, principal, batch_id, include_closed)}


@router.get("/candidates/{candidate_id}", summary="One candidate with evidence and issues")
def get_candidate(candidate_id: uuid.UUID, principal: Principal = Depends(require(*EDITORS))) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        row, _ = review.load_candidate(conn, principal, candidate_id)
        data = review.candidate_view(conn, row)
    return JSONResponse({"data": data}, headers={"ETag": etag(data["version"])})


@router.get("/candidates/{candidate_id}/changes", summary="Reviewer change history")
def candidate_changes(candidate_id: uuid.UUID, principal: Principal = Depends(require(*EDITORS))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        return {"data": review.changes_for(conn, principal, candidate_id)}


@router.patch("/candidates/{candidate_id}", summary="Autosave reviewer corrections (If-Match)")
def patch_candidate(
    candidate_id: uuid.UUID,
    payload: dict[str, Any] = Body(...),
    expected_version: int = Depends(if_match),
    principal: Principal = Depends(require(*EDITORS, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    try:
        body = CandidatePatch.model_validate(payload)
    except ValidationError as exc:
        raise validation_failed(
            [Issue(e["type"].upper(), e["msg"], ".".join(map(str, e["loc"]))) for e in exc.errors()]
        ) from exc
    _check_fields(body.fields)
    decision_set = "duplicate_decision" in payload
    decision = body.duplicate_decision.model_dump() if body.duplicate_decision else None
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"PATCH /candidates/{candidate_id}",
            key,
            [expected_version, payload],
            lambda: (
                200,
                {
                    "data": review.patch_candidate(
                        conn, principal, candidate_id, expected_version, body.fields, decision, decision_set
                    )
                },
            ),
        )
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})


@router.post("/approvals", summary="Approve selected entries atomically")
def approve(
    body: ApprovalIn,
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    items = [(i.candidate_id, i.version) for i in body.items]
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            "POST /approvals",
            key,
            body.model_dump(mode="json"),
            lambda: (200, {"data": review.approve(conn, principal, items, body.ack_partial)}),
        )
    return JSONResponse(out, status_code=status)


@router.post("/candidates/{candidate_id}/reject", summary="Reject an entry with a reason")
def reject(
    candidate_id: uuid.UUID,
    body: ReasonIn,
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /candidates/{candidate_id}/reject",
            key,
            body.model_dump(),
            lambda: (200, {"data": review.reject(conn, principal, candidate_id, body.reason)}),
        )
    return JSONResponse(out, status_code=status)


@router.post("/uploads/{upload_id}/candidates", status_code=201, summary="Start manual entry for a file")
def manual_candidate(
    upload_id: uuid.UUID,
    principal: Principal = Depends(require(*EDITORS, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /uploads/{upload_id}/candidates",
            key,
            None,
            lambda: (201, {"data": review.create_manual_candidate(conn, principal, upload_id)}),
        )
    return JSONResponse(out, status_code=status)


@router.post("/batches/{batch_id}/reprocess", status_code=202, summary="Extract again; edits are preserved")
def reprocess(
    batch_id: uuid.UUID,
    body: ReprocessIn,
    principal: Principal = Depends(require(*EDITORS, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /batches/{batch_id}/reprocess",
            key,
            body.model_dump(mode="json"),
            lambda: (202, {"data": review.reprocess(conn, principal, batch_id, body.upload_ids)}),
        )
    return JSONResponse(out, status_code=status)


@router.get("/uploads/{upload_id}/pages/{page_no}", summary="Page text and evidence spans for the source viewer")
def source_page(upload_id: uuid.UUID, page_no: int, principal: Principal = Depends(require(*EDITORS))) -> dict:
    with tenant_tx(principal.tenant_id) as conn:
        up, batch = ingestion.load_upload(conn, principal, upload_id)
        if batch.owner_id != principal.membership_id and not principal.has_any(Role.REVIEWER):
            raise not_found()
        if up.state != "READY":
            raise conflict("SOURCE_NOT_SCANNED", "The file is not available until scanning has passed.")
        if up.source_purged_at is not None:
            raise ApiError(410, "SOURCE_PURGED", "Source file deleted under the retention policy. The record, its "
                           "values and their provenance remain.")  # fmt: skip
        page = conn.execute(
            select(t.page_result).where(
                t.page_result.c.upload_id == up.id,
                t.page_result.c.page_no == page_no,
                t.page_result.c.pipeline_version == PIPELINE_VERSION,
            )
        ).one_or_none()
        if page is None:
            raise not_found()
        audit.record(
            conn,
            tenant_id=up.tenant_id,
            actor=principal.actor,
            action="SOURCE_ACCESSED",
            object_type="upload",
            object_id=up.id,
            after={"page": page_no},
        )
    storage = get_storage()
    doc = json.loads(storage.get_bytes(page.text_key, 50_000_000)) if page.state == "SUCCEEDED" else None
    image_key = object_key_for("derived", up.tenant_id, up.id, f".p{page_no}.png")
    image_url = None
    if storage.head(image_key) is not None:
        image_url = storage.presign_get(image_key)
    elif up.extension in ("jpg", "jpeg", "png"):
        image_url = storage.presign_get(up.object_key)
    return {
        "data": {
            "upload_id": str(up.id),
            "page_no": page_no,
            "page_count": up.page_count,
            "state": page.state,
            "parser": page.parser,
            "error": {"code": page.error_code, "message": page.error_message} if page.error_code else None,
            "text": doc["text"] if doc else None,
            "spans": doc["spans"] if doc else [],
            "image_url": image_url,
            "file_name": up.display_name,
        }
    }


# --- records (FR10) --------------------------------------------------------------------------


def _record_response(status: int, out: dict) -> JSONResponse:
    return JSONResponse(out, status_code=status, headers={"ETag": etag(out["data"]["version"])})


@router.get("/records/{record_id}", summary="Approved record with revision history")
def get_record(record_id: uuid.UUID, principal: Principal = Depends(require())) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        return _record_response(200, {"data": records.record_view(conn, principal, record_id)})


@router.post("/records/{record_id}/revisions", status_code=201, summary="Propose a correction (If-Match)")
def propose_revision(
    record_id: uuid.UUID,
    body: RevisionIn,
    expected_version: int = Depends(if_match),
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    _check_fields(body.fields)
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /records/{record_id}/revisions",
            key,
            [expected_version, body.model_dump(mode="json")],
            lambda: (
                201,
                {
                    "data": records.propose_revision(
                        conn, principal, record_id, expected_version, body.fields, body.reason
                    )
                },
            ),
        )
    return JSONResponse(out, status_code=status)


@router.post("/records/{record_id}/revisions/{revision_id}/approve", summary="Approve a pending correction")
def approve_revision(
    record_id: uuid.UUID,
    revision_id: uuid.UUID,
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /records/{record_id}/revisions/{revision_id}/approve",
            key,
            None,
            lambda: (200, {"data": records.decide_revision(conn, principal, record_id, revision_id, True)}),
        )
    return _record_response(status, out)


@router.post("/records/{record_id}/revisions/{revision_id}/reject", summary="Reject a pending correction")
def reject_revision(
    record_id: uuid.UUID,
    revision_id: uuid.UUID,
    body: ReasonIn,
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /records/{record_id}/revisions/{revision_id}/reject",
            key,
            body.model_dump(),
            lambda: (
                200,
                {"data": records.decide_revision(conn, principal, record_id, revision_id, False, body.reason)},
            ),
        )
    return _record_response(status, out)


@router.post("/records/{record_id}/archive", summary="Archive a record (excluded from future totals)")
def archive_record(
    record_id: uuid.UUID,
    body: ReasonIn,
    principal: Principal = Depends(require(Role.REVIEWER, mutation=True)),
    key: str | None = Depends(idempotency_key),
) -> JSONResponse:
    with tenant_tx(principal.tenant_id) as conn:
        status, out = _idem(
            conn,
            principal,
            f"POST /records/{record_id}/archive",
            key,
            body.model_dump(),
            lambda: (200, {"data": records.archive(conn, principal, record_id, body.reason)}),
        )
    return _record_response(status, out)
