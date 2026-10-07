"""Credential encryption and Sheets row mapping (FR13, FR22) without a database or network."""

import base64
import os
import uuid

import pytest

from app.core import crypto
from app.core.config import get_settings
from app.integrations.base import IntegrationError, check
from app.integrations.sheets import COLUMNS, row_values


@pytest.fixture
def keys(monkeypatch):
    def use(value: str) -> None:
        monkeypatch.setattr(get_settings(), "integration_keys", value)
        crypto._keys.cache_clear()

    yield use
    crypto._keys.cache_clear()


def _key(name: str) -> str:
    return f"{name}:{base64.b64encode(os.urandom(32)).decode()}"


def test_round_trip_is_bound_to_the_connection(keys):
    keys(_key("a"))
    cid = uuid.uuid4()
    blob, key_id = crypto.encrypt(cid, {"client_secret": "s3cret"})
    assert b"s3cret" not in blob and key_id == "a"
    assert crypto.decrypt(cid, blob, key_id) == {"client_secret": "s3cret"}
    with pytest.raises(crypto.SecretsUnavailable):
        crypto.decrypt(uuid.uuid4(), blob, key_id)
    with pytest.raises(crypto.SecretsUnavailable):
        crypto.decrypt(cid, blob[:-1] + bytes([blob[-1] ^ 1]), key_id)


def test_key_rotation_keeps_old_secrets_readable(keys):
    old = _key("old")
    keys(old)
    cid = uuid.uuid4()
    blob, key_id = crypto.encrypt(cid, {"x": 1})
    keys(_key("new") + "," + old)
    assert crypto.decrypt(cid, blob, key_id) == {"x": 1}
    assert crypto.encrypt(cid, {"x": 1})[1] == "new"


def test_missing_key_refuses_instead_of_storing_plaintext(keys):
    keys("")
    assert not crypto.configured()
    with pytest.raises(crypto.SecretsUnavailable):
        crypto.encrypt(uuid.uuid4(), {"x": 1})


def test_row_values_keep_exact_quantities_and_column_order():
    rec = {"record_id": "r1", "revision": 2, "production_date": "2026-09-27", "department_name": "Tapeline",
           "operator_name": "A", "machine_code": "TL-1", "production_qty": "1250.000", "target_qty": "0.100",
           "unit": "m", "status": "RUNNING", "stop_minutes": 5, "remarks": "=SUM(A1)", "state": "ACTIVE",
           "approved_at": "t", "updated_at": "u", "department_id": "d", "machine_id": "m"}  # fmt: skip
    row = row_values(rec)
    assert len(row) == len(COLUMNS)
    assert row[COLUMNS.index("production_qty")] == 1250.0
    assert row[COLUMNS.index("remarks")] == "=SUM(A1)"  # sent with valueInputOption=RAW, stays text


@pytest.mark.parametrize(("status", "flag"), [(401, "reconnect"), (403, "reconnect"), (404, "reconnect"),
                                              (429, "transient"), (503, "transient"), (400, None)])  # fmt: skip
def test_provider_answers_map_to_the_contract(status, flag):
    import httpx

    with pytest.raises(IntegrationError) as exc:
        check(httpx.Response(status, headers={"Retry-After": "3"}), "Call")
    if flag:
        assert getattr(exc.value, flag)
    else:
        assert not (exc.value.transient or exc.value.reconnect)
    if status == 429:
        assert exc.value.retry_after == 3
