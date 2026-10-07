"""Review, correction, approval and rejection of extracted candidates (FR07-FR09; spec §3 "Approval
transaction"; API operations 10-14).

Access: an Uploader edits candidates of their own batches; a Reviewer edits and decides candidates in
granted departments. Only a Reviewer approves or rejects, and only for departments they are granted.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, func, insert, select, text, update

from app.audit import service as audit
from app.auth.principal import Principal
from app.core.errors import ApiError, Issue, conflict, forbidden, not_found, precondition_failed
from app.db import tables as t
from app.domain.enums import Role
from app.extraction import pipeline
from app.extraction.normalize import FIELDS, FieldInput, normalize, record_values
from app.ingestion import service as ingestion
from app.ingestion.limits import PIPELINE_VERSION
from app.outbox import service as outbox

c = t.candidate
EDITABLE_STATES = ("NEEDS_REVIEW",)


# --- views -----------------------------------------------------------------------------------


def candidate_view(conn: Connection, row: Any, evidence: dict[str, dict] | None = None) -> dict[str, Any]:
    if evidence is None:
        evidence = evidence_for(conn, row.upload_id, _evidence_ids(row.fields))
    dups = conn.execute(select(t.duplicate_link).where(t.duplicate_link.c.candidate_id == row.id)).all()
    changes = conn.execute(
        select(func.count()).select_from(t.candidate_change).where(t.candidate_change.c.candidate_id == row.id)
    ).scalar_one()
    fields = {}
    for name in FIELDS:
        f = row.fields[name]
        fields[name] = {
            "value": f.get("value"),
            "display": f.get("display"),
            "raw": f.get("raw"),
            "source": f.get("source"),
            "evidence": [evidence[e] for e in f.get("evidence_ids", []) if e in evidence],
        }
    issues = row.issues
    duplicates = [
        {
            "kind": d.kind,
            "record_id": _s(d.other_record_id),
            "candidate_id": _s(d.other_candidate_id),
            "upload_id": _s(d.other_upload_id),
        }
        for d in dups
    ]
    if row.state == "NEEDS_REVIEW":
        # Other entries may have been approved since this one was saved: duplicates are always live.
        upload = conn.execute(select(t.upload).where(t.upload.c.id == row.upload_id)).one()
        values = {n: row.fields[n].get("value") for n in FIELDS}
        hits = pipeline.find_duplicates(conn, candidate_id=row.id, upload=upload, values=values)
        duplicate_codes = {"DUPLICATE_UNRESOLVED", "DUPLICATE_SKIP", "POSSIBLE_DUPLICATE_PENDING"}
        issues = [i for i in row.issues if i["code"] not in duplicate_codes] + [
            pipeline_issue(i) for i in pipeline.duplicate_issues(hits, row.duplicate_decision)
        ]
        duplicates = [
            {
                "kind": h.kind,
                "record_id": _s(h.other_record_id),
                "candidate_id": _s(h.other_candidate_id),
                "upload_id": _s(h.other_upload_id),
            }
            for h in hits
        ]
    return {
        "id": str(row.id),
        "batch_id": str(row.batch_id),
        "upload_id": str(row.upload_id),
        "source_record_key": row.source_record_key,
        "state": row.state,
        "confidence": row.confidence,
        "fields": fields,
        "issues": issues,
        "approvable": row.state == "NEEDS_REVIEW" and not _blocking(issues),
        "duplicate_decision": row.duplicate_decision,
        "duplicates": duplicates,
        "previous_candidate_id": _s(row.previous_candidate_id),
        "record_id": _s(row.record_id),
        "reject_reason": row.reject_reason,
        "change_count": changes,
        "version": row.version,
        "updated_at": row.updated_at.isoformat(),
    }


def _s(v: Any) -> str | None:
    return str(v) if v else None


def _blocking(issues: list[dict[str, Any]]) -> bool:
    return any(i["severity"] == "error" for i in issues)


def _evidence_ids(fields: dict[str, Any]) -> set[str]:
    return {e for f in fields.values() for e in f.get("evidence_ids", [])}


def evidence_for(conn: Connection, upload_id: uuid.UUID, ids: set[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    ev = t.evidence
    rows = conn.execute(
        select(ev).where(
            ev.c.upload_id == upload_id, ev.c.pipeline_version == PIPELINE_VERSION, ev.c.span_id.in_(sorted(ids))
        )
    ).all()
    return {
        r.span_id: {
            "id": r.span_id,
            "page": r.page,
            "text": r.raw_text,
            "char_start": r.char_start,
            "char_end": r.char_end,
            "sheet": r.sheet,
            "cell": r.cell,
            "polygon": r.polygon,
            "confidence": float(r.confidence) if r.confidence is not None else None,
        }
        for r in rows
    }


def load_candidate(conn: Connection, principal: Principal, candidate_id: uuid.UUID, lock: bool = False) -> tuple:
    q = select(c).where(c.c.id == candidate_id)
    row = conn.execute(q.with_for_update() if lock else q).one_or_none()
    if row is None:
        raise not_found()
    batch = ingestion.load_batch(conn, principal, row.batch_id)
    return row, batch


def list_candidates(conn: Connection, principal: Principal, batch_id: uuid.UUID, include_closed: bool) -> dict:
    batch = ingestion.load_batch(conn, principal, batch_id)
    q = select(c).where(c.c.batch_id == batch.id)
    if not include_closed:
        q = q.where(c.c.state == "NEEDS_REVIEW")
    rows = conn.execute(q.order_by(c.c.upload_id, c.c.created_at, c.c.source_record_key)).all()
    extractions = conn.execute(
        select(t.extraction)
        .join(t.upload, t.upload.c.id == t.extraction.c.upload_id)
        .where(t.upload.c.batch_id == batch.id)
        .order_by(t.extraction.c.created_at.desc())
    ).all()
    latest: dict[uuid.UUID, Any] = {}
    for e in extractions:
        latest.setdefault(e.upload_id, e)
    return {
        "candidates": [candidate_view(conn, r) for r in rows],
        "extractions": [
            {
                "upload_id": str(u),
                "state": e.state,
                "extractor": e.extractor,
                "model": e.model,
                "release_state": e.release_state,
                "warnings": e.warnings,
                "error_code": e.error_code,
                "error_message": e.error_message,
                "created_at": e.created_at.isoformat(),
            }
            for u, e in latest.items()
        ],
    }


# --- edits (FR08) ----------------------------------------------------------------------------


def _can_edit(principal: Principal, batch: Any) -> bool:
    return principal.has_any(Role.REVIEWER) or (
        principal.has_any(Role.UPLOADER) and batch.owner_id == principal.membership_id
    )


def _renormalize(conn: Connection, row: Any, fields: dict[str, Any], decision: dict | None) -> tuple:
    upload = conn.execute(select(t.upload).where(t.upload.c.id == row.upload_id)).one()
    inputs = pipeline.inputs_from_stored(fields)
    norm = normalize(inputs, pipeline.load_context(conn, row.tenant_id))
    values = {n: f["value"] for n, f in norm.fields.items()}
    hits = pipeline.find_duplicates(conn, candidate_id=row.id, upload=upload, values=values)
    issues = norm.issues + pipeline.duplicate_issues(hits, decision)
    unevaluated = [i for i in row.issues if i["code"] == "UNEVALUATED_MODEL"]
    return norm, inputs, hits, [pipeline_issue(i) for i in issues] + unevaluated


def pipeline_issue(i: Any) -> dict[str, Any]:
    return {"field": i.field, "code": i.code, "message": i.message, "severity": i.severity}


def patch_candidate(
    conn: Connection,
    principal: Principal,
    candidate_id: uuid.UUID,
    expected_version: int,
    changes: dict[str, Any],
    decision: dict[str, Any] | None,
    decision_set: bool,
) -> dict:
    row, batch = load_candidate(conn, principal, candidate_id, lock=True)
    if not _can_edit(principal, batch):
        raise not_found()
    if row.state not in EDITABLE_STATES:
        raise conflict("IMMUTABLE", f"This entry is {row.state.lower()} and can no longer be edited.")
    if row.version != expected_version:
        raise precondition_failed(row.version)

    fields = {n: dict(f) for n, f in row.fields.items()}
    diff: dict[str, Any] = {}
    for name, value in changes.items():
        before = fields[name].get("value")
        fields[name] = fields[name] | {"source": "reviewer", "value": value}
        if value != before:
            diff[name] = {"from": before, "to": value}
    if decision_set:
        if (
            decision is not None
            and decision.get("action") == "KEEP"
            and len((decision.get("reason") or "").strip()) < 5
        ):
            raise ApiError(
                422,
                "VALIDATION_FAILED",
                "Explain why this is a separate event (at least 5 characters).",
                [Issue("REASON_REQUIRED", "A reason is required.", "duplicate_decision.reason")],
            )
        diff["duplicate_decision"] = {"from": row.duplicate_decision, "to": decision}
    else:
        decision = row.duplicate_decision

    norm, inputs, hits, issues = _renormalize(conn, row, fields, decision)
    stored = pipeline.stored_fields(norm, inputs)
    ocr_spans = _ocr_span_ids(conn, row.upload_id)
    confidence = pipeline.triage(norm, _span_confidence(conn, row.upload_id), ocr_spans)
    conn.execute(
        update(c)
        .where(c.c.id == row.id)
        .values(
            fields=stored, issues=issues, confidence=confidence, duplicate_decision=decision, version=c.c.version + 1
        )
    )
    if diff:
        conn.execute(
            insert(t.candidate_change).values(
                id=uuid.uuid4(),
                tenant_id=row.tenant_id,
                candidate_id=row.id,
                version=row.version + 1,
                actor_id=principal.membership_id,
                changes=diff,
            )
        )
    return candidate_view(conn, conn.execute(select(c).where(c.c.id == row.id)).one())


def _span_confidence(conn: Connection, upload_id: uuid.UUID) -> dict[str, float | None]:
    ev = t.evidence
    return {
        r.span_id: (float(r.confidence) if r.confidence is not None else None)
        for r in conn.execute(select(ev.c.span_id, ev.c.confidence).where(ev.c.upload_id == upload_id))
    }


def _ocr_span_ids(conn: Connection, upload_id: uuid.UUID) -> set[str]:
    pages = set(
        conn.execute(
            select(t.page_result.c.page_no).where(
                t.page_result.c.upload_id == upload_id, t.page_result.c.parser.like("ocr:%")
            )
        ).scalars()
    )
    ev = t.evidence
    return set(conn.execute(select(ev.c.span_id).where(ev.c.upload_id == upload_id, ev.c.page.in_(pages))).scalars())


def changes_for(conn: Connection, principal: Principal, candidate_id: uuid.UUID) -> list[dict[str, Any]]:
    load_candidate(conn, principal, candidate_id)
    cc = t.candidate_change
    return [
        {"version": r.version, "actor_id": str(r.actor_id), "changes": r.changes, "at": r.created_at.isoformat()}
        for r in conn.execute(select(cc).where(cc.c.candidate_id == candidate_id).order_by(cc.c.created_at))
    ]


def create_manual_candidate(conn: Connection, principal: Principal, upload_id: uuid.UUID) -> dict[str, Any]:
    """Manual entry when no extractor produced a record (spec §8: fail closed to manual review)."""
    up, batch = ingestion.load_upload(conn, principal, upload_id)
    if not _can_edit(principal, batch):
        raise not_found()
    if up.state != "READY":
        raise conflict("SOURCE_NOT_SCANNED", "Manual entry is available once the file has passed scanning.")
    extraction_id = uuid.uuid4()
    conn.execute(
        insert(t.extraction).values(
            id=extraction_id,
            tenant_id=up.tenant_id,
            upload_id=up.id,
            extractor="manual",
            state="SUCCEEDED",
            candidate_count=1,
        )
    )
    inputs = {n: FieldInput(source="manual") for n in FIELDS}
    inputs["department_id"] = FieldInput(source="upload_context", value=str(batch.department_id))
    norm = normalize(inputs, pipeline.load_context(conn, up.tenant_id))
    cid = uuid.uuid4()
    conn.execute(
        insert(c).values(
            id=cid,
            tenant_id=up.tenant_id,
            batch_id=batch.id,
            upload_id=up.id,
            extraction_id=extraction_id,
            source_record_key=f"manual-{cid.hex[:8]}",
            fields=pipeline.stored_fields(norm, inputs),
            issues=[pipeline_issue(i) for i in norm.issues],
            confidence="ATTENTION",
        )
    )
    audit.record(
        conn,
        tenant_id=up.tenant_id,
        actor=principal.actor,
        action="CANDIDATE_MANUAL_CREATED",
        object_type="candidate",
        object_id=cid,
    )
    return candidate_view(conn, conn.execute(select(c).where(c.c.id == cid)).one())


# --- decisions (FR09) ------------------------------------------------------------------------


def reject(conn: Connection, principal: Principal, candidate_id: uuid.UUID, reason: str) -> dict[str, Any]:
    if not principal.has_any(Role.REVIEWER):
        raise forbidden()
    row, _ = load_candidate(conn, principal, candidate_id, lock=True)
    if row.state != "NEEDS_REVIEW":
        raise conflict("IMMUTABLE", f"This entry is already {row.state.lower()}.")
    conn.execute(
        update(c)
        .where(c.c.id == row.id)
        .values(
            state="REJECTED",
            reject_reason=reason,
            decided_by=principal.membership_id,
            decided_at=func.now(),
            version=c.c.version + 1,
        )
    )
    audit.record(
        conn,
        tenant_id=row.tenant_id,
        actor=principal.actor,
        action="CANDIDATE_REJECTED",
        object_type="candidate",
        object_id=row.id,
        reason=reason,
    )
    _batch_decided(conn, principal, [row.batch_id])
    return candidate_view(conn, conn.execute(select(c).where(c.c.id == row.id)).one())


def _batch_decided(conn: Connection, principal: Principal, batch_ids: list[uuid.UUID]) -> None:
    """Owner reports: a batch whose entries are all decided may now be reported automatically (if switched on)."""
    from app.owner_reports import service as owner_reports

    for batch_id in sorted(set(batch_ids)):
        owner_reports.after_decision(conn, principal, batch_id)


def _business_lock(conn: Connection, tenant_id: uuid.UUID, day: str, machine: str) -> None:
    """Serialize approvals of the same date+machine so concurrent duplicates are caught (TC16)."""
    digest = hashlib.sha256(f"{tenant_id}|{day}|{machine}".encode()).digest()
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": int.from_bytes(digest[:8], "big", signed=True)})


def approve(conn: Connection, principal: Principal, items: list[tuple[uuid.UUID, int]], ack_partial: bool) -> dict:
    if not principal.has_any(Role.REVIEWER):
        raise forbidden()
    if not items:
        raise ApiError(422, "VALIDATION_FAILED", "Select at least one entry to approve.")
    ids = sorted({cid for cid, _ in items})
    expected = dict(items)
    rows = conn.execute(select(c).where(c.c.id.in_(ids)).order_by(c.c.id).with_for_update()).all()
    if len(rows) != len(ids):
        raise not_found()

    # Business locks first (sorted, so concurrent approvals cannot deadlock), then recheck everything.
    keys = sorted({(r.fields["production_date"]["value"] or "", r.fields["machine_id"]["value"] or "") for r in rows})
    for day, machine in keys:
        _business_lock(conn, principal.tenant_id, day, machine)

    problems: list[Issue] = []
    prepared = []
    for row in rows:
        ingestion.load_batch(conn, principal, row.batch_id)  # batch scope still granted
        where = f"candidates.{row.id}"
        if row.version != expected[row.id]:
            raise precondition_failed(row.version)
        if row.state != "NEEDS_REVIEW":
            raise conflict("IMMUTABLE", f"Entry {row.source_record_key} is already {row.state.lower()}.")
        norm, _, hits, issues = _renormalize(conn, row, row.fields, row.duplicate_decision)
        dept = norm.fields["department_id"]["value"]
        if dept and uuid.UUID(dept) not in principal.department_ids:
            raise forbidden("You cannot approve entries for this department.")
        for i in issues:
            if i["severity"] == "error":
                problems.append(
                    Issue(i["code"], f"{row.source_record_key}: {i['message']}", f"{where}.{i['field'] or 'record'}")
                )
        prepared.append((row, norm, hits))
    if problems:
        dup = [p for p in problems if p.code == "DUPLICATE_UNRESOLVED"]
        if dup and len(dup) == len(problems):
            raise ApiError(409, "DUPLICATE_UNRESOLVED", "Resolve possible duplicates before approving.", problems)
        raise ApiError(
            422, "VALIDATION_FAILED", f"{len(problems)} problem(s) must be fixed; nothing was approved.", problems
        )

    upload_ids = sorted({r.upload_id for r, _, _ in prepared})
    excluded = []
    for up_id in upload_ids:
        remaining = conn.execute(
            select(func.count())
            .select_from(c)
            .where(c.c.upload_id == up_id, c.c.state == "NEEDS_REVIEW", c.c.id.not_in(ids))
        ).scalar_one()
        if remaining or pipeline.upload_is_partial(conn, up_id):
            excluded.append(
                {
                    "upload_id": str(up_id),
                    "unselected_entries": remaining,
                    "failed_pages": pipeline.upload_is_partial(conn, up_id),
                }
            )
    if excluded and not ack_partial:
        raise ApiError(
            409,
            "PARTIAL_ACK_REQUIRED",
            "Some pages or entries of these files are not included. Confirm that they are excluded.",
            extra={"excluded": excluded},
        )

    now = datetime.now(UTC)
    record_ids = []
    for row, norm, _ in prepared:
        values = record_values(norm.fields)
        record_id, revision_id = uuid.uuid4(), uuid.uuid4()
        conn.execute(
            insert(t.production_record).values(
                id=record_id,
                tenant_id=row.tenant_id,
                department_id=values["department_id"],
                current_revision_id=revision_id,
                production_date=values["production_date"],
                created_by=principal.membership_id,
            )
        )
        provenance = {
            "upload_id": str(row.upload_id),
            "candidate_id": str(row.id),
            "extraction_id": str(row.extraction_id),
            "fields": {
                n: {
                    "raw": row.fields[n].get("raw"),
                    "source": row.fields[n].get("source"),
                    "evidence_ids": row.fields[n].get("evidence_ids", []),
                }
                for n in FIELDS
            },
            "duplicate_decision": row.duplicate_decision,
        }
        conn.execute(
            insert(t.record_revision).values(
                id=revision_id,
                tenant_id=row.tenant_id,
                record_id=record_id,
                number=1,
                **values,
                provenance=provenance,
                approval_state="APPROVED",
                created_by=principal.membership_id,
                approved_by=principal.membership_id,
                approved_at=now,
            )
        )
        conn.execute(
            update(c)
            .where(c.c.id == row.id)
            .values(
                state="APPROVED",
                record_id=record_id,
                decided_by=principal.membership_id,
                decided_at=now,
                version=c.c.version + 1,
            )
        )
        audit.record(
            conn,
            tenant_id=row.tenant_id,
            actor=principal.actor,
            action="RECORD_APPROVED",
            object_type="production_record",
            object_id=record_id,
            object_revision=1,
            after={"candidate_id": str(row.id)},
            reason=(row.duplicate_decision or {}).get("reason"),
        )
        outbox.enqueue(
            conn,
            tenant_id=row.tenant_id,
            event_type="production_record.changed",
            event_key=f"record:{record_id}:1",
            payload={"record_id": str(record_id), "revision": 1},
        )
        record_ids.append(str(record_id))

    data_version = conn.execute(
        update(t.tenant)
        .where(t.tenant.c.id == principal.tenant_id)
        .values(data_version=t.tenant.c.data_version + 1)
        .returning(t.tenant.c.data_version)
    ).scalar_one()
    _batch_decided(conn, principal, [r.batch_id for r, _, _ in prepared])
    return {"record_ids": record_ids, "data_version": data_version}


def reprocess(conn: Connection, principal: Principal, batch_id: uuid.UUID, upload_ids: list[uuid.UUID]) -> list:
    """New extraction generation per upload. Edited candidates are kept for an explicit comparison."""
    from app.jobs import ledger

    batch = ingestion.load_batch(conn, principal, batch_id)
    if not _can_edit(principal, batch):
        raise not_found()
    jobs = []
    for up_id in upload_ids:
        up = conn.execute(select(t.upload).where(t.upload.c.id == up_id, t.upload.c.batch_id == batch.id)).one_or_none()
        if up is None:
            raise not_found()
        if up.state != "READY":
            raise conflict("SOURCE_NOT_SCANNED", f"{up.display_name} has not passed scanning.")
        j = t.job
        running = conn.execute(
            select(j.c.id).where(
                j.c.kind == "upload.extract", j.c.object_id == up.id, j.c.state.in_(("QUEUED", "RUNNING", "RETRY_WAIT"))
            )
        ).first()
        if running:
            raise conflict("JOB_RUNNING", f"{up.display_name} is already being extracted.")
        gen = (
            conn.execute(
                select(func.max(j.c.generation)).where(j.c.kind == "upload.extract", j.c.object_id == up.id)
            ).scalar_one()
            or 0
        ) + 1
        job_id = ledger.create_job(
            conn,
            tenant_id=up.tenant_id,
            kind="upload.extract",
            object_id=up.id,
            generation=gen,
            created_by=principal.membership_id,
            source_event_key=f"reprocess:{uuid.uuid4()}",
        )
        jobs.append(ingestion.job_view(conn.execute(select(j).where(j.c.id == job_id)).one()))
    audit.record(
        conn,
        tenant_id=principal.tenant_id,
        actor=principal.actor,
        action="BATCH_REPROCESS_REQUESTED",
        object_type="batch",
        object_id=batch.id,
        after={"uploads": [str(u) for u in upload_ids]},
    )
    return jobs
