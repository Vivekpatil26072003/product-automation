"""upload.extract (object = upload): parsed pages -> validated, normalized review candidates (FR05-FR07).

Customer order pages become order drafts for the order review form, decided per page:
1. a page the production extractors recognise (production table or "Production / Machine / ..." lines) stays a
   production page, unchanged;
2. otherwise, with AI_PROVIDER=claude and a model release the FR28 gate allows, Claude reads the orders on the
   page (several orders, tables without borders, Gujarati / Hindi / English, crossed-out values; app.orders.ai);
3. otherwise, or when the AI is not available, the deterministic "Label : value" reader (app.orders.fields);
4. pages with no orders go through the production extractors (and the production AI) as before.

Idempotent per job: candidates are keyed by (extraction, source_record_key) and the extraction row is
written in the same fenced transaction, so a crashed attempt leaves nothing behind and a re-run of the
same job simply produces the extraction again.
"""

import json
import logging
import uuid
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.audit import service as audit
from app.core.config import get_settings
from app.db import tables as t
from app.domain.issues import FieldIssue
from app.extraction import deterministic, pipeline
from app.extraction.claude import AiError, ClaudeExtractor
from app.extraction.normalize import normalize
from app.extraction.validate import InvalidExtraction
from app.ingestion.limits import PIPELINE_VERSION
from app.orders import ai as order_ai
from app.orders import fields as order_fields
from app.orders import service as orders
from app.pick_registers import ai as register_ai
from app.pick_registers import reader as register_reader
from app.pick_registers import service as registers
from app.shift_reports import ai as sheet_ai
from app.shift_reports import reader as sheet_reader
from app.shift_reports import service as sheets
from app.storage.objects import get_storage, object_key_for
from workers.registry import Outcome, handler
from workers.runtime import JobContext, RetryableError

log = logging.getLogger("workers.extraction")
SERVICE = audit.Actor("service", None)
AI_EXTRACTOR = "claude-extract"


def release_state(conn, tenant_id: uuid.UUID, extractor: Any, name: str = AI_EXTRACTOR) -> str:
    approved = conn.execute(
        select(t.model_release.c.id).where(
            t.model_release.c.extractor == name,
            t.model_release.c.model == extractor.model,
            t.model_release.c.prompt_hash == extractor.prompt_hash,
            t.model_release.c.schema_version == "1",
            t.model_release.c.state == "APPROVED",
        )
    ).first()
    return "APPROVED" if approved else "UNEVALUATED"


def _issue_json(i: FieldIssue) -> dict[str, Any]:
    return {"field": i.field, "code": i.code, "message": i.message, "severity": i.severity}


