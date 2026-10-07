"""ERP adapter contract (spec addendum A5, FR22). No ERP vendor is named in the specification, so M5 ships
the contract, the sync ledger and a mock used only in development and tests. A real adapter implements
ErpAdapter and is registered in ADAPTERS; nothing else changes.

Direction: OUTBOUND only (approved records -> ERP). Bidirectional sync needs a documented conflict policy
and is refused until one is approved.
"""

import hashlib
from typing import Any, Protocol

from app.core.config import get_settings
from app.integrations.base import IntegrationError


class ErpAdapter(Protocol):
    name: str
    is_mock: bool

    def capabilities(self) -> dict[str, Any]: ...

    def health(self) -> dict[str, Any]: ...

    def test(self) -> dict[str, Any]:
        """Connection test: health and capabilities only, no record is written."""
        ...

    def push_record(self, record: dict[str, Any], mapping_version: int) -> str:
        """Create or update the record in the ERP. Must be idempotent on record_id. Returns the ERP reference."""
        ...


class MockErpAdapter:
    """Deterministic in-memory ERP for development and tests. Clearly labelled; never used in staging/production."""

    name, is_mock = "mock", True

    def __init__(self, config: dict[str, Any], secret: dict[str, Any]):
        self.fail_codes = set(config.get("mock_fail_record_ids", []))
        self.store: dict[str, dict[str, Any]] = {}

    def capabilities(self) -> dict[str, Any]:
        return {"directions": ["OUTBOUND"], "idempotent_upsert": True, "mock": True}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "mock": True}

    def test(self) -> dict[str, Any]:
        return {"health": self.health(), "capabilities": self.capabilities()}

    def push_record(self, record: dict[str, Any], mapping_version: int) -> str:
        if record["record_id"] in self.fail_codes:
            raise IntegrationError("ERP_REJECTED", "The mock ERP rejected this record (test setting).")
        self.store[record["record_id"]] = {**record, "mapping_version": mapping_version}
        return "MOCK-" + hashlib.sha256(record["record_id"].encode()).hexdigest()[:10].upper()


ADAPTERS: dict[str, type] = {"mock": MockErpAdapter}


def build(config: dict[str, Any], secret: dict[str, Any]) -> ErpAdapter:
    if config.get("direction", "OUTBOUND") != "OUTBOUND":
        raise IntegrationError(
            "DIRECTION_NOT_SUPPORTED", "Only one-way (outbound) ERP sync is supported.", reconnect=True
        )
    name = config.get("adapter", "")
    cls = ADAPTERS.get(name)
    if cls is None:
        raise IntegrationError("ERP_ADAPTER_NOT_CONFIGURED", "No adapter is available for this ERP.", reconnect=True)
    if getattr(cls, "is_mock", False) and get_settings().app_env in ("staging", "production"):
        raise IntegrationError(
            "ERP_MOCK_NOT_ALLOWED", "The mock ERP cannot be used in staging or production.", reconnect=True
        )
    return cls(config, secret)
