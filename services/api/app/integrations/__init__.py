"""Downstream destinations (M5): Google Sheets, Power BI, Microsoft Graph mail and the ERP adapter contract.

Adapters call providers over HTTPS with httpx. Tests replace the network with `set_transport()` (an
httpx.MockTransport that imitates the provider); nothing in the application fakes a provider's success.
"""

from typing import Any

import httpx

from app.core.config import get_settings

_transport: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Test hook: route every adapter request through `transport` (None restores the real network)."""
    global _transport
    _transport = transport


def http_client() -> httpx.Client:
    kwargs: dict[str, Any] = {"timeout": get_settings().integration_timeout_seconds}
    if _transport is not None:
        kwargs["transport"] = _transport
    return httpx.Client(**kwargs)


def adapter_for(provider: str, config: dict[str, Any], secret: dict[str, Any]) -> Any:
    from app.integrations import erp, graph_mail, powerbi, sheets

    if provider == "erp":
        return erp.build(config, secret)
    cls = {
        "google_sheets": sheets.SheetsAdapter,
        "power_bi": powerbi.PowerBiAdapter,
        "ms_graph_mail": graph_mail.GraphMailAdapter,
    }[provider]
    return cls(config, secret, http_client())