@handler("upload.extract")
def extract_upload(ctx: JobContext) -> Outcome:
    settings = get_settings()
    with ctx.transaction() as conn:
        up = conn.execute(select(t.upload).where(t.upload.c.id == ctx.claim.object_id)).one()
        if up.state != "READY":
            return Outcome(result={"skipped": up.state})
        batch = conn.execute(select(t.batch).where(t.batch.c.id == up.batch_id)).one()
        pages = conn.execute(
            select(t.page_result)
            .where(
                t.page_result.c.upload_id == up.id,
                t.page_result.c.pipeline_version == PIPELINE_VERSION,
                t.page_result.c.state == "SUCCEEDED",
            )
            .order_by(t.page_result.c.page_no)
        ).all()
        master = pipeline.load_context(conn, up.tenant_id)
        extractor = ClaudeExtractor() if settings.ai_provider == "claude" else None
        gate = release_state(conn, up.tenant_id, extractor) if extractor else "NOT_APPLICABLE"
        sheet_targets, _ = sheets.targets_and_params(conn, up.tenant_id)
        sheet_reader_ai = sheet_ai.ClaudeSheetReader() if settings.ai_provider == "claude" else None
        sheet_gate = (release_state(conn, up.tenant_id, sheet_reader_ai, sheet_ai.EXTRACTOR)
                      if sheet_reader_ai else "NOT_APPLICABLE")  # fmt: skip
        register_reader_ai = register_ai.ClaudeRegisterReader() if settings.ai_provider == "claude" else None
        register_gate = (release_state(conn, up.tenant_id, register_reader_ai, register_ai.EXTRACTOR)
                         if register_reader_ai else "NOT_APPLICABLE")  # fmt: skip
        reader = order_ai.ClaudeOrderReader() if settings.ai_provider == "claude" else None
        order_gate = release_state(conn, up.tenant_id, reader, order_ai.EXTRACTOR) if reader else "NOT_APPLICABLE"

    storage = get_storage()
    docs = [json.loads(storage.get_bytes(p.text_key, 50_000_000)) for p in pages]
    all_docs = docs  # evidence is stored for every page, sheet and register pages included
    ocr_spans = {s["id"] for d in docs if d["parser"].startswith("ocr:") for s in d["spans"]}
    span_conf = {s["id"]: s.get("confidence") for d in docs for s in d["spans"]}

    warnings: list[str] = []
    ai_extract = None
    if extractor and gate == "UNEVALUATED" and settings.app_env in ("staging", "production"):
        warnings.append("AI_MODEL_NOT_APPROVED")  # FR28: an unevaluated model version never runs here
    elif extractor:
        ai_extract = lambda spans, sid: extractor.extract(spans, master.date_order, sid)  # noqa: E731
    elif settings.ai_provider == "none":
        warnings.append("AI_NOT_CONFIGURED")

    if reader and order_gate == "UNEVALUATED" and settings.app_env in ("staging", "production"):
        warnings.append("AI_MODEL_NOT_APPROVED")
        reader = None
    # Daily production sheets first: a page recognised as one is merged into that day's sheet.
    if sheet_reader_ai and sheet_gate == "UNEVALUATED" and settings.app_env in ("staging", "production"):
        warnings.append("AI_MODEL_NOT_APPROVED")
        sheet_reader_ai = None
    # Pick reading registers (WGS-02) before everything else: their grid of numbers is never a sheet or an order.
    if register_reader_ai and register_gate == "UNEVALUATED" and settings.app_env in ("staging", "production"):
        warnings.append("AI_MODEL_NOT_APPROVED")
        register_reader_ai = None
    register_pages: dict[int, dict[str, Any]] = {}
    for d in docs:
        n = d["page_no"]
        reg = register_reader.read_register(d, master.date_order)
        if not reg.is_register:
            continue
        chosen, name = (reg, "pick-register-lines-1") if reg.shift and reg.cells else (None, "")
        if d["parser"].startswith("ocr:") and register_reader_ai and d["spans"]:
            try:
                ai_reg, written_date, ai_warnings = register_reader_ai.read(d, _page_image(storage, up, d))
            except AiError as exc:
                if exc.transient:
                    raise RetryableError(exc.code, exc.message, exc.retry_after) from exc
                warnings.append(f"{exc.code}:p{n}")
                ai_reg = None
            except Exception:  # noqa: BLE001 - the lines reader result is still used
                log.exception("register reader failed upload=%s page=%s", up.id, n)
                warnings.append(f"AI_REGISTER_READER_FAILED:p{n}")
                ai_reg = None
            if ai_reg is not None and ai_reg.shift and ai_reg.cells:
                if written_date:
                    ai_reg.register_date, why = register_reader.page_date(written_date, master.date_order)
                    if why:
                        ai_reg.notes.append({"label": "Date not used", "text": why})
                ai_reg.register_date = ai_reg.register_date or reg.register_date
                chosen, name = ai_reg, register_ai.EXTRACTOR
                warnings += ai_warnings
        if chosen is None:
            warnings.append(f"REGISTER_NOT_READ:p{n}")  # recognised, but no shift / values: entered by a person
        register_pages[n] = {"reading": chosen, "reader": name} if chosen else {}
    docs = [d for d in docs if d["page_no"] not in register_pages]

    sheet_pages: dict[int, dict[str, Any]] = {}
    for d in docs:
        n = d["page_no"]
        typed = sheet_reader.read_sheet(d, sheet_targets, master.date_order)
        if typed.is_sheet and not d["parser"].startswith("ocr:"):
            sheet_pages[n] = {"cells": typed.cells, "date": typed.report_date, "supervisors": typed.supervisors,
                              "notes": typed.notes, "reader": "daily-sheet-labelled-1",
                              "written_calc": typed.written_calc}  # fmt: skip
            continue
        is_production_note = deterministic.extract_tabular(d, "chk") or deterministic.extract_labelled(d, "chk")
        if (
            sheet_reader_ai
            and d["spans"]
            and d["parser"].startswith("ocr:")
            and not is_production_note
            and (typed.rows >= 2 or not order_fields.read_orders(d))
        ):
            try:
                ai_sheet = sheet_reader_ai.read(d, _page_image(storage, up, d))
            except AiError as exc:
                if exc.transient:
                    raise RetryableError(exc.code, exc.message, exc.retry_after) from exc
                warnings.append(f"{exc.code}:p{n}")
                ai_sheet = None
            except Exception:  # noqa: BLE001 - never block the other readers
                log.exception("sheet reader failed upload=%s page=%s", up.id, n)
                warnings.append(f"AI_SHEET_READER_FAILED:p{n}")
                ai_sheet = None
            if ai_sheet and ai_sheet.is_sheet and len(ai_sheet.cells) >= sheet_reader.MIN_ROWS:
                day = None
                if ai_sheet.report_date:
                    from datetime import date as _date

                    from app.domain.dates import parse_production_date

                    day = parse_production_date(ai_sheet.report_date, _date(9999, 12, 31), master.date_order).value
                sheet_pages[n] = {"cells": ai_sheet.cells, "date": day or typed.report_date,
                                  "supervisors": ai_sheet.supervisors or typed.supervisors,
                                  "notes": ai_sheet.notes or typed.notes, "reader": sheet_ai.EXTRACTOR}  # fmt: skip
                warnings += ai_sheet.warnings
                continue
        if typed.is_sheet:  # OCR lines that read cleanly as the sheet, without AI
            sheet_pages[n] = {"cells": typed.cells, "date": typed.report_date, "supervisors": typed.supervisors,
                              "notes": typed.notes, "reader": "daily-sheet-labelled-1",
                              "written_calc": typed.written_calc}  # fmt: skip
    docs = [d for d in docs if d["page_no"] not in sheet_pages]

    order_pages: dict[int, list[dict[str, Any]]] = {}
    order_errors: list[str] = []
    for d in docs:
        n = d["page_no"]
        if deterministic.extract_tabular(d, "chk") or deterministic.extract_labelled(d, "chk"):
            continue  # a production page: unchanged path
        found = order_fields.read_orders(d)
        reading = None
        if reader and d["spans"]:
            try:
                reading = reader.read(d, _page_image(storage, up, d), master.date_order)
            except AiError as exc:
                if exc.transient:
                    raise RetryableError(exc.code, exc.message, exc.retry_after) from exc
                order_errors.append(f"{exc.code}:p{n}")  # falls back to the deterministic reader below
            except Exception:  # noqa: BLE001 - e.g. no API key configured: never block the other readers
                log.exception("order reader failed upload=%s page=%s", up.id, n)
                order_errors.append(f"AI_ORDER_READER_FAILED:p{n}")
        if reading and reading.page_kind == "orders" and reading.orders:
            meta = reading.meta() | {"release": order_gate}
            order_pages[n] = [o | {"reading": meta | {"position": i}} for i, o in enumerate(reading.orders)]
            warnings += reading.warnings
        elif found:
            meta = {"reader": "order-labelled-1", "parser": d["parser"]}
            order_pages[n] = [
                {"inputs": order_fields.inputs_from_reading(f) | _confidences(f, span_conf), "extra": [],
                 "reading": meta | {"position": i}}
                for i, f in enumerate(found)
            ]  # fmt: skip
    warnings += order_errors
    record_docs = [d for d in docs if d["page_no"] not in order_pages]

    error: tuple[str, str] | None = None
    try:
        result = pipeline.extract_pages(record_docs, f"u{str(up.id)[:8]}", ai_extract)
    except AiError as exc:
        if exc.transient:
            raise RetryableError(exc.code, exc.message, exc.retry_after) from exc
        result = pipeline.PageExtraction(manual_pages=[d["page_no"] for d in record_docs])
        error = (exc.code, exc.message)
    except InvalidExtraction:
        result = pipeline.PageExtraction(manual_pages=[d["page_no"] for d in record_docs])
        error = ("AI_INVALID_OUTPUT", "The AI answer did not match the extraction schema. Enter the values manually.")
    warnings += result.warnings

    ai_used = result.ai is not None
    extraction_id = uuid.uuid4()
    with ctx.transaction() as conn:
        if all_docs:
            conn.execute(
                pg_insert(t.evidence)
                .values(
                    [
                        {
                            "id": uuid.uuid4(),
                            "tenant_id": up.tenant_id,
                            "upload_id": up.id,
                            "pipeline_version": PIPELINE_VERSION,
                            "span_id": s["id"],
                            "page": s.get("page"),
                            "raw_text": s["text"],
                            "char_start": s.get("char_start"),
                            "char_end": s.get("char_end"),
                            "sheet": s.get("sheet"),
                            "cell": s.get("cell"),
                            "polygon": s.get("polygon"),
                            "confidence": s.get("confidence"),
                        }
                        for d in all_docs
                        for s in d["spans"]
                    ]
                )
                .on_conflict_do_nothing(index_elements=["upload_id", "pipeline_version", "span_id"])
            )

        candidates = []
        for record in result.records:
            inputs = pipeline.inputs_from_record(record, batch.department_id)
            norm = normalize(inputs, master)
            values = {name: f["value"] for name, f in norm.fields.items()}
            hits = pipeline.find_duplicates(conn, candidate_id=None, upload=up, values=values)
            issues = norm.issues + pipeline.duplicate_issues(hits, None)
            if ai_used and gate == "UNEVALUATED":
                issues.append(
                    FieldIssue(
                        None,
                        "UNEVALUATED_MODEL",
                        "Extracted by an AI model version that has not passed evaluation.",
                        "warning",
                    )
                )
            candidates.append(
                (
                    record["source_record_key"],
                    pipeline.stored_fields(norm, inputs),
                    issues,
                    pipeline.triage(norm, span_conf, ocr_spans),
                    hits,
                )
            )

        read_registers = {n: p for n, p in register_pages.items() if p}
        found = candidates or order_pages or sheet_pages or read_registers
        state = "FAILED" if error else ("SUCCEEDED" if found else "NO_RECORDS")
        order_readers = {o["reading"]["reader"] for page in order_pages.values() for o in page}
        extractors = sorted(result.extractors | order_readers | {p["reader"] for p in sheet_pages.values()}
                            | {p["reader"] for p in read_registers.values()})  # fmt: skip
        conn.execute(
            insert(t.extraction).values(
                id=extraction_id,
                tenant_id=up.tenant_id,
                upload_id=up.id,
                job_id=ctx.claim.job_id,
                extractor="+".join(extractors) or "none",
                model=result.ai.model if ai_used else None,
                prompt_version=result.ai.prompt_version if ai_used else None,
                prompt_hash=result.ai.prompt_hash if ai_used else None,
                release_state=gate if ai_used else "NOT_APPLICABLE",
                state=state,
                candidate_count=len(candidates),
                warnings=sorted(set(warnings)),
                error_code=error[0] if error else None,
                error_message=error[1] if error else None,
                input_tokens=result.ai.input_tokens if ai_used else None,
                output_tokens=result.ai.output_tokens if ai_used else None,
            )
        )

        # Earlier candidates nobody edited are replaced; edited ones stay for an explicit decision.
        c = t.candidate
        previous = {
            r.source_record_key: r
            for r in conn.execute(
                select(c.c.id, c.c.source_record_key, func.count(t.candidate_change.c.id).label("edits"))
                .outerjoin(t.candidate_change, t.candidate_change.c.candidate_id == c.c.id)
                .where(c.c.upload_id == up.id, c.c.state == "NEEDS_REVIEW")
                .group_by(c.c.id)
            )
        }
        untouched = [r.id for r in previous.values() if r.edits == 0]
        if untouched:
            conn.execute(update(c).where(c.c.id.in_(untouched)).values(state="SUPERSEDED", version=c.c.version + 1))

        for key, fields, issues, confidence, hits in candidates:
            cid = uuid.uuid4()
            prev = previous.get(key)
            conn.execute(
                insert(c).values(
                    id=cid,
                    tenant_id=up.tenant_id,
                    batch_id=batch.id,
                    upload_id=up.id,
                    extraction_id=extraction_id,
                    source_record_key=key,
                    fields=fields,
                    issues=[_issue_json(i) for i in issues],
                    confidence=confidence,
                    previous_candidate_id=prev.id if prev is not None and prev.edits else None,
                )
            )
            for h in hits:
                conn.execute(
                    insert(t.duplicate_link).values(
                        id=uuid.uuid4(),
                        tenant_id=up.tenant_id,
                        candidate_id=cid,
                        kind=h.kind,
                        other_record_id=h.other_record_id,
                        other_candidate_id=h.other_candidate_id,
                        other_upload_id=h.other_upload_id,
                    )
                )
        # Order notes: unedited drafts from an earlier run are replaced; edited ones stay for the reviewer.
        d = t.order_draft
        conn.execute(
            update(d)
            .where(d.c.upload_id == up.id, d.c.state == "NEEDS_REVIEW", d.c.source == "extracted", d.c.version == 1)
            .values(state="SUPERSEDED", version=d.c.version + 1)
        )
        for page_no, page_orders in sorted(order_pages.items()):
            for order in page_orders:
                orders.create_from_reading(conn, upload=up, batch=batch, extraction_id=extraction_id,
                                           page_no=page_no, inputs=order["inputs"], extra=order["extra"],
                                           reading=order["reading"])  # fmt: skip

        for page_no, sp in sorted(sheet_pages.items()):
            sheets.merge_reading(conn, upload=up, batch=batch, page_no=page_no, cells=sp["cells"],
                                 report_date=sp["date"], supervisors=sp["supervisors"], notes=sp["notes"],
                                 reader=sp["reader"], written_calc=sp.get("written_calc"))  # fmt: skip

        for page_no, rp in sorted(read_registers.items()):
            registers.merge_reading(conn, upload=up, batch=batch, page_no=page_no, reading=rp["reading"],
                                    reader=rp["reader"])  # fmt: skip

        audit.record(
            conn,
            tenant_id=up.tenant_id,
            actor=SERVICE,
            action="EXTRACTION_COMPLETED",
            object_type="upload",
            object_id=up.id,
            after={
                "extraction_id": str(extraction_id),
                "state": state,
                "candidates": len(candidates),
                "orders": sum(len(v) for v in order_pages.values()),
                "sheet_pages": sorted(sheet_pages),
                "register_pages": sorted(read_registers),
                "extractors": extractors,
                "release": gate,
            },
        )

    out = {
        "extraction_id": str(extraction_id),
        "candidates": len(candidates),
        "orders": sum(len(v) for v in order_pages.values()),
        "extractors": extractors,
        "manual_pages": result.manual_pages,
        "warnings": sorted(set(warnings)),
    }
    if error:
        return Outcome(state="FAILED", result=out, error_code=error[0], error_message=error[1])
    return Outcome(result=out)


