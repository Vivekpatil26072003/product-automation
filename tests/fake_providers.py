"""In-process imitations of Google Sheets, Microsoft Entra and Power BI for adapter tests.

They implement only the REST calls the adapters make, with the documented request/response shapes, and
record every request so tests can assert on what was (and was not) sent. Faults are injected per test.
"""

import base64
import json
import re
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

GUID = "11111111-2222-3333-4444-555555555555"
SPREADSHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def service_account_json() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()  # fmt: skip
    return json.dumps({"type": "service_account", "project_id": "test", "private_key_id": "k1", "private_key": pem,
                       "client_email": "sync@test.iam.gserviceaccount.com",
                       "token_uri": "https://oauth2.googleapis.com/token"})  # fmt: skip


def _jwt(claims: dict) -> str:
    def enc(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class FakeProviders:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tabs: dict[str, list[list]] = {"Production_Data": []}
        self.graph_roles = ["Mail.Send"]
        self.refreshes: list[dict] = []
        self.refresh_status = "Unknown"
        # hooks: (method, path regex) -> callable(request) -> Response | None (None = continue normally)
        self.faults: list[tuple[str, str, Callable[[httpx.Request], httpx.Response | None]]] = []
        self.timeout_after_append = False
        # Microsoft Graph sendMail: "accept" | "reject" | "timeout_after_accept" | "server_error" | "throttle_once"
        self.mail_mode = "accept"
        self.sent: list[dict] = []  # messages the imitation provider accepted

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def calls(self, method: str, pattern: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and re.search(pattern, r.url.path)]

    def fail_once(self, method: str, pattern: str, response: httpx.Response) -> None:
        state = {"done": False}

        def hook(_req: httpx.Request) -> httpx.Response | None:
            if state["done"]:
                return None
            state["done"] = True
            return response

        self.faults.append((method, pattern, hook))

    # --- dispatch ---------------------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, pattern, hook in self.faults:
            if request.method == method and re.search(pattern, request.url.path):
                resp = hook(request)
                if resp is not None:
                    return resp
        host, path = request.url.host, request.url.path
        if host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            return httpx.Response(200, json={"access_token": "g-token", "expires_in": 3600})
        if host == "login.microsoftonline.com":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["client_credentials"]
            scope = form["scope"][0]
            roles = self.graph_roles if "graph.microsoft.com" in scope else []
            return httpx.Response(200, json={"access_token": _jwt({"roles": roles}), "expires_in": 3600})
        if host == "sheets.googleapis.com":
            assert request.headers["Authorization"] == "Bearer g-token"
            return self._sheets(request, path)
        if host == "api.powerbi.com":
            return self._powerbi(request, path)
        if host == "graph.microsoft.com":
            return self._graph(request, path)
        return httpx.Response(404)

    # --- Google Sheets ------------------------------------------------------------------------------
    def _sheets(self, request: httpx.Request, path: str) -> httpx.Response:
        m = re.match(r"^/v4/spreadsheets/([^/]+)(.*)$", path)
        if not m or m[1] != SPREADSHEET_ID:
            return httpx.Response(404, json={"error": {"message": "not found"}})
        rest = m[2]
        if rest == "" and request.method == "GET":
            return httpx.Response(200, json={"properties": {"title": "Production"},
                                             "sheets": [{"properties": {"title": t}} for t in self.tabs]})  # fmt: skip
        if rest.startswith("/values/") and rest.endswith(":append"):
            tab, _ = self._split(unquote(rest[len("/values/") : -len(":append")]))
            params = parse_qs(request.url.query.decode())
            assert params["valueInputOption"] == ["RAW"] and params["insertDataOption"] == ["INSERT_ROWS"]
            self.tabs[tab].extend(json.loads(request.content)["values"])
            if self.timeout_after_append:
                self.timeout_after_append = False
                raise httpx.ReadTimeout("applied but no answer", request=request)
            return httpx.Response(200, json={})
        if rest.startswith("/values/") and request.method == "GET":
            tab, a1 = self._split(unquote(rest[len("/values/") :]))
            rows = self.tabs[tab]
            if a1.endswith("1") and ":" in a1 and a1.split(":")[1][-1].isdigit():
                rows = rows[:1]
            return httpx.Response(200, json={"values": [list(r) for r in rows]} if rows else {})
        if rest == "/values:batchUpdate":
            body = json.loads(request.content)
            assert body["valueInputOption"] == "RAW"
            for item in body["data"]:
                tab, a1 = self._split(item["range"])
                n = int(re.match(r"A(\d+):", a1)[1])
                grid = self.tabs[tab]
                while len(grid) < n:
                    grid.append([])
                grid[n - 1] = item["values"][0]
            return httpx.Response(200, json={})
        return httpx.Response(400)

    @staticmethod
    def _split(a1range: str) -> tuple[str, str]:
        tab, _, a1 = a1range.rpartition("!")
        return tab.strip("'"), a1

    @property
    def sheet(self) -> list[list]:
        return self.tabs["Production_Data"]

    # --- Power BI -----------------------------------------------------------------------------------
    def _powerbi(self, request: httpx.Request, path: str) -> httpx.Response:
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return httpx.Response(401)
        base = f"/v1.0/myorg/groups/{GUID}/datasets/{GUID}"
        if path == base and request.method == "GET":
            return httpx.Response(200, json={"id": GUID, "name": "Production", "isRefreshable": True})
        if path == base + "/refreshes" and request.method == "POST":
            rid = request.headers["RequestId"]
            self.refreshes.append({"requestId": rid, "status": "Unknown"})
            return httpx.Response(202, headers={"RequestId": rid})
        if path == base + "/refreshes" and request.method == "GET":
            for r in self.refreshes:
                if r["status"] == "Unknown":
                    r["status"] = self.refresh_status
            return httpx.Response(200, json={"value": list(reversed(self.refreshes))})
        return httpx.Response(404)

    # --- Microsoft Graph sendMail --------------------------------------------------------------------
    def _graph(self, request: httpx.Request, path: str) -> httpx.Response:
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return httpx.Response(401)
        m = re.match(r"^/v1.0/users/([^/]+)/sendMail$", path)
        if not m or request.method != "POST":
            return httpx.Response(404)
        body = json.loads(request.content)
        assert body["message"]["body"]["contentType"] == "Text" and body["saveToSentItems"] is True
        mode = self.mail_mode
        if mode == "throttle_once":
            self.mail_mode = "accept"
            return httpx.Response(429, headers={"Retry-After": "2"})
        if mode == "reject":
            return httpx.Response(400, json={"error": {"code": "ErrorInvalidRecipients"}})
        if mode == "server_error":
            return httpx.Response(500)
        self.sent.append({"sender": m[1], **body["message"]})
        if mode == "timeout_after_accept":
            raise httpx.ReadTimeout("accepted but the answer was lost", request=request)
        return httpx.Response(202, headers={"request-id": f"req-{len(self.sent)}"})


def query_of(request: httpx.Request) -> dict:
    return parse_qs(urlparse(str(request.url)).query)
