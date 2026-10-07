"""Extraction orchestration shared by the worker, the review API and the evaluation harness.

Per page: tables -> labelled lines -> AI extractor (when configured) -> nothing (manual entry).
Every path produces schema-v1 records that go through the same validation and normalization.
"""

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, and_, func, select

from app.db import tables as t
from app.domain.enums import Unit
from app.domain.issues import FieldIssue
from app.extraction import deterministic, validate
from app.extraction.normalize import (
    CRITICAL,
    EXTRACTED_NAME,
    FIELDS,
    REQUIRED,
    FieldInput,
    MasterContext,
    Normalized,
    compact_key,
)

CONFIDENCE_THRESHOLD = 0.90  # pilot heuristic from spec §8, not a calibrated probability


# --- master data -----------------------------------------------------------------------------


def load_context(conn: Connection, tenant_id: uuid.UUID) -> MasterContext:
    tenant = conn.execute(select(t.tenant.c.timezone, t.tenant.c.date_order).where(t.tenant.c.id == tenant_id)).one()
    departments = {
        r.id: (r.code, r.name)
        for r in conn.execute(
            select(t.department.c.id, t.department.c.code, t.department.c.name).where(t.department.c.active)
        )
    }
    machines = {
        r.id: (r.code, r.department_id)
        for r in conn.execute(
            select(t.machine.c.id, t.machine.c.code, t.machine.c.department_id).where(t.machine.c.active)
        )
    }
    dept_keys: dict[str, uuid.UUID] = {}
    for dep_id, (code, name) in departments.items():
        dept_keys[compact_key(code)] = dep_id
        dept_keys[compact_key(name)] = dep_id
    machine_keys = {compact_key(code): m_id for m_id, (code, _) in machines.items()}
    for row in conn.execute(select(t.master_alias)):
        if row.department_id in departments:
            dept_keys[compact_key(row.alias)] = row.department_id
        elif row.machine_id in machines:
            machine_keys[compact_key(row.alias)] = row.machine_id
    units = {
        r.alias_key: (Unit(r.unit), Decimal(r.factor))
        for r in conn.execute(select(t.unit_alias).where(t.unit_alias.c.active))
    }
    today = datetime.now(ZoneInfo(tenant.timezone)).date()
    extra: dict[str, Any] = {"unit_aliases": units} if units else {}
    return MasterContext(
        departments, machines, dept_keys, machine_keys, date_order=tenant.date_order, today=today, **extra
    )


# --- extraction ------------------------------------------------------------------------------


@dataclass
class PageExtraction:
    records: list[dict[str, Any]] = field(default_factory=list)
    extractors: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    manual_pages: list[int] = field(default_factory=list)
    ai: Any = None  # AiExtraction metadata when the AI extractor ran


def extract_pages(pages: list[dict[str, Any]], key_prefix: str, ai_extract: Callable | None) -> PageExtraction:
    """Run extractors page by page and validate everything they return against the supplied spans."""
    out = PageExtraction()
    spans = {s["id"]: s["text"] for p in pages for s in p["spans"]}
    for page in pages:
        records, name = deterministic.extract_tabular(page, key_prefix), "tabular-1"
        if records is None:
            records, name = deterministic.extract_labelled(page, key_prefix), "labelled-text-1"
        if records is None and ai_extract is not None and page["spans"]:
            result = ai_extract(page["spans"], f"{key_prefix}-p{page['page_no']}")
            out.ai = result
            records, name = result.document["records"], "claude"
            for r in records:
                r["source_record_key"] = f"{key_prefix}-p{page['page_no']}-{r['source_record_key']}"[:200]
            out.warnings += result.document.get("warnings", [])
        if not records:
            out.manual_pages.append(page["page_no"])
            continue
        out.extractors.add(name)
        out.records += records
    if out.records:
        checked, warnings = validate.check({"schema_version": "1", "records": out.records, "warnings": []}, spans)
        out.records, out.warnings = checked, out.warnings + warnings
    return out


def inputs_from_record(record: dict[str, Any], batch_department_id: uuid.UUID) -> dict[str, FieldInput]:
    inputs = {}
    for name in FIELDS:
        f = record["fields"][EXTRACTED_NAME.get(name, name)]
        inputs[name] = FieldInput(raw=f["value"], evidence_ids=f["evidence_ids"], extractor_issues=f["issue_codes"])
    if inputs["department_id"].raw is None:
        # The upload was made for one department; propose it, visibly marked (never silently).
        inputs["department_id"] = FieldInput(source="upload_context", value=str(batch_department_id))
    return inputs


def inputs_from_stored(fields: Mapping[str, Mapping[str, Any]]) -> dict[str, FieldInput]:
    return {
        name: FieldInput(
            raw=f.get("raw"),
            evidence_ids=list(f.get("evidence_ids", [])),
            extractor_issues=list(f.get("extractor_issues", [])),
            source=f.get("source", "extracted"),
            value=f.get("value") if f.get("source") in ("reviewer", "upload_context") else None,
        )
        for name, f in fields.items()
    }


