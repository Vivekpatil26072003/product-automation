"""FR28 evaluation harness and promotion gate (TC52 harness part)."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import tables as t
from app.evaluation import releases
from app.evaluation.harness import evaluate

GOLD = Path(__file__).parents[1] / "ai_eval/gold_synthetic.json"


def test_metrics_are_computed_separately_and_honestly():
    report = evaluate(GOLD)
    m = report["metrics"]
    assert m["records"] == 11
    assert m["fabricated_critical"] == 0 and m["missing_blocked"] == 1.0 and m["invalid_outputs"] == 0
    assert m["routing_recall"] == 1.0  # every wrong critical value would be flagged for review
    # The deterministic extractors cannot read free text, so the run must fail the accuracy gate.
    assert report["slices"]["free-text"]["critical_accuracy"] == 0.0
    assert report["gates"]["critical_accuracy"] is False and report["passed"] is False
    for name, slice_metrics in report["slices"].items():
        if name != "free-text":
            assert slice_metrics["critical_accuracy"] == 1.0, name


def test_a_fabricating_extractor_is_caught(tmp_path):
    manifest = json.loads(GOLD.read_text(encoding="utf-8"))
    manifest["documents"] = [d for d in manifest["documents"] if d["slice"] == "free-text"]
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    class Guessing:
        """Returns a target that is not in the source: validation must drop it (anti-fabrication)."""

        def __call__(self, spans, source_id):
            from app.extraction.claude import AiExtraction

            sid = spans[0]["id"]
            f = lambda v: {"value": v, "evidence_ids": [sid] if v else [], "issue_codes": []}  # noqa: E731
            record = {
                "source_record_key": "r1",
                "fields": {
                    "production_date": f("26 Sept"),
                    "department": f("Tapeline"),
                    "operator_name": f("Rajesh"),
                    "machine": f("T-03"),
                    "production_qty": f("1100m"),
                    "target_qty": f("1250"),
                    "unit": f(None),
                    "status": f("running"),
                    "stop_minutes": f("20 minutes"),
                    "remarks": f(None),
                },
            }
            return AiExtraction({"schema_version": "1", "records": [record], "warnings": []}, "fake", "v", "h")

    report = evaluate(path, ai_extract=Guessing())
    assert report["metrics"]["fabricated_critical"] == 0  # the invented 1250 never survives validation
    assert report["metrics"]["field_accuracy"]["target_qty"] == 0.0
    assert report["metrics"]["routing_recall"] == 1.0  # and the missing target blocks approval


@pytest.mark.db
def test_failed_evaluation_blocks_promotion(seeded, owner_engine):
    report = evaluate(GOLD)
    with owner_engine.begin() as conn:
        run_id, release_id = releases.record_run(
            conn, seeded.tenant_id, report, extractor="claude-extract", model="claude-opus-5", prompt_hash="abc123"
        )
        release = conn.execute(select(t.model_release).where(t.model_release.c.tenant_id == seeded.tenant_id)).one()
        assert release.state == "CANDIDATE" and release.evaluation_run_id == run_id and release.id == release_id
    with pytest.raises(releases.PromotionRefused), owner_engine.begin() as conn:
        releases.promote(conn, seeded.tenant_id, release.id, None)

    passing = report | {"passed": True, "gates": {k: True for k in report["gates"]}}
    with owner_engine.begin() as conn:
        releases.record_run(
            conn, seeded.tenant_id, passing, extractor="claude-extract", model="claude-opus-5", prompt_hash="abc123"
        )
        releases.promote(conn, seeded.tenant_id, release.id, seeded.users["dev-admin"])
        state = conn.execute(select(t.model_release.c.state).where(t.model_release.c.id == release.id)).scalar_one()
    assert state == "APPROVED"
