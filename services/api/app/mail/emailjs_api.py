"""EmailJS REST API, called by the worker (no browser needed): POST https://api.emailjs.com/api/v1.0/email/send.

Request: service_id, template_id, user_id (public key), accessToken (private key), template_params. EmailJS answers
200 "OK" when it accepted the email for its connected service (here the company Gmail); that is acceptance, not
proof of delivery. EmailJS must allow non-browser requests: EmailJS dashboard -> Account -> Security.

Outcomes, chosen so an email is never sent twice by accident:
- ACCEPTED  200.
- FAILED    4xx other than 429: EmailJS refused (wrong IDs, key, template, size). Nothing was sent.
- RETRY     429 or the connection could not be opened: nothing was sent; the job retries later.
- UNKNOWN   timeout after the request was sent, or a 5xx: it may have been sent. Never retried automatically;
            a person checks EmailJS -> Email History and records the outcome.
The private key is sent only to EmailJS and never logged or returned.
"""

from dataclasses import dataclass
from typing import Any

import httpx

ENDPOINT = "https://api.emailjs.com/api/v1.0/email/send"


@dataclass(frozen=True)
class Config:
    service_id: str
    template_id: str
    public_key: str
    private_key: str
    max_request_kb: int = 50

    def __repr__(self) -> str:  # never print the private key
        return f"Config(service_id={self.service_id!r}, template_id={self.template_id!r})"


@dataclass(frozen=True)
class Result:
    outcome: str  # ACCEPTED | FAILED | RETRY | UNKNOWN
    status: int | None
    text: str


def request_bytes(config: Config, params: dict[str, str]) -> int:
    return len(httpx.Request("POST", ENDPOINT, json=_body(config, params)).content)


def _body(config: Config, params: dict[str, str]) -> dict[str, Any]:
    return {
        "service_id": config.service_id,
        "template_id": config.template_id,
        "user_id": config.public_key,
        "accessToken": config.private_key,
        "template_params": params,
    }


def http_client() -> httpx.Client:
    """The HTTP client used for EmailJS (tests replace this with a recorded-response transport)."""
    return httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))


def send(config: Config, params: dict[str, str], http: httpx.Client | None = None) -> Result:
    client = http or http_client()
    try:
        resp = client.post(ENDPOINT, json=_body(config, params))
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return Result("RETRY", None, f"EmailJS could not be reached ({type(exc).__name__}).")
    except httpx.HTTPError as exc:  # sent, but no answer: it may have gone out
        return Result("UNKNOWN", None, f"No answer from EmailJS ({type(exc).__name__}).")
    finally:
        if http is None:
            client.close()
    text = (resp.text or "").strip()[:300]
    if resp.status_code == 200:
        return Result("ACCEPTED", 200, text or "OK")
    if resp.status_code == 429:
        return Result("RETRY", 429, text or "EmailJS rate limit.")
    if resp.status_code >= 500:
        return Result("UNKNOWN", resp.status_code, text or "EmailJS server error.")
    return Result("FAILED", resp.status_code, text or f"EmailJS refused the request (HTTP {resp.status_code}).")


def explain(result: Result) -> str:
    """A useful message for the most common EmailJS refusals."""
    t = result.text.lower()
    if "non-browser" in t or ("browser" in t and result.status == 403):
        return (
            "EmailJS refused a server request. In the EmailJS dashboard open Account -> Security and allow "
            "API requests for non-browser applications."
        )
    if "private key" in t or "access token" in t:
        return "EmailJS rejected the private key. Enter it again in Settings -> Owner report & email."
    if "public key" in t or "user id" in t or "user_id" in t:
        return "EmailJS rejected the public key. Check it in Settings -> Owner report & email."
    if "template" in t:
        return f"EmailJS: {result.text} Check the template ID in Settings -> Owner report & email."
    if "service" in t:
        return f"EmailJS: {result.text} Check the service ID in Settings -> Owner report & email."
    if "size" in t or "large" in t or result.status == 413:
        return "EmailJS refused the email as too large for the plan's request size limit."
    return f"EmailJS: {result.text}"
