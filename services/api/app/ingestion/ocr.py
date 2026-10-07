"""OCR providers (FR04, spec §8). Pages that have no usable native text go through one of these.

- NotConfiguredOcr: the honest default. The page fails with OCR_NOT_CONFIGURED; no text is invented.
- GeminiTranscriber (app.ingestion.gemini): Google Gemini transcribes photos into lines, tables as "a | b | c".
- AzureReadOcr: Azure AI Document Intelligence prebuilt-read over its documented REST API
  (analyze + poll Operation-Location). It is unit-tested against recorded response shapes only and
  still needs a staging credential test before it is relied on (release gate, spec §19).
Provider errors are translated to stable codes; raw provider bodies are never stored or shown.
"""

import base64
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.core.config import get_settings


class OcrError(Exception):
    def __init__(self, code: str, message: str, transient: bool, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.message, self.transient, self.retry_after = code, message, transient, retry_after


@dataclass
class OcrLine:
    text: str
    polygon: list[list[float]] | None  # normalized [0,1] page coordinates
    confidence: float | None


@dataclass
class OcrPage:
    provider: str
    lines: list[OcrLine] = field(default_factory=list)


class OcrProvider(Protocol):
    name: str

    def read(self, image_png: bytes) -> OcrPage: ...


class NotConfiguredOcr:
    name = "none"

    def read(self, image_png: bytes) -> OcrPage:
        raise OcrError(
            "OCR_NOT_CONFIGURED",
            "This page is a scan or photo and needs an OCR provider. Ask an administrator to configure one.",
            transient=False,
        )


class AzureReadOcr:
    name = "azure-read"
    MODEL = "prebuilt-read"

    def __init__(self, endpoint: str, key: str, api_version: str, http: httpx.Client | None = None,
                 poll_seconds: float = 1.0, deadline_seconds: float = 120.0):  # fmt: skip
        self.endpoint = endpoint.rstrip("/")
        self._key = key
        self.api_version = api_version
        self.http = http or httpx.Client(timeout=30)
        self.poll_seconds, self.deadline_seconds = poll_seconds, deadline_seconds

    def _check(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise OcrError("OCR_AUTH_FAILED", "Processing paused: OCR provider authorization failed.", False)
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = float(resp.headers.get("Retry-After", "0") or 0)
            raise OcrError("OCR_UNAVAILABLE", "The OCR provider is busy or unavailable.", True, retry_after)
        if resp.status_code >= 400:
            raise OcrError("OCR_REJECTED", "The OCR provider could not read this page.", False)

    def read(self, image_png: bytes) -> OcrPage:
        headers = {"Ocp-Apim-Subscription-Key": self._key}
        url = f"{self.endpoint}/documentintelligence/documentModels/{self.MODEL}:analyze"
        try:
            resp = self.http.post(url, params={"api-version": self.api_version}, headers=headers,
                                  json={"base64Source": base64.b64encode(image_png).decode()})  # fmt: skip
            self._check(resp)
            op_url = resp.headers.get("Operation-Location")
            if not op_url:
                raise OcrError("OCR_REJECTED", "The OCR provider returned no operation.", True)
            started = time.monotonic()
            while True:
                poll = self.http.get(op_url, headers=headers)
                self._check(poll)
                body = poll.json()
                status = body.get("status")
                if status == "succeeded":
                    return self._to_page(body.get("analyzeResult", {}))
                if status == "failed":
                    raise OcrError("OCR_REJECTED", "The OCR provider could not read this page.", False)
                if time.monotonic() - started > self.deadline_seconds:
                    raise OcrError("OCR_TIMEOUT", "OCR took too long.", True)
                time.sleep(self.poll_seconds)
        except httpx.HTTPError as exc:
            raise OcrError("OCR_UNAVAILABLE", "The OCR provider could not be reached.", True) from exc

    def _to_page(self, result: dict[str, Any]) -> OcrPage:
        page = OcrPage(self.name)
        for p in result.get("pages", [])[:1]:  # one image in, one page out
            width, height = float(p.get("width") or 1), float(p.get("height") or 1)
            words = p.get("words", [])
            for line in p.get("lines", []):
                poly = line.get("polygon") or []
                polygon = (
                    [[round(poly[i] / width, 6), round(poly[i + 1] / height, 6)] for i in range(0, len(poly) - 1, 2)]
                    if poly else None
                )  # fmt: skip
                spans = line.get("spans", [])
                confs = [
                    w["confidence"] for w in words if "confidence" in w and any(
                        s["offset"] <= w["span"]["offset"] < s["offset"] + s["length"] for s in spans)
                ]  # fmt: skip
                page.lines.append(OcrLine(line.get("content", ""), polygon, min(confs) if confs else None))
        return page


@lru_cache
def get_ocr() -> OcrProvider:
    s = get_settings()
    if s.ocr_provider == "azure":
        return AzureReadOcr(s.azure_di_endpoint, s.azure_di_key, s.azure_di_api_version)
    if s.ocr_provider == "claude":
        from app.extraction.claude import ClaudeTranscriber

        return ClaudeTranscriber()
    if s.ocr_provider == "gemini":
        from app.ingestion.gemini import GeminiTranscriber

        return GeminiTranscriber()
    return NotConfiguredOcr()
