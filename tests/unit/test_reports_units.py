"""Report facts, grounded summary (FR17, A4, TC34) and PDF rendering (FR16) without a database."""

import io
import uuid
from datetime import date

import pytest
from pypdf import PdfReader

from app.reports import narrative, pdf
from app.reports.facts import (
    Ungrounded,
    build_facts,
    email_summary,
    fmt_number,
    template_summary,
    unverified_quantities,
    validate_sentences,
)
from app.reports.service import metrics_from_items

F1 = [  # spec §4 fixture: 4,830 m against 6,000 m, 80.5 %, statuses 2/1/1/1, 75 stop minutes
    ("d1", "Tapeline", "1250.000", "1500.000", "RUNNING", 20),
    ("d2", "Warping", "980.000", "1200.000", "COMPLETED", 10),
    ("d3", "Lamination", "1600.000", "1800.000", "RUNNING", 15),
    ("d4", "Dispatch", "550.000", "800.000", "HOLD", 20),
    ("d5", "Multifilament", "450.000", "700.000", "PENDING", 10),
]


def items(rows=F1, unit="m"):
    return [{"department_id": d, "department_name": n, "production_qty": p, "target_qty": tq, "unit": unit,
             "status": s, "stop_minutes": m, "production_date": "2026-09-27", "machine_code": "X", "operator_name": "A",
             "remarks": ""} for d, n, p, tq, s, m in rows]  # fmt: skip


def facts_for(rows_items):
    names = {x["department_id"]: x["department_name"] for x in rows_items}
    metrics = metrics_from_items(rows_items, names)
    f = build_facts(date_from=date(2026, 9, 27), date_to=date(2026, 9, 27), timezone="Asia/Kolkata", metrics=metrics,
                    department_count=len(names), excluded_pending=0)  # fmt: skip
    return metrics, f


def test_f1_metrics_and_template_summary():
    metrics, facts = facts_for(items())
    m = metrics["metrics"][0]
    assert (m["production_qty"], m["target_qty"], m["achievement_pct"], m["variance"], m["record_count"]) == (
        "4830.000", "6000.000", "80.5", "-1170.000", 5)  # fmt: skip
    assert metrics["status_counts"] == {"RUNNING": 2, "COMPLETED": 1, "PENDING": 1, "HOLD": 1}
    assert metrics["stop_total_minutes"] == 75
    text = " ".join(s["text"] for s in template_summary(facts))
    assert "4,830 m against a target of 6,000 m, achieving 80.5%" in text and "-1,170 m" in text
    assert validate_sentences(template_summary(facts), facts)  # the template is itself grounded
    assert email_summary(facts).startswith("Production for 27 September 2026 was 4,830 m")


def test_zero_target_and_mixed_units_are_reported_separately():
    rows = items(F1[:2]) + items([("d9", "Packing", "12.000", "0.000", "RUNNING", 0)], unit="pcs")
    metrics, facts = facts_for(rows)
    assert [m["unit"] for m in metrics["metrics"]] == ["m", "pcs"]
    pcs = metrics["metrics"][1]
    assert pcs["achievement_pct"] is None
    text = " ".join(s["text"] for s in template_summary(facts))
    assert "12 pcs" in text and "achievement is N/A" in text
    validate_sentences(template_summary(facts), facts)


@pytest.mark.parametrize(("sentence", "code"), [
    ({"text": "Production was 4,830 m, achieving 81%.", "facts": ["unit.m.production", "unit.m.achievement_pct"]},
     "UNGROUNDED_NUMBER"),
    ({"text": "Output fell short because Dispatch was on hold.", "facts": ["unit.m.production"]}, "UNSUPPORTED_CLAIM"),
    ({"text": "The team should add a shift.", "facts": ["record_count"]}, "UNSUPPORTED_CLAIM"),
    ({"text": "Production was 4,830 m.", "facts": ["unit.m.bogus"]}, "UNKNOWN_FACT"),
    ({"text": "There were 5 records.", "facts": []}, "NO_FACTS"),
    ({"text": "Target was 6,000 m.", "facts": ["unit.m.production"]}, "UNGROUNDED_NUMBER"),  # right number, wrong ref
])  # fmt: skip
def test_validator_rejects_unsupported_content(sentence, code):  # TC34
    _, facts = facts_for(items())
    with pytest.raises(Ungrounded) as exc:
        validate_sentences([sentence], facts)
    assert exc.value.code == code


