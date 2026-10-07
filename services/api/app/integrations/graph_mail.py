"""Microsoft Graph mail (FR20, FR22).

Test: obtains an app-only token and confirms the Mail.Send application permission; it never sends a message.
Administrators should restrict the application to the sender mailbox with an Exchange application access
policy; that restriction is outside what the test can verify.

Send: POST /users/{sender}/sendMail with a plain-text body and the report PDF as a file attachment.
HTTP 202 means the provider ACCEPTED the message; it is not delivery evidence. Outcomes are classified so the
caller never retries blindly:
- NOT_SENT  the request never reached Microsoft (connection refused, DNS, token failure): safe to retry.
- RETRY     429 before acceptance (Retry-After honoured): safe to retry.
- ACCEPTED  202.
- UNKNOWN   timeout or 5xx after the request was sent: it may have been accepted. No automatic retry.
- REJECTED  400/404/413 etc.: permanent until the draft or configuration is corrected.
- AUTH      401/403: reconnect required.
"""

import base64
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.integrations.auth import entra_token, token_roles
from app.integrations.base import IntegrationError, Token, client

SCOPE = "https://graph.microsoft.com/.default"
GRAPH = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class SendOutcome:
    kind: str  # NOT_SENT | RETRY | ACCEPTED | UNKNOWN | REJECTED | AUTH
    http_status: int | None = None
    request_id: str | None = None
    retry_after: float | None = None
    code: str | None = None


class GraphMailAdapter:
    def __init__(self, config: dict[str, Any], secret: dict[str, Any], http: httpx.Client | None = None):
        self.tenant = config["tenant"]
        self.sender_mailbox = config["sender_mailbox"]
        self._client_id = secret.get("client_id", "")
        self._client_secret = secret.get("client_secret", "")
        self.http = client(http)

    def token(self) -> Token:
        return entra_token(self.http, self.tenant, self._client_id, self._client_secret, SCOPE)

    def test(self) -> dict[str, Any]:
        roles = token_roles(self.token())
        if "Mail.Send" not in roles:
            raise IntegrationError(
                "PERMISSION_MISSING",
                "The application does not have the Mail.Send permission with admin consent.",
                reconnect=True,
            )
        return {"sender_mailbox": self.sender_mailbox, "permissions": sorted(roles)}

    def send(self, *, subject: str, body: str, recipients: list[dict[str, str]], attachment_name: str | None = None,
             attachment: bytes | None = None) -> SendOutcome:  # fmt: skip
        try:
            token = self.token()
        except IntegrationError as exc:
            return SendOutcome("AUTH" if exc.reconnect else "NOT_SENT", code=exc.code)

        def addr(kind: str) -> list[dict[str, Any]]:
            return [{"emailAddress": {"address": r["address"], **({"name": r["name"]} if r.get("name") else {})}}
                    for r in recipients if r["kind"] == kind]  # fmt: skip

        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": addr("TO"), "ccRecipients": addr("CC"), "bccRecipients": addr("BCC"),
        }  # fmt: skip
        if attachment is not None:
            message["attachments"] = [{"@odata.type": "#microsoft.graph.fileAttachment", "name": attachment_name,
                                       "contentType": "application/pdf",
                                       "contentBytes": base64.b64encode(attachment).decode()}]  # fmt: skip
        client_request_id = str(uuid.uuid4())
        try:
            resp = self.http.post(
                f"{GRAPH}/users/{self.sender_mailbox}/sendMail",
                headers={"Authorization": f"Bearer {token.value}", "client-request-id": client_request_id,
                         "return-client-request-id": "true"},
                json={"message": message, "saveToSentItems": True},
            )  # fmt: skip
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return SendOutcome("NOT_SENT", code="PROVIDER_UNREACHABLE")
        except httpx.HTTPError:  # read/write timeout or broken connection after sending: may have been accepted
            return SendOutcome("UNKNOWN", request_id=client_request_id, code="PROVIDER_TIMEOUT")
        rid = resp.headers.get("request-id") or resp.headers.get("client-request-id") or client_request_id
        if resp.status_code == 202:
            return SendOutcome("ACCEPTED", 202, rid)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "0") or 0)
            return SendOutcome("RETRY", 429, rid, retry_after, "PROVIDER_THROTTLED")
        if resp.status_code in (401, 403):
            return SendOutcome("AUTH", resp.status_code, rid, code="AUTH_FAILED")
        if resp.status_code >= 500:
            return SendOutcome("UNKNOWN", resp.status_code, rid, code="PROVIDER_ERROR")
        return SendOutcome("REJECTED", resp.status_code, rid, code="PROVIDER_REJECTED")