def stored_fields(norm: Normalized, inputs: Mapping[str, FieldInput]) -> dict[str, dict[str, Any]]:
    return {name: norm.fields[name] | {"extractor_issues": inputs[name].extractor_issues} for name in FIELDS}


def triage(norm: Normalized, span_confidence: Mapping[str, float | None], ocr_spans: set[str]) -> str:
    """A7: confidence is reported separately from validation. Every record is still reviewed."""
    if norm.blocking or any(norm.fields[f]["value"] is None for f in REQUIRED):
        return "ATTENTION"
    critical_ids = [e for f in CRITICAL for e in norm.fields[f]["evidence_ids"]]
    confs = [span_confidence.get(e) for e in critical_ids]
    if any(c is not None and c < CONFIDENCE_THRESHOLD for c in confs):
        return "ATTENTION"
    if any(e in ocr_spans and span_confidence.get(e) is None for e in critical_ids):
        return "UNASSESSED"
    return "OK"


# --- duplicates (FR07) -----------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateHit:
    kind: str  # EXACT_FILE | SAME_RECORD | NEAR_RECORD | PENDING_CANDIDATE
    other_record_id: uuid.UUID | None = None
    other_candidate_id: uuid.UUID | None = None
    other_upload_id: uuid.UUID | None = None


BLOCKING_DUPLICATES = {"EXACT_FILE", "SAME_RECORD", "NEAR_RECORD"}


def find_duplicates(
    conn: Connection, *, candidate_id: uuid.UUID | None, upload: Any, values: Mapping[str, Any]
) -> list[DuplicateHit]:
    hits: list[DuplicateHit] = []
    if upload.duplicate_of:
        hits.append(DuplicateHit("EXACT_FILE", other_upload_id=upload.duplicate_of))
    day, machine = values.get("production_date"), values.get("machine_id")
    if not (day and machine):
        return hits
    r, rev = t.production_record, t.record_revision
    rows = conn.execute(
        select(r.c.id, rev.c.production_qty, rev.c.unit, rev.c.operator_name)
        .join(rev, rev.c.id == r.c.current_revision_id)
        .where(
            r.c.state == "ACTIVE",
            rev.c.production_date == date.fromisoformat(day),
            rev.c.machine_id == uuid.UUID(machine),
        )
    ).all()
    for row in rows:
        same = (
            values.get("production_qty") is not None
            and Decimal(values["production_qty"]) == row.production_qty
            and values.get("unit") == row.unit
            and compact_key(values.get("operator_name") or "") == compact_key(row.operator_name)
        )
        hits.append(DuplicateHit("SAME_RECORD" if same else "NEAR_RECORD", other_record_id=row.id))
    c = t.candidate
    conditions = [
        c.c.state == "NEEDS_REVIEW",
        c.c.upload_id != upload.id,
        c.c.fields["production_date"]["value"].astext == day,
        c.c.fields["machine_id"]["value"].astext == machine,
    ]
    if candidate_id:
        conditions.append(c.c.id != candidate_id)
    pending = conn.execute(select(c.c.id).where(*conditions).limit(5)).scalars()
    hits += [DuplicateHit("PENDING_CANDIDATE", other_candidate_id=cid) for cid in pending]
    return hits


def duplicate_issues(hits: Iterable[DuplicateHit], decision: Mapping[str, Any] | None) -> list[FieldIssue]:
    hits = list(hits)
    issues = []
    if any(h.kind in BLOCKING_DUPLICATES for h in hits) and not decision:
        kinds = sorted({h.kind for h in hits if h.kind in BLOCKING_DUPLICATES})
        issues.append(
            FieldIssue(
                None,
                "DUPLICATE_UNRESOLVED",
                f"Possible duplicate ({', '.join(kinds).lower().replace('_', ' ')}). "
                "Decide whether this is a separate event before approving.",
            )
        )
    if decision and decision.get("action") == "SKIP":
        issues.append(FieldIssue(None, "DUPLICATE_SKIP", "Marked as a duplicate; reject it instead of approving."))
    if any(h.kind == "PENDING_CANDIDATE" for h in hits):
        issues.append(
            FieldIssue(
                None, "POSSIBLE_DUPLICATE_PENDING", "Another unapproved entry has the same date and machine.", "warning"
            )
        )
    return issues


def upload_is_partial(conn: Connection, upload_id: uuid.UUID) -> bool:
    from app.ingestion.limits import PIPELINE_VERSION

    return bool(
        conn.execute(
            select(func.count())
            .select_from(t.page_result)
            .where(
                and_(
                    t.page_result.c.upload_id == upload_id,
                    t.page_result.c.pipeline_version == PIPELINE_VERSION,
                    t.page_result.c.state == "FAILED",
                )
            )
        ).scalar_one()
    )
