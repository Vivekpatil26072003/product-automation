"""Claude adapters through the real Anthropic SDK with a mocked HTTP transport (no network, no key).
Checks the exact request we send and how every response class is handled. Live behaviour is a staging gate.
"""

import io
import json
import os
from pathlib import Path

import anthropic
import httpx2
import pytest

from app.extraction.claude import MAX_IMAGE_BYTES, AiError, ClaudeExtractor, ClaudeTranscriber
from app.extraction.validate import provider_schema
from app.ingestion.ocr import OcrError
from tests import filegen

SAMPLE = json.loads((Path(__file__).parents[2] / "packages/contracts/fixtures/f2_extraction_sample.json").read_text())


def message(payload: dict | str, stop_reason: str = "end_turn") -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 812, "output_tokens": 245},
    }


def client_with(*responses: httpx2.Response):
    sent: list[httpx2.Request] = []
    queue = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return queue.pop(0)

    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return client, sent


SPANS = [{"id": f"S{i}", "text": t} for i, t in enumerate(["Date 27/09/2026", "Tapeline T-04 made 1250 m"], start=1)]


def test_extraction_request_is_schema_bound_and_tool_free():
    client, sent = client_with(httpx2.Response(200, json=message(SAMPLE)))
    result = ClaudeExtractor(client=client).extract(SPANS, "DMY", "u1")
    body = json.loads(sent[0].content)
    assert body["model"] == "claude-opus-5-5"  # the configured default model
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": provider_schema()}
    assert "tools" not in body and "untrusted document content" in body["system"]
    payload = json.loads(body["messages"][0]["content"][0]["text"])
    assert payload["source_spans"] == [
        {"id": "S1", "text": "Date 27/09/2026"},
        {"id": "S2", "text": "Tapeline T-04 made 1250 m"},
    ]
    assert payload["document_context"]["configured_date_order"] == "DMY"
    assert result.document["records"][0]["source_record_key"] == "c1-U001-p1-row1"
    assert (result.model, result.input_tokens, result.output_tokens) == ("claude-opus-5-5", 812, 245)
    assert len(result.prompt_hash) == 16


def test_large_inputs_are_split_at_span_boundaries():
    spans = [{"id": f"S{i}", "text": "x" * 10_000} for i in range(5)]
    empty = {"schema_version": "1", "records": [], "warnings": []}
    client, sent = client_with(*[httpx2.Response(200, json=message(empty)) for _ in range(3)])
    ClaudeExtractor(client=client).extract(spans, "DMY", "u1")
    per_call = [
        len(json.loads(json.loads(r.content)["messages"][0]["content"][0]["text"])["source_spans"]) for r in sent
    ]
    assert per_call == [2, 2, 1]


@pytest.mark.parametrize(
    ("response", "code", "transient"),
    [
        (
            httpx2.Response(
                429,
                headers={"retry-after": "12"},
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            ),
            "AI_UNAVAILABLE",
            True,
        ),
        (
            httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
            "AI_UNAVAILABLE",
            True,
        ),
        (
            httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "oops"}}),
            "AI_UNAVAILABLE",
            True,
        ),
        (
            httpx2.Response(
                401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
            ),
            "AI_AUTH_FAILED",
            False,
        ),
        (
            httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "model"}}),
            "AI_MODEL_UNAVAILABLE",
            False,
        ),
        (
            httpx2.Response(
                400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "secret detail"}}
            ),
            "AI_REJECTED",
            False,
        ),
        (httpx2.Response(200, json=message(SAMPLE, stop_reason="refusal")), "AI_REFUSED", False),
        (httpx2.Response(200, json=message(SAMPLE, stop_reason="max_tokens")), "AI_OUTPUT_TRUNCATED", False),
        (httpx2.Response(200, json=message("{not json")), "AI_INVALID_OUTPUT", False),
    ],
)
def test_every_response_class_maps_to_a_stable_code(response, code, transient):
    client, _ = client_with(response)
    with pytest.raises(AiError) as exc:
        ClaudeExtractor(client=client).extract(SPANS, "DMY", "u1")
    assert (exc.value.code, exc.value.transient) == (code, transient)
    assert "secret detail" not in exc.value.message and "bad key" not in exc.value.message
    if response.status_code == 429:
        assert exc.value.retry_after == 12


def test_transcriber_returns_lines_without_invented_geometry():
    lines = {"legible": True, "lines": [{"text": "Date 27/09/2026"}, {"text": "Production [illegible] m"}]}
    client, sent = client_with(httpx2.Response(200, json=message(lines)))
    page = ClaudeTranscriber(client=client).read(filegen.png())
    assert [line.text for line in page.lines] == ["Date 27/09/2026", "Production [illegible] m"]
    assert all(line.polygon is None and line.confidence is None for line in page.lines)
    assert page.provider == "claude:claude-opus-5-5"
    block = json.loads(sent[0].content)["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"


def test_transcriber_errors_speak_the_ocr_contract():
    client, _ = client_with(httpx2.Response(200, json=message({"legible": False, "lines": []})))
    with pytest.raises(OcrError) as exc:
        ClaudeTranscriber(client=client).read(filegen.png())
    assert exc.value.code == "OCR_ILLEGIBLE"
    client, _ = client_with(
        httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}})
    )
    with pytest.raises(OcrError) as exc:
        ClaudeTranscriber(client=client).read(filegen.png())
    assert exc.value.transient


def test_oversized_images_are_recompressed_before_sending():
    from PIL import Image

    noisy = Image.frombytes("RGB", (1800, 1800), os.urandom(1800 * 1800 * 3))
    buf = io.BytesIO()
    noisy.save(buf, format="PNG")
    assert buf.tell() > MAX_IMAGE_BYTES
    client, sent = client_with(httpx2.Response(200, json=message({"legible": True, "lines": []})))
    ClaudeTranscriber(client=client).read(buf.getvalue())
    source = json.loads(sent[0].content)["messages"][0]["content"][0]["source"]
    assert source["media_type"] == "image/jpeg"
    assert len(source["data"]) * 3 / 4 <= MAX_IMAGE_BYTES
