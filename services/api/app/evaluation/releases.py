"""Recording evaluation runs and promoting AI releases (FR28). Runs on the owner connection (operations)."""

import uuid
from typing import Any

from sqlalchemy import Connection, func, insert, select, update

from app.audit import service as audit
from app.db import tables as t


def record_run(
    conn: Connection,
    tenant_id: uuid.UUID,
    report: dict[str, Any],
    *,
    extractor: str,
    model: str | None,
    prompt_hash: str | None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Store the run and (for AI extractors) register the exact release it measured as a CANDIDATE.

    Returns (evaluation run id, model release id or None for deterministic extractors)."""
    run_id = uuid.uuid4()
    conn.execute(
        insert(t.evaluation_run).values(
            id=run_id,
            tenant_id=tenant_id,
            extractor=extractor,
            model=model,
            prompt_hash=prompt_hash,
            schema_version="1",
            dataset_hash=report["dataset_hash"],
            metrics={"overall": report["metrics"], "slices": report["slices"], "dataset": report["dataset"]},
            gates=report["gates"],
            passed=report["passed"],
        )
    )
    release_id = None
    if model and prompt_hash:
        mr = t.model_release
        existing = conn.execute(
            select(mr).where(
                mr.c.tenant_id == tenant_id,
                mr.c.extractor == extractor,
                mr.c.model == model,
                mr.c.prompt_hash == prompt_hash,
                mr.c.schema_version == "1",
            )
        ).one_or_none()
        release_id = existing.id if existing is not None else uuid.uuid4()
        if existing is None:
            conn.execute(
                insert(mr).values(
                    id=release_id,
                    tenant_id=tenant_id,
                    extractor=extractor,
                    model=model,
                    prompt_hash=prompt_hash,
                    schema_version="1",
                    evaluation_run_id=run_id,
                )
            )
        elif existing.state == "CANDIDATE":
            conn.execute(update(mr).where(mr.c.id == existing.id).values(evaluation_run_id=run_id))
    audit.record(
        conn,
        tenant_id=tenant_id,
        actor=audit.SYSTEM,
        action="EVALUATION_RECORDED",
        object_type="evaluation_run",
        object_id=run_id,
        after={"passed": report["passed"], "gates": report["gates"]},
    )
    return run_id, release_id


class PromotionRefused(Exception):
    pass


def promote(conn: Connection, tenant_id: uuid.UUID, release_id: uuid.UUID, approver_id: uuid.UUID | None) -> None:
    """CANDIDATE -> APPROVED only when its latest evaluation passed every gate; other versions retire."""
    mr, er = t.model_release, t.evaluation_run
    release = conn.execute(
        select(mr).where(mr.c.id == release_id, mr.c.tenant_id == tenant_id).with_for_update()
    ).one_or_none()
    if release is None:
        raise PromotionRefused("release not found")
    run = (
        conn.execute(select(er).where(er.c.id == release.evaluation_run_id)).one_or_none()
        if release.evaluation_run_id
        else None
    )
    if run is None or not run.passed:
        raise PromotionRefused("the release has no passing evaluation run; failed evaluation blocks promotion")
    if (run.model, run.prompt_hash) != (release.model, release.prompt_hash):
        raise PromotionRefused("the evaluation run measured a different model or prompt")
    conn.execute(
        update(mr)
        .where(mr.c.tenant_id == tenant_id, mr.c.extractor == release.extractor, mr.c.state == "APPROVED")
        .values(state="RETIRED")
    )
    conn.execute(
        update(mr)
        .where(mr.c.id == release.id)
        .values(state="APPROVED", approved_by=approver_id, approved_at=func.now())
    )
    audit.record(
        conn,
        tenant_id=tenant_id,
        actor=audit.Actor("user", approver_id) if approver_id else audit.SYSTEM,
        action="MODEL_RELEASE_APPROVED",
        object_type="model_release",
        object_id=release.id,
        after={"model": release.model, "prompt_hash": release.prompt_hash, "run": str(run.id)},
    )