class FakeWriter:
    model = "fake-model"

    def __init__(self, answer):
        self.answer = answer

    def write(self, facts):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_ai_summary_is_used_only_when_grounded():
    _, facts = facts_for(items())
    good = [{"text": "The plant produced 4,830 m, or 80.5% of the 6,000 m target.",
             "facts": ["unit.m.production", "unit.m.achievement_pct", "unit.m.target"]}]  # fmt: skip
    ok = narrative.summarize(facts, FakeWriter(good))
    assert ok.source == "AI" and ok.sentences == good and ok.model == "fake-model"

    bad = [{"text": "Production reached 4,830 m because Warping improved.", "facts": ["unit.m.production"]}]
    fb = narrative.summarize(facts, FakeWriter(bad))
    assert fb.source == "TEMPLATE_FALLBACK" and fb.fallback_reason == "UNSUPPORTED_CLAIM"
    assert fb.sentences == template_summary(facts)

    broken = narrative.summarize(facts, FakeWriter(ValueError("invalid json")))
    assert broken.source == "TEMPLATE_FALLBACK" and broken.fallback_reason == "AI_UNAVAILABLE_OR_INVALID"
    assert narrative.summarize(facts, None).source == "TEMPLATE"


def test_email_quantities_are_checked_against_the_report():
    _, facts = facts_for(items())
    assert unverified_quantities("Production was 4,830 m (80.5%) across 5 records.", facts) == []
    assert unverified_quantities("Production was 4,900 m. Call +91 98765 43210.", facts) == ["4,900 m"]


def test_number_formatting_matches_the_web_app():
    assert fmt_number("4830.000") == "4,830"
    assert fmt_number("123456.500") == "1,23,456.5"
    assert fmt_number("-1170", signed=True) == "-1,170"


def _render(**over):
    metrics, facts = facts_for(items())
    report = {"id": str(uuid.UUID(int=1)), "code": "P0000A1", "version": 1, "title": "Daily Production Report",
              "date_from": "2026-09-27", "date_to": "2026-09-27", "timezone": "Asia/Kolkata",
              "snapshot_at": "2026-09-27 12:00 UTC", "data_version": 7, "departments": ["Tapeline", "Warping"],
              "record_count": 5, "metrics": metrics, "facts": facts, "summary": template_summary(facts),
              "summary_source": "TEMPLATE", "include_detail": True,
              "items": [{"fields": x} for x in items()], "excluded_pending": 1} | over  # fmt: skip
    return pdf.render(report)


def test_pdf_is_reproducible_and_shows_the_snapshot_figures():
    a, b = _render(), _render()
    assert a == b  # identical bytes for the same snapshot: a retry cannot change the checksum
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(a)).pages)
    for needle in ("Daily Production Report", "4,830", "6,000", "80.5%", "-1,170", "75 minutes", "Page 1 of"):
        assert needle in text


def test_pdf_escapes_markup_and_reports_unshowable_characters():
    hostile = items([("d1", "Tapeline", "1.000", "1.000", "RUNNING", 0)])
    hostile[0]["remarks"] = '<font color="red">=SUM(A1)</font> नमस्ते'
    data = _render(items=[{"fields": x} for x in hostile])
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
    assert '<font color="red">=SUM(A1)</font>' in text.replace("\n", "")  # shown as text, not interpreted
    assert "could not be shown in this font" in text


def test_status_order_does_not_depend_on_stored_key_order():
    _, facts = facts_for(items())
    facts["status_counts"] = dict(sorted(facts["status_counts"].items(), key=lambda kv: (len(kv[0]), kv[0])))  # jsonb
    status = next(s["text"] for s in template_summary(facts) if s["text"].startswith("Status"))
    assert status == "Status at the time of recording: 2 running, 1 completed, 1 pending, 1 on hold."
