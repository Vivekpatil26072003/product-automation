"""Extraction evaluation and the model promotion gate (FR28, spec §14).

Runs an extractor over a labelled gold set with exactly the production pipeline (parse -> extract ->
validate -> normalize -> triage) and scores it. Metrics are computed separately, never blended:

- critical_accuracy: exact canonical match on date, machine, production, target and unit, over expected
  records whose source is readable (a missed record counts every field as wrong);
- record_accuracy: all required fields of a record correct;
- fabricated_critical: non-null critical values produced where the gold value is null (must be 0);
- routing_recall: share of wrong critical values that review would flag (blocking issue, ATTENTION
  confidence, or record missed and left to manual entry);
- missing_blocked: share of gold-null required fields that block approval (must be 1.0);
- invalid_outputs: extractor outputs rejected by the schema.

A candidate AI release may be promoted only when an evaluation run for that exact extractor, model,
prompt hash and schema version passed every gate. Synthetic gold sets check the harness; promotion
needs the held-out set of authorized real notes described in spec §14.
"""

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.extraction import pipeline, validate
from app.extraction.normalize import CRITICAL, REQUIRED, MasterContext, compact_key, normalize
from app.ingestion import parsers

GATES = {
    "critical_accuracy": (">=", 0.98),
    "record_accuracy": (">=", 0.95),
    "routing_recall": (">=", 0.99),
    "fabricated_critical": ("==", 0),
    "missing_blocked": ("==", 1.0),
    "invalid_outputs": ("==", 0),
}
COMPARED = [
    "production_date",
    "department_id",
    "operator_name",
    "machine_id",
    "production_qty",
    "target_qty",
    "unit",
    "status",
    "stop_minutes",
]


def context_from_seed(today: date) -> MasterContext:
    """Deterministic master data (the seeded demo departments/machines) so evaluation needs no database."""
    from app.seed.demo import DEPARTMENT_ALIASES, DEPARTMENTS

    departments, machines, dept_keys, machine_keys = {}, {}, {}, {}
    for code, name, codes in DEPARTMENTS:
        dep_id = uuid.uuid5(uuid.NAMESPACE_URL, f"dept:{code}")
        departments[dep_id] = (code, name)
        dept_keys[compact_key(code)] = dept_keys[compact_key(name)] = dep_id
        for m in codes:
            m_id = uuid.uuid5(uuid.NAMESPACE_URL, f"machine:{m}")
            machines[m_id] = (m, dep_id)
            machine_keys[compact_key(m)] = m_id
    for alias, code in DEPARTMENT_ALIASES.items():
        dept_keys[compact_key(alias)] = uuid.uuid5(uuid.NAMESPACE_URL, f"dept:{code}")
    return MasterContext(departments, machines, dept_keys, machine_keys, today=today)


def _expected_values(expected: dict[str, Any]) -> dict[str, Any]:
    """Gold records use codes (department/machine); translate to the canonical IDs the pipeline emits."""
    out = dict(expected)
    if expected.get("department") is not None:
        out["department_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"dept:{expected['department']}"))
    elif "department" in expected:
        out["department_id"] = None
    if expected.get("machine") is not None:
        out["machine_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"machine:{expected['machine']}"))
    elif "machine" in expected:
        out["machine_id"] = None
    return out


def _pages(doc: dict[str, Any], base: Path) -> list[dict[str, Any]]:
    ext = doc["extension"]
    data = doc["text"].encode() if "text" in doc else (base / doc["file"]).read_bytes()
    return {"txt": parsers.parse_txt, "xlsx": parsers.parse_xlsx, "docx": parsers.parse_docx, "pdf": parsers.parse_pdf}[
        ext
    ](data)


@dataclass
class Tally:
    field_total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    field_correct: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    records: int = 0
    records_correct: int = 0
    critical_wrong: int = 0
    critical_wrong_flagged: int = 0
    fabricated_critical: int = 0
    missing_expected: int = 0
    missing_blocked: int = 0
    invalid_outputs: int = 0

    def metrics(self) -> dict[str, Any]:
        crit_total = sum(self.field_total[f] for f in CRITICAL)
        crit_ok = sum(self.field_correct[f] for f in CRITICAL)
        ratio = lambda a, b: round(a / b, 4) if b else 1.0  # noqa: E731
        return {
            "records": self.records,
            "critical_accuracy": ratio(crit_ok, crit_total),
            "record_accuracy": ratio(self.records_correct, self.records),
            "routing_recall": ratio(self.critical_wrong_flagged, self.critical_wrong),
            "fabricated_critical": self.fabricated_critical,
            "missing_blocked": ratio(self.missing_blocked, self.missing_expected),
            "invalid_outputs": self.invalid_outputs,
            "field_accuracy": {f: ratio(self.field_correct[f], self.field_total[f]) for f in COMPARED},
        }


def _score(tally: Tally, expected: dict[str, Any], predicted: Any | None) -> None:
    """predicted: (Normalized, confidence) or None when the record was not extracted at all."""
    tally.records += 1
    all_ok = True
    for name in COMPARED:
        want = expected.get(name)
        if predicted is None:
            got, flagged = None, True  # missed record: the file goes to manual entry, which review sees
        else:
            norm, confidence = predicted
            got = norm.fields[name]["value"]
            flagged = confidence == "ATTENTION" or any(i.field == name and i.blocking for i in norm.issues)
        if want is None:  # deliberately absent or unreadable in the source
            if name in REQUIRED:
                tally.missing_expected += 1
                tally.missing_blocked += int(got is None and flagged)
            if got is not None and name in CRITICAL:
                tally.fabricated_critical += 1
            continue
        tally.field_total[name] += 1
        ok = got == want
        tally.field_correct[name] += int(ok)
        all_ok &= ok
        if not ok and name in CRITICAL:
            tally.critical_wrong += 1
            tally.critical_wrong_flagged += int(flagged)
    tally.records_correct += int(all_ok and predicted is not None)


def evaluate(manifest_path: Path, ai_extract: Callable | None = None, today: date | None = None) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ctx = context_from_seed(today or date.fromisoformat(manifest["as_of"]))
    overall, slices = Tally(), defaultdict(Tally)
    for doc in manifest["documents"]:
        pages = _pages(doc, manifest_path.parent)
        try:
            result = pipeline.extract_pages(pages, doc["id"], ai_extract)
        except validate.InvalidExtraction:
            overall.invalid_outputs += 1
            slices[doc["slice"]].invalid_outputs += 1
            result = pipeline.PageExtraction()
        dept = uuid.uuid5(uuid.NAMESPACE_URL, f"dept:{doc['upload_department']}")
        predicted = []
        for record in result.records:
            inputs = pipeline.inputs_from_record(record, dept)
            norm = normalize(inputs, ctx)
            predicted.append((norm, pipeline.triage(norm, {}, set())))
        for i, expected in enumerate(doc["expected"]):
            got = predicted[i] if i < len(predicted) else None
            for tally in (overall, slices[doc["slice"]]):
                _score(tally, _expected_values(expected), got)
    metrics = overall.metrics()
    gates = {name: _passes(metrics[name], op, bound) for name, (op, bound) in GATES.items()}
    return {
        "dataset": manifest.get("name"),
        "dataset_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "documents": len(manifest["documents"]),
        "metrics": metrics,
        "gates": gates,
        "passed": all(gates.values()),
        "slices": {name: t.metrics() for name, t in sorted(slices.items())},
    }


def _passes(value: float, op: str, bound: float) -> bool:
    return value >= bound if op == ">=" else value == bound
