"""Checks every extractor output passes before it can become a candidate (FR05, spec §8).

1. The document must satisfy extraction schema v1 (strict: no extra keys, values are strings or null).
2. Every evidence ID must resolve to a span that was actually supplied for this upload.
3. Anti-fabrication: a non-null value must be supported by the text of its evidence spans
   (compared without case, spaces and punctuation). Otherwise the value is dropped to null and
   flagged, so a model can never introduce a value that is not in the source.
A document that fails check 1 is rejected as a whole; checks 2-3 degrade single fields.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parents[4] / "packages/contracts/json-schema/extraction.v1.json"


@lru_cache
def extraction_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def provider_schema() -> dict[str, Any]:
    """Schema as sent to a structured-output API: annotations removed, constraints unchanged."""
    schema = dict(extraction_schema())
    for key in ("$schema", "$id", "title", "description"):
        schema.pop(key, None)
    return schema


class InvalidExtraction(Exception):  # noqa: N818
    pass


def _key(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def check(document: Any, spans: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (records, warnings). `spans` maps each supplied evidence ID to its text."""
    errors = sorted(Draft202012Validator(extraction_schema()).iter_errors(document), key=lambda e: e.path)
    if errors:
        raise InvalidExtraction(f"schema violation at {'/'.join(map(str, errors[0].path)) or 'root'}")

    warnings = list(document["warnings"])
    seen_keys: set[str] = set()
    records = []
    for record in document["records"]:
        key = record["source_record_key"]
        if key in seen_keys:  # overlapping chunks must not create repeated rows
            warnings.append(f"DUPLICATE_SOURCE_RECORD_KEY:{key}")
            continue
        seen_keys.add(key)
        for field in record["fields"].values():
            known = [e for e in field["evidence_ids"] if e in spans]
            if len(known) != len(field["evidence_ids"]):
                field["issue_codes"] = sorted(set(field["issue_codes"]) | {"UNKNOWN_EVIDENCE"})
            field["evidence_ids"] = known
            value = field["value"]
            if value is None:
                continue
            if not known:
                field["value"] = None
                field["issue_codes"] = sorted(set(field["issue_codes"]) | {"EVIDENCE_MISSING"})
            elif _key(value) and _key(value) not in _key(" ".join(spans[e] for e in known)):
                field["value"] = None
                field["issue_codes"] = sorted(set(field["issue_codes"]) | {"UNSUPPORTED_VALUE"})
        records.append(record)
    return records, warnings
