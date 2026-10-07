"""Local infrastructure contract: Redis wake-up queue and S3-compatible object storage.

Skipped (not passed) when the services from infra/local are not running.
"""

import base64
import hashlib
import uuid

import httpx
import pytest
import redis

from app.core.config import get_settings
from app.storage.objects import S3Storage, new_object_key

pytestmark = pytest.mark.infra


@pytest.fixture(scope="module")
def storage():
    s = S3Storage()
    try:
        s.ensure_bucket()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"object storage unreachable: {type(exc).__name__}")
    return s


@pytest.fixture(scope="module")
def rds():
    client = redis.Redis.from_url(get_settings().redis_url, socket_timeout=3)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("redis unreachable")
    return client


def _b64sha(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_redis_wake_queue(rds):
    key = f"prodauto:test:{uuid.uuid4().hex}"
    rds.lpush(key, "1")
    assert rds.brpop([key], timeout=2) == (key.encode(), b"1")
    assert rds.brpop([key], timeout=1) is None


def test_put_get_head_delete(storage):
    key = new_object_key("derived", uuid.uuid4())
    data = b"production note bytes"
    storage.put_bytes(key, data, "text/plain")
    info = storage.head(key)
    assert info is not None and info.size == len(data)
    assert storage.get_bytes(key, max_bytes=1024) == data
    with pytest.raises(ValueError):
        storage.get_bytes(key, max_bytes=5)  # size cap enforced before reading
    storage.delete(key)
    assert storage.head(key) is None


def test_presigned_put_then_get(storage):
    key = new_object_key("quarantine", uuid.uuid4())
    data = b"%PDF-1.7 fake but fine for storage"
    url = storage.presign_put(key, "application/pdf", _b64sha(data))
    r = httpx.put(url, content=data, headers={"Content-Type": "application/pdf",
                                              "x-amz-checksum-sha256": _b64sha(data)})  # fmt: skip
    assert r.status_code == 200, r.text
    got = httpx.get(storage.presign_get(key, download_name="note.pdf"))
    assert got.status_code == 200 and got.content == data
    assert httpx.get(f"{get_settings().storage_endpoint_url}/{storage.bucket}/{key}").status_code in (401, 403)


def test_presigned_put_rejects_bytes_that_do_not_match_signed_checksum(storage):
    key = new_object_key("quarantine", uuid.uuid4())
    declared = b"the bytes the client declared"
    url = storage.presign_put(key, "text/plain", _b64sha(declared))
    r = httpx.put(url, content=b"different bytes", headers={"Content-Type": "text/plain",
                                                            "x-amz-checksum-sha256": _b64sha(declared)})  # fmt: skip
    assert r.status_code >= 400
    assert storage.head(key) is None
