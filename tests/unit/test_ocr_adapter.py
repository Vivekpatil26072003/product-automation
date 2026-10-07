"""Azure Read adapter against recorded response shapes (no network). Live verification is a staging gate."""

import httpx
import pytest

from app.ingestion.ocr import AzureReadOcr, NotConfiguredOcr, OcrError

ENDPOINT = "https://ocr.test.invalid"
OP = f"{ENDPOINT}/documentintelligence/documentModels/prebuilt-read/analyzeResults/op-1"

RESULT = {
    "status": "succeeded",
    "analyzeResult": {
        "pages": [{
            "width": 1000, "height": 500, "unit": "pixel",
            "lines": [
                {"content": "Production 1250 m", "polygon": [100, 50, 400, 50, 400, 80, 100, 80],
                 "spans": [{"offset": 0, "length": 17}]},
                {"content": "Target 1500", "polygon": [100, 100, 300, 100, 300, 130, 100, 130],
                 "spans": [{"offset": 18, "length": 11}]},
            ],
            "words": [
                {"content": "Production", "confidence": 0.99, "span": {"offset": 0, "length": 10}},
                {"content": "1250", "confidence": 0.81, "span": {"offset": 11, "length": 4}},
                {"content": "m", "confidence": 0.95, "span": {"offset": 16, "length": 1}},
                {"content": "Target", "confidence": 0.97, "span": {"offset": 18, "length": 6}},
                {"content": "1500", "confidence": 0.93, "span": {"offset": 25, "length": 4}},
            ],
        }]
    },
}  # fmt: skip


def _client(responses):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    return AzureReadOcr(ENDPOINT, "key-123", "2024-11-30", http=httpx.Client(transport=httpx.MockTransport(handler)),
                        poll_seconds=0), calls  # fmt: skip


def test_lines_polygons_and_confidence_are_normalized():
    ocr, calls = _client([
        httpx.Response(202, headers={"Operation-Location": OP}),
        httpx.Response(200, json={"status": "running"}),
        httpx.Response(200, json=RESULT),
    ])  # fmt: skip
    page = ocr.read(b"png-bytes")
    assert [line.text for line in page.lines] == ["Production 1250 m", "Target 1500"]
    assert page.lines[0].polygon[0] == [0.1, 0.1] and page.lines[0].polygon[2] == [0.4, 0.16]
    assert page.lines[0].confidence == 0.81  # weakest word decides the line
    assert calls[0].headers["Ocp-Apim-Subscription-Key"] == "key-123"
    assert calls[0].url.params["api-version"] == "2024-11-30"


@pytest.mark.parametrize(
    ("response", "code", "transient"),
    [
        (httpx.Response(401), "OCR_AUTH_FAILED", False),
        (httpx.Response(429, headers={"Retry-After": "7"}), "OCR_UNAVAILABLE", True),
        (httpx.Response(503), "OCR_UNAVAILABLE", True),
        (httpx.Response(400, json={"error": {"message": "secret details"}}), "OCR_REJECTED", False),
    ],
)
def test_provider_errors_map_to_stable_codes(response, code, transient):
    ocr, _ = _client([response])
    with pytest.raises(OcrError) as exc:
        ocr.read(b"png")
    assert (exc.value.code, exc.value.transient) == (code, transient)
    assert "secret details" not in exc.value.message
    if response.status_code == 429:
        assert exc.value.retry_after == 7


def test_not_configured_never_invents_text():
    with pytest.raises(OcrError) as exc:
        NotConfiguredOcr().read(b"png")
    assert exc.value.code == "OCR_NOT_CONFIGURED" and not exc.value.transient