def _page_image(storage: Any, up: Any, doc: dict[str, Any]) -> bytes | None:
    """The page image for photographed / scanned pages (lets the AI see columns and crossed-out values)."""
    if not doc["parser"].startswith("ocr:"):
        return None
    key = object_key_for("derived", up.tenant_id, up.id, f".p{doc['page_no']}.png")
    try:
        return storage.get_bytes(key, 20_000_000) if storage.head(key) is not None else None
    except Exception:  # noqa: BLE001 - the lines alone are still readable
        log.warning("page image unavailable upload=%s page=%s", up.id, doc["page_no"])
        return None


def _confidences(found: dict[str, Any], span_conf: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """OCR line confidence for each value read by the label reader (low confidence -> must be confirmed)."""
    out = {}
    for name, (raw, ids) in found.items():
        confs = [span_conf[i] for i in ids if span_conf.get(i) is not None]
        if confs:
            out[name] = {"raw": raw, "evidence_ids": ids, "source": "extracted", "confidence": float(min(confs))}
    return out


def queue_after_parse(conn, tenant_id: uuid.UUID, upload_id: uuid.UUID, parse_job_id: uuid.UUID) -> None:
    """Called by upload.parse in its fenced transaction; one extraction per parse job, even on re-runs."""
    from app.jobs import ledger

    j = t.job
    key = f"parse:{parse_job_id}"
    if conn.execute(select(j.c.id).where(j.c.kind == "upload.extract", j.c.source_event_key == key)).first():
        return
    generation = (
        conn.execute(
            select(func.max(j.c.generation)).where(j.c.kind == "upload.extract", j.c.object_id == upload_id)
        ).scalar_one()
        or 0
    ) + 1
    ledger.create_job(
        conn,
        tenant_id=tenant_id,
        kind="upload.extract",
        object_id=upload_id,
        generation=generation,
        source_event_key=key,
    )
