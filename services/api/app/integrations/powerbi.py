"""Power BI semantic model refresh (FR15, spec §11 "Power BI").

Power BI imports approved data from the read-only views approved_production_v and production_watermark_v
(see migration 0005 and `app.cli bi-access`). This adapter only asks Power BI to refresh the semantic model
and reads the refresh history; it never pushes rows. Authentication is an Entra application (client
credentials) that has been added to the workspace.
"""

import uuid
from typing import Any

import httpx

from app.integrations.auth import entra_token
from app.integrations.base import IntegrationError, Token, client, request

API = "https://api.powerbi.com/v1.0/myorg"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"
# Refresh history statuses (REST "Get Refresh History In Group"): Unknown means still running.
STATE_BY_STATUS = {
    "Unknown": "IN_PROGRESS",
    "NotStarted": "IN_PROGRESS",
    "Completed": "COMPLETED",
    "Failed": "FAILED",
    "Disabled": "FAILED",
    "Cancelled": "FAILED",
}


class PowerBiAdapter:
    def __init__(self, config: dict[str, Any], secret: dict[str, Any], http: httpx.Client | None = None):
        self.tenant = config["tenant"]
        self.workspace_id = config["workspace_id"]
        self.dataset_id = config["dataset_id"]
        self._client_id = secret.get("client_id", "")
        self._client_secret = secret.get("client_secret", "")
        self.http = client(http)
        self._token: Token | None = None

    def _auth(self) -> dict[str, str]:
        if self._token is None or not self._token.fresh:
            self._token = entra_token(self.http, self.tenant, self._client_id, self._client_secret, SCOPE)
        return {"Authorization": f"Bearer {self._token.value}"}

    @property
    def _dataset(self) -> str:
        return f"{API}/groups/{self.workspace_id}/datasets/{self.dataset_id}"

    def test(self) -> dict[str, Any]:
        """Reads the semantic model's metadata; does not start a refresh."""
        body = request(self.http, "GET", self._dataset, "Read semantic model", headers=self._auth()).json()
        if body.get("isRefreshable") is False:
            raise IntegrationError(
                "NOT_REFRESHABLE", "This semantic model cannot be refreshed by the service.", reconnect=True
            )
        return {"name": body.get("name"), "configured_by": body.get("configuredBy")}

    def start_refresh(self) -> str:
        """Start a refresh. Returns the provider's request ID used to find it in the refresh history."""
        request_id = str(uuid.uuid4())
        resp = request(
            self.http,
            "POST",
            f"{self._dataset}/refreshes",
            "Start refresh",
            headers={**self._auth(), "RequestId": request_id},
            json={"notifyOption": "NoNotification"},
        )
        return resp.headers.get("RequestId") or resp.headers.get("x-ms-request-id") or request_id

    def refresh_state(self, request_id: str | None) -> tuple[str, str | None]:
        """(IN_PROGRESS | COMPLETED | FAILED, provider error code) for the refresh, else the latest one."""
        body = request(
            self.http,
            "GET",
            f"{self._dataset}/refreshes",
            "Read refresh history",
            headers=self._auth(),
            params={"$top": "10"},
        ).json()
        entries = body.get("value", [])
        match = next((e for e in entries if request_id and e.get("requestId") == request_id), None)
        entry = match or (entries[0] if entries else None)
        if entry is None:
            return "IN_PROGRESS", None
        state = STATE_BY_STATUS.get(entry.get("status", "Unknown"), "IN_PROGRESS")
        code = None
        if state == "FAILED":
            code = "REFRESH_" + str(entry.get("status", "FAILED")).upper()
        return state, code
