"""Ingestion jobs (FR02, FR04, FR25, FR27).

upload.scan  (object = upload): verify bytes and checksum, inspect content, malware-scan, then promote
             the file from quarantine to originals and queue parsing. Rejections are final and explained.
upload.parse (object = upload): produce per-page text with evidence. Completed pages are kept; a retry
             (new generation) processes only pages that are missing or failed.

Both handlers are idempotent: deterministic object keys and upserts mean a re-run after a crash never
duplicates objects, pages or jobs.
"""

import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.audit import service as audit
from app.db import tables as t
from app.ingestion import limits, sandbox
from app.ingestion.ocr import OcrError, OcrPage, get_ocr
from app.ingestion.parsers import ParseError
from app.ingestion.scanner import ScannerUnavailable, get_scanner
from app.ingestion.sniff import Rejected
from app.jobs import ledger
from app.storage.objects import get_storage, object_key_for
from workers.registry import Outcome, handler
from workers.runtime import JobContext, PermanentError, RetryableError

log = logging.getLogger("workers.ingestion")
SERVICE = audit.Actor("service", None)


def _load_upload(ctx: JobContext) -> Any:
    with ctx.transaction() as conn:
        return conn.execute(select(t.upload).where(t.upload.c.id == ctx.claim.object_id)).one()


def _read(up: Any) -> bytes:
    try:
        return get_storage().get_bytes(up.object_key, max_bytes=up.declared_bytes)
    except ValueError as exc:
        raise Rejected("SIZE_MISMATCH", "The stored file is larger than declared.") from exc


# --- scan ------------------------------------------------------------------------------------


def _reject(ctx: JobContext, up: Any, code: str, message: str, detail: dict | None = None) -> Outcome:
    with ctx.transaction() as conn:
        conn.execute(update(t.upload).where(t.upload.c.id == up.id, t.upload.c.state == "QUARANTINED").values(
            state="REJECTED", reject_code=code, reject_message=message, version=t.upload.c.version + 1))  # fmt: skip
        audit.record(conn, tenant_id=up.tenant_id, actor=SERVICE, action="UPLOAD_REJECTED", object_type="upload",
                     object_id=up.id, reason=code, after=detail)  # fmt: skip
    # The quarantined object stays for the rejected-file retention period (support access only).
    return Outcome(result={"rejected": code})


@handler("upload.scan")
def scan_upload(ctx: JobContext) -> Outcome:
    up = _load_upload(ctx)
    if up.state != "QUARANTINED":
        return Outcome(result={"skipped": up.state})

    try:
        data = _read(up)
        if len(data) != up.declared_bytes or hashlib.sha256(data).digest() != bytes(up.declared_sha256):
            return _reject(ctx, up, "CHECKSUM_MISMATCH", "The file changed after it was declared. Upload it again.")
        info = sandbox.run("inspect", data, up.extension)
    except Rejected as exc:
        return _reject(ctx, up, exc.code, exc.message)
    except ParseError as exc:  # inspection crashed or timed out: the file is not safe to process
        return _reject(ctx, up, "CORRUPT_FILE", "The file could not be inspected safely.", {"cause": exc.code})
    except FileNotFoundError:
        return _reject(ctx, up, "UPLOAD_MISSING", "The uploaded file is missing. Upload it again.")

    try:
        verdict = get_scanner().scan(data)
    except ScannerUnavailable as exc:
        raise RetryableError("SCANNER_UNAVAILABLE", "The malware scanner is unavailable.") from exc
    if verdict.status == "INFECTED":
        return _reject(ctx, up, "MALWARE_DETECTED", "The malware scanner flagged this file. It was not processed.",
                       {"scanner": verdict.scanner, "signature": verdict.signature})  # fmt: skip

    original_key = object_key_for("originals", up.tenant_id, up.id)
    get_storage().put_bytes(original_key, data, limits.CONTENT_TYPES[up.extension])

    with ctx.transaction() as conn:
        u = t.upload
        duplicate = conn.execute(
            select(u.c.id).where(u.c.declared_sha256 == up.declared_sha256, u.c.id != up.id, u.c.state == "READY")
            .order_by(u.c.created_at).limit(1)
        ).scalar_one_or_none()  # fmt: skip
        conn.execute(update(u).where(u.c.id == up.id).values(
            state="READY", object_key=original_key, detected_type=info["detected_type"],
            page_count=info["page_count"], scan_status=verdict.status, scanner=verdict.scanner, ready_at=func.now(),
            duplicate_of=duplicate, version=u.c.version + 1))  # fmt: skip
        ledger.create_job(conn, tenant_id=up.tenant_id, kind="upload.parse", object_id=up.id,
                          total=info["page_count"])  # fmt: skip
        audit.record(
            conn, tenant_id=up.tenant_id, actor=SERVICE, action="UPLOAD_READY", object_type="upload",
            object_id=up.id,
            after={"scan": verdict.status, "scanner": verdict.scanner, "warnings": info["warnings"],
                   "duplicate_of": str(duplicate) if duplicate else None},
        )  # fmt: skip
    try:
        get_storage().delete(up.object_key)
    except Exception:  # noqa: BLE001 - an orphaned quarantine copy is purged by retention
        log.warning("quarantine cleanup failed upload=%s", up.id)
    return Outcome(result={"ready": True, "pages": info["page_count"], "warnings": info["warnings"],
                           "duplicate_of": str(duplicate) if duplicate else None})  # fmt: skip


# --- parse -----------------------------------------------------------------------------------


