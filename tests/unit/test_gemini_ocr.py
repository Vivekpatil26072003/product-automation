"""Gemini OCR adapter against recorded response shapes (no network): request shape, key handling, error codes, and
that a transcribed register page is read by the existing register reader."""

import json

import httpx
import pytest

from app.ingestion.gemini import ENDPOINT, GeminiTranscriber
from app.ingestion.ocr import OcrError
from app.pick_registers.reader import read_register
from tests import filegen
from tests.unit.test_pick_register import PAGE_SHIFT_II

KEY = "test-key-not-real"


def answer(doc: dict, status: int = 200, extra_parts: list | None = None) -> httpx.Response:
    parts = (extra_parts or []) + [{"text": json.dumps(doc)}]
    return httpx.Response(status, json={"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]})


def reader(handler) -> GeminiTranscriber:
    return GeminiTranscriber(
        api_key=KEY,
        model="gemini-3.8-flash",
        fallbacks=[],
        pause=0,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_request_carries_image_and_schema_and_key_only_in_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(request.url), request.headers, json.loads(request.content)
        return answer({"legible": True, "lines": PAGE_SHIFT_II})

    page = reader(handler).read(filegen.png())
    assert seen["url"] == ENDPOINT.format(model="gemini-3.8-flash") and seen["headers"]["x-goog-api-key"] == KEY
    assert KEY not in json.dumps(seen["body"]) and KEY not in seen["url"]
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png" and parts[0]["inline_data"]["data"]
    config = seen["body"]["generationConfig"]
    assert config["temperature"] == 0 and config["responseSchema"]["required"] == ["legible", "lines"]
    assert page.provider == "gemini:gemini-3.8-flash" and len(page.lines) == len(PAGE_SHIFT_II)

    # The transcription is read by the register reader exactly like typed lines.
    doc = {"page_no": 1, "parser": "ocr:gemini", "spans": [
        {"id": f"p1-s{i}", "text": line.text, "confidence": line.confidence} for i, line in enumerate(page.lines)
    ]}  # fmt: skip
    r = read_register(doc)
    assert r.shift == "II" and r.rows == 30 and len(r.cells) == 129


def test_thought_parts_and_blank_lines_are_ignored():
    page = reader(
        lambda req: answer(
            {"legible": True, "lines": ["DATE : 02/10/26", "  ", "27 | 2038 |  2090   24 |"]},
            extra_parts=[{"text": "thinking...", "thought": True}],
        )  # fmt: skip
    ).read(filegen.png())
    assert [x.text for x in page.lines] == ["DATE : 02/10/26", "27 | 2038 | 2090 24 |"]


@pytest.mark.parametrize(
    ("response", "code", "transient"),
    [
        (
            httpx.Response(429, headers={"retry-after": "20"}, json={"error": {"status": "RESOURCE_EXHAUSTED"}}),
            "OCR_RATE_LIMITED",
            True,
        ),
        (httpx.Response(503, json={}), "OCR_UNAVAILABLE", True),
        (httpx.Response(400, json={"error": {"message": "API key not valid"}}), "OCR_REJECTED", False),
        (httpx.Response(403, json={}), "OCR_REJECTED", False),
        (httpx.Response(200, json={"promptFeedback": {"blockReason": "OTHER"}}), "OCR_REFUSED", False),
        (
            httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}),
            "OCR_INVALID_OUTPUT",
            True,
        ),
        (answer({"legible": False, "lines": []}), "OCR_ILLEGIBLE", False),
    ],
)
def test_errors_become_stable_ocr_codes(response, code, transient):
    with pytest.raises(OcrError) as err:
        reader(lambda req: response).read(filegen.png())
    assert err.value.code == code and err.value.transient is transient and KEY not in err.value.message
    if code == "OCR_RATE_LIMITED":
        assert err.value.retry_after == 20


def test_timeout_is_retried_and_missing_key_is_refused(monkeypatch):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(OcrError) as err:
        reader(slow).read(filegen.png())
    assert err.value.code == "OCR_TIMEOUT" and err.value.transient
    fake = {"gemini_model": "m", "gemini_api_key": None, "ai_timeout_seconds": 60, "gemini_fallback_models": ""}
    monkeypatch.setattr("app.ingestion.gemini.get_settings", lambda: type("S", (), fake)())
    with pytest.raises(OcrError) as err:
        GeminiTranscriber()
    assert err.value.code == "OCR_NOT_CONFIGURED"


def test_busy_or_retired_model_falls_back_to_the_next_one():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.rsplit("/", 1)[1].split(":")[0]
        calls.append(model)
        if model == "first":
            return httpx.Response(503, json={})
        if model == "second":
            return httpx.Response(404, json={})
        return answer({"legible": True, "lines": ["DATE : 02/10/26"]})

    t = GeminiTranscriber(api_key=KEY, model="first", fallbacks=["second", "third"], pause=0,
                          http=httpx.Client(transport=httpx.MockTransport(handler)))  # fmt: skip
    page = t.read(filegen.png())
    assert calls == ["first", "second", "third"] and page.provider == "gemini:third"
    bad = GeminiTranscriber(api_key=KEY, model="first", fallbacks=["second"], pause=0,
                            http=httpx.Client(transport=httpx.MockTransport(handler)))  # fmt: skip
    with pytest.raises(OcrError) as err:
        bad.read(filegen.png())
    assert err.value.code == "OCR_MODEL_UNAVAILABLE"  # the last model's answer
    no = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    refused = GeminiTranscriber(api_key=KEY, model="x", fallbacks=["y"], http=no)
    calls.clear()
    with pytest.raises(OcrError) as err:
        refused.read(filegen.png())
    assert err.value.code == "OCR_REJECTED"  # a refused key is not retried on other models


def test_all_models_busy_then_a_quick_second_round_answers():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) <= 2:  # both models busy in the first round
            return httpx.Response(503, json={})
        return answer({"legible": True, "lines": ["DATE : 02/10/26"]})

    t = GeminiTranscriber(api_key=KEY, model="a", fallbacks=["b"], pause=0,
                          http=httpx.Client(transport=httpx.MockTransport(handler)))  # fmt: skip
    assert t.read(filegen.png()).provider == "gemini:a" and len(calls) == 3
