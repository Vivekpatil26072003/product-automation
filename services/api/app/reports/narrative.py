"""Report summary (FR17, A4): deterministic template by default, optional Claude paraphrase.

The model receives only the immutable facts object: no records, operators, remarks or source text. It has no
tools. Its answer must be sentences with fact references, and every sentence is re-validated by
app.reports.facts.validate_sentences. Any failure (not configured, timeout, refusal, invalid JSON, an
ungrounded number, cause or recommendation language) falls back to the deterministic template, with the
reason recorded on the report.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

from app.core.config import get_settings
from app.reports.facts import Ungrounded, template_summary, validate_sentences

PROMPT_VERSION = "narrative-v1"
SYSTEM = """You write a short factual management summary of a production report.

The user message is a JSON object of computed facts, each with an ID. Rules:
- State only these facts. Do not explain causes, assign blame, name people, predict, or recommend anything.
- Copy numbers exactly as given (you may add thousands separators). Do not compute new numbers.
- Every sentence must list the IDs of the facts it states in "facts".
- At most 6 sentences, plain business English."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sentences"],
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "facts"],
                "properties": {"text": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}}},
            },
        }
    },
}
PROMPT_HASH = hashlib.sha256((SYSTEM + json.dumps(SCHEMA, sort_keys=True)).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Narrative:
    sentences: list[dict[str, Any]]
    source: str  # TEMPLATE | AI | TEMPLATE_FALLBACK
    model: str | None = None
    prompt_hash: str | None = None
    fallback_reason: str | None = None


class Writer(Protocol):
    model: str

    def write(self, facts: dict[str, Any]) -> Any: ...


def _facts_for_model(facts: dict[str, Any]) -> dict[str, Any]:
    from app.reports.facts import fact_values

    ids = sorted(fact_values(facts))
    return {"fact_ids": ids, "facts": facts}


class ClaudeWriter:
    def __init__(self, model: str | None = None, client: anthropic.Anthropic | None = None):
        s = get_settings()
        self.model = model or s.anthropic_model
        self.client = client or anthropic.Anthropic(timeout=min(s.ai_timeout_seconds, 60), max_retries=1)

    def write(self, facts: dict[str, Any]) -> Any:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(_facts_for_model(facts))}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if resp.stop_reason != "end_turn":
            raise Ungrounded("AI_" + str(resp.stop_reason).upper(), "The model did not finish normally.")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)["sentences"]


def default_writer() -> Writer | None:
    return ClaudeWriter() if get_settings().narrative_provider == "claude" else None


def summarize(facts: dict[str, Any], writer: Writer | None = None) -> Narrative:
    template = template_summary(facts)
    if writer is None or facts["record_count"] == 0:
        return Narrative(template, "TEMPLATE")
    try:
        sentences = validate_sentences(writer.write(facts), facts)
    except Ungrounded as exc:
        return Narrative(template, "TEMPLATE_FALLBACK", writer.model, PROMPT_HASH, exc.code)
    except (anthropic.APIError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return Narrative(template, "TEMPLATE_FALLBACK", writer.model, PROMPT_HASH, "AI_UNAVAILABLE_OR_INVALID")
    return Narrative(sentences, "AI", writer.model, PROMPT_HASH)
