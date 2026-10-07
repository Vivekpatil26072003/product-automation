"""Contract checks: extraction JSON Schema (spec §8) and OpenAPI drift."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).parents[2]
SCHEMA = json.loads((ROOT / "packages/contracts/json-schema/extraction.v1.json").read_text(encoding="utf-8"))
SAMPLE = json.loads((ROOT / "packages/contracts/fixtures/f2_extraction_sample.json").read_text(encoding="utf-8"))
validator = Draft202012Validator(SCHEMA)


def test_schema_is_valid_and_sample_conforms():
    Draft202012Validator.check_schema(SCHEMA)
    validator.validate(SAMPLE)


def test_every_object_forbids_additional_properties_and_requires_all_keys():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(SCHEMA)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["records"][0]["fields"].pop("target_qty"),  # missing required field
        lambda d: d["records"][0]["fields"].update(
            achievement={"value": "83.3", "evidence_ids": [], "issue_codes": []}
        ),
        lambda d: d["records"][0].update(state="APPROVED"),  # approval is never a model field
        lambda d: d["records"][0]["fields"]["production_qty"].update(value=1250),  # numbers stay strings
        lambda d: d.update(schema_version="2"),
    ],
)
def test_schema_rejects_out_of_contract_output(mutate):
    doc = copy.deepcopy(SAMPLE)
    mutate(doc)
    with pytest.raises(ValidationError):
        validator.validate(doc)


def test_unknown_value_is_null_not_guessed():
    doc = copy.deepcopy(SAMPLE)
    doc["records"][0]["fields"]["stop_minutes"] = {"value": None, "evidence_ids": [], "issue_codes": ["MISSING_VALUE"]}
    validator.validate(doc)


def test_committed_openapi_matches_code():
    from app.main import create_app

    committed = json.loads((ROOT / "packages/contracts/openapi.json").read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(create_app().openapi())), (
        "OpenAPI drifted: run `python -m app.cli openapi` and review the diff"
    )