def _ocr_page(page_no: int, ocr: OcrPage) -> dict[str, Any]:
    parts, spans, offset = [], [], 0
    for line in ocr.lines:
        if not line.text.strip():
            continue
        if parts:
            parts.append("\n")
            offset += 1
        spans.append({"id": f"p{page_no}-s{len(spans) + 1}", "page": page_no, "text": line.text,
                      "char_start": offset, "char_end": offset + len(line.text), "polygon": line.polygon,
                      "confidence": line.confidence})  # fmt: skip
        parts.append(line.text)
        offset += len(line.text)
    return {"page_no": page_no, "parser": f"ocr:{ocr.provider}", "text": "".join(parts), "spans": spans,
            "warnings": [], "needs_ocr": False}  # fmt: skip


def _save_page(ctx: JobContext, up: Any, page: dict[str, Any], derived_image: bytes | None = None) -> None:
    n = page["page_no"]
    storage = get_storage()
    if derived_image is not None:
        storage.put_bytes(object_key_for("derived", up.tenant_id, up.id, f".p{n}.png"), derived_image, "image/png")
    key = object_key_for("derived", up.tenant_id, up.id, f".p{n}.{limits.PIPELINE_VERSION}.json")
    doc = {"schema": "page-text/1", "upload_id": str(up.id), "pipeline_version": limits.PIPELINE_VERSION, **page}
    storage.put_bytes(key, json.dumps(doc, ensure_ascii=False).encode(), "application/json")
    confidences = [s["confidence"] for s in page["spans"] if s.get("confidence") is not None]
    _upsert_page(ctx, up, n, page["parser"], "SUCCEEDED", text_key=key, char_count=len(page["text"]),
                 span_count=len(page["spans"]), min_confidence=min(confidences) if confidences else None,
                 warnings=page["warnings"])  # fmt: skip


def _upsert_page(ctx: JobContext, up: Any, page_no: int, parser: str, state: str, **values: Any) -> None:
    row = {"text_key": None, "char_count": None, "span_count": None, "min_confidence": None, "warnings": [],
           "error_code": None, "error_message": None} | values  # fmt: skip
    p = t.page_result
    with ctx.transaction() as conn:
        stmt = insert(p).values(
            id=uuid.uuid4(), tenant_id=up.tenant_id, upload_id=up.id, page_no=page_no,
            pipeline_version=limits.PIPELINE_VERSION,
            parser=parser, state=state, **row,
        )  # fmt: skip
        conn.execute(stmt.on_conflict_do_update(
            index_elements=["upload_id", "page_no", "pipeline_version"],
            set_={"parser": parser, "state": state, "attempts": p.c.attempts + 1, "updated_at": func.now(), **row},
        ))  # fmt: skip


def _done_pages(ctx: JobContext, up: Any) -> set[int]:
    with ctx.transaction() as conn:
        return set(conn.execute(select(t.page_result.c.page_no).where(
            t.page_result.c.upload_id == up.id, t.page_result.c.pipeline_version == limits.PIPELINE_VERSION,
            t.page_result.c.state == "SUCCEEDED")).scalars())  # fmt: skip


def _progress(ctx: JobContext, up: Any, total: int) -> None:
    with ctx.transaction() as conn:
        processed = conn.execute(select(func.count()).select_from(t.page_result).where(
            t.page_result.c.upload_id == up.id,
            t.page_result.c.pipeline_version == limits.PIPELINE_VERSION)).scalar_one()  # fmt: skip
        ledger.report_progress(conn, ctx.claim, processed, total)


@handler("upload.parse")
def parse_upload(ctx: JobContext) -> Outcome:
    up = _load_upload(ctx)
    if up.state != "READY":
        return Outcome(result={"skipped": up.state})
    data = get_storage().get_bytes(up.object_key, max_bytes=up.declared_bytes)
    family = {"jpg": "image", "jpeg": "image", "png": "image"}.get(up.extension, up.extension)

    if family == "image":
        pages = [{"page_no": 1, "needs_ocr": True}]
    else:
        try:
            pages = sandbox.run(family, data)
        except ParseError as exc:
            raise PermanentError(exc.code, exc.message) from exc

    total = len(pages)
    done = _done_pages(ctx, up)
    failed: dict[int, str] = {}
    ocr = get_ocr()
    for page in pages:
        n = page["page_no"]
        if n in done:
            continue
        ctx.check_cancelled()
        if not page["needs_ocr"]:
            _save_page(ctx, up, page)
        else:
            try:
                if family == "pdf":
                    image = sandbox.run("render_pdf_page", data, n)
                else:
                    image = sandbox.run("normalize_image", data)
                _save_page(ctx, up, _ocr_page(n, ocr.read(image)), derived_image=image)
            except OcrError as exc:
                if exc.transient:  # completed pages are kept; the retry resumes from here
                    raise RetryableError(exc.code, exc.message, exc.retry_after) from exc
                failed[n] = exc.code
                _upsert_page(ctx, up, n, f"ocr:{ocr.name}", "FAILED", error_code=exc.code, error_message=exc.message)
            except ParseError as exc:
                failed[n] = exc.code
                _upsert_page(ctx, up, n, "render", "FAILED", error_code=exc.code, error_message=exc.message)
        _progress(ctx, up, total)

    result = {"pages": total, "failed_pages": sorted(failed), "failure_codes": sorted(set(failed.values()))}
    if len(failed) < total:  # at least one readable page: queue extraction for review
        from workers.extraction import queue_after_parse

        with ctx.transaction() as conn:
            queue_after_parse(conn, up.tenant_id, up.id, ctx.claim.job_id)
    if not failed:
        return Outcome(result=result)
    message = f"Pages {', '.join(map(str, sorted(failed)))} could not be read ({', '.join(result['failure_codes'])})."
    state = "FAILED" if len(failed) == total else "PARTIAL"
    return Outcome(state=state, result=result, error_code=result["failure_codes"][0], error_message=message)
