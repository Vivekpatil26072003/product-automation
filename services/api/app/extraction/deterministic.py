"""Deterministic extractors (spec §8 "Native parsing plus template": first choice for well-formed input).

They emit exactly the extraction schema v1 shape an AI extractor must emit, with evidence IDs that
point at the source spans they read. When the input does not look like the pattern they understand,
they return None and the next extractor (AI or manual entry) takes over; they never guess.
"""

import re
from collections import defaultdict
from typing import Any

SOURCE_FIELDS = [
    "production_date",
    "department",
    "operator_name",
    "machine",
    "production_qty",
    "target_qty",
    "unit",
    "status",
    "stop_minutes",
    "remarks",
]
MIN_FIELDS = 4  # a line block or table row must carry at least this many fields to count as a record

SYNONYMS: dict[str, list[str]] = {
    "production_date": ["production date", "date", "dt"],
    "department": ["department", "dept", "section"],
    "operator_name": ["operator name", "operator", "op name"],
    "machine": ["machine no", "machine", "m/c", "mc no", "mc"],
    "production_qty": ["production qty", "production", "prod qty", "prod", "produced", "output", "actual"],
    "target_qty": ["target qty", "target", "tgt", "plan"],
    "unit": ["unit", "uom", "units"],
    "status": ["status", "state"],
    "stop_minutes": ["stop time", "stop min", "stop minutes", "stoppage", "downtime", "stop"],
    "remarks": ["remarks", "remark", "notes", "note", "comment", "comments"],
}
_LABELS = sorted(((syn, f) for f, syns in SYNONYMS.items() for syn in syns), key=lambda x: -len(x[0]))
_LINE = re.compile(
    r"^\s*(?P<label>" + "|".join(re.escape(s) for s, _ in _LABELS) + r")\b\s*[:=\-–]?\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)
_SYNONYM_FIELD = {s: f for s, f in _LABELS}


def _field(value: str | None, evidence: list[str]) -> dict[str, Any]:
    v = value.strip() if value else None
    return {"value": v or None, "evidence_ids": evidence if v else [], "issue_codes": [] if v else ["MISSING_VALUE"]}


def _record(key: str, found: dict[str, tuple[str, str]]) -> dict[str, Any]:
    fields = {f: _field(found[f][0], [found[f][1]]) if f in found else _field(None, []) for f in SOURCE_FIELDS}
    return {"source_record_key": key, "fields": fields}


def extract_labelled(page: dict[str, Any], key_prefix: str) -> list[dict[str, Any]] | None:
    """ "Label value" lines (TXT, DOCX paragraphs, PDF text, OCR lines). A repeated label starts a new record."""
    records: list[dict[str, tuple[str, str]]] = [{}]
    for span in page["spans"]:
        m = _LINE.match(span["text"])
        if not m or not m["value"]:
            continue
        name = _SYNONYM_FIELD[m["label"].lower()]
        if name in records[-1]:
            records.append({})
        records[-1][name] = (m["value"], span["id"])
    good = [r for r in records if len(r) >= MIN_FIELDS]
    if not good:
        return None
    return [_record(f"{key_prefix}-p{page['page_no']}-b{i}", r) for i, r in enumerate(good, start=1)]


_CELL = re.compile(r"^([A-Z]+)(\d+)$")


def _grid(page: dict[str, Any]) -> dict[tuple, dict[int, dict[int, dict[str, Any]]]]:
    """Group cell spans into tables: {table key: {row: {col: span}}} for XLSX cells and DOCX tables."""
    tables: dict[tuple, dict[int, dict[int, dict[str, Any]]]] = defaultdict(lambda: defaultdict(dict))
    for span in page["spans"]:
        if span.get("cell"):
            m = _CELL.match(span["cell"])
            if not m:
                continue
            col = 0
            for ch in m[1]:
                col = col * 26 + ord(ch) - 64
            tables[("sheet", span.get("sheet"))][int(m[2])][col] = span
        elif span.get("table"):
            tables[("table", span["table"])][span["row"]][span["col"]] = span
    return tables


def _header_map(row: dict[int, dict[str, Any]]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for col, span in row.items():
        text = " ".join(span["text"].lower().replace("_", " ").split()).rstrip(":")
        text = re.sub(r"\s*\((m|kg|pcs|min|mins|minutes)\)$", "", text)
        name = _SYNONYM_FIELD.get(text)
        if name and name not in mapping.values():
            mapping[col] = name
    return mapping


def extract_tabular(page: dict[str, Any], key_prefix: str) -> list[dict[str, Any]] | None:
    """Tables whose header row names at least four known fields; each later non-empty row is a record."""
    out: list[dict[str, Any]] = []
    for (_, table_name), rows in _grid(page).items():
        header_row, mapping = None, {}
        for r in sorted(rows):
            mapping = _header_map(rows[r])
            if len(mapping) >= MIN_FIELDS:
                header_row = r
                break
        if header_row is None:
            continue
        for r in sorted(rows):
            if r <= header_row:
                continue
            found = {
                mapping[c]: (span["text"], span["id"]) for c, span in rows[r].items() if c in mapping and span["text"]
            }
            if len(found) >= MIN_FIELDS:
                out.append(_record(f"{key_prefix}-p{page['page_no']}-{table_name}-r{r}", found))
    return out or None
