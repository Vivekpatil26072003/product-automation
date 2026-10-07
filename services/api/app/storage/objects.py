"""S3-compatible object storage (MinIO locally).

- Object keys are generated: <area>/<tenant_id>/<uuid>. User filenames never become path segments.
- Areas separate quarantine, originals, derived and generated artifacts so service policies can be
  least-privilege per prefix.
- Signed URLs live at most 5 minutes and must never be logged (spec §10, §13).
"""

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings

Area = Literal["quarantine", "originals", "derived", "reports", "exports"]
_AREAS: tuple[str, ...] = ("quarantine", "originals", "derived", "reports", "exports")


def new_object_key(area: Area, tenant_id: uuid.UUID) -> str:
    if area not in _AREAS:
        raise ValueError(f"unknown storage area {area!r}")
    return f"{area}/{tenant_id}/{uuid.uuid4()}"


def object_key_for(area: Area, tenant_id: uuid.UUID, object_id: uuid.UUID, suffix: str = "") -> str:
    """Deterministic key for an object derived from a known ID, so retries overwrite instead of duplicating."""
    if area not in _AREAS:
        raise ValueError(f"unknown storage area {area!r}")
    if "/" in suffix or ".." in suffix:
        raise ValueError("suffix must not contain path separators")
    return f"{area}/{tenant_id}/{object_id}{suffix}"


def key_belongs_to(key: str, tenant_id: uuid.UUID) -> bool:
    parts = key.split("/")
    return len(parts) == 3 and parts[0] in _AREAS and parts[1] == str(tenant_id)


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None
    sha256_b64: str | None


class ObjectStorage(Protocol):
    def ensure_bucket(self) -> None: ...
    def put_bytes(self, key: str, data: bytes, content_type: str) -> None: ...
    def get_bytes(self, key: str, max_bytes: int) -> bytes: ...
    def head(self, key: str) -> ObjectInfo | None: ...
    def delete(self, key: str) -> None: ...
    def list_keys(self, prefix: str) -> list[str]: ...
    def presign_put(self, key: str, content_type: str, sha256_b64: str) -> str: ...
    def presign_get(self, key: str, download_name: str | None = None) -> str: ...
    def ping(self) -> None: ...


class S3Storage:
    def __init__(self) -> None:
        s = get_settings()
        self.bucket = s.storage_bucket
        self.ttl = s.signed_url_ttl_seconds
        self._client = boto3.client(
            "s3",
            endpoint_url=s.storage_endpoint_url,
            aws_access_key_id=s.storage_access_key_id,
            aws_secret_access_key=s.storage_secret_access_key,
            region_name=s.storage_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=30),
        )  # fmt: skip

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)
        origins = get_settings().storage_cors_origins
        if origins:
            # Browsers PUT directly to signed URLs, so the bucket must allow those origins.
            self._client.put_bucket_cors(
                Bucket=self.bucket,
                CORSConfiguration={"CORSRules": [{
                    "AllowedOrigins": origins, "AllowedMethods": ["PUT", "GET", "HEAD"],
                    "AllowedHeaders": ["content-type", "x-amz-checksum-sha256", "x-amz-sdk-checksum-algorithm"],
                    "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 600,
                }]},
            )  # fmt: skip
        # Private bucket: no public ACLs or policies are ever applied.

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                                ChecksumAlgorithm="SHA256")  # fmt: skip

    def get_bytes(self, key: str, max_bytes: int) -> bytes:
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(key) from exc
            raise
        if obj["ContentLength"] > max_bytes:
            raise ValueError("object exceeds the permitted size")
        return obj["Body"].read(max_bytes + 1)[:max_bytes]

    def head(self, key: str) -> ObjectInfo | None:
        try:
            h = self._client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectInfo(h["ContentLength"], h.get("ContentType"), h.get("ChecksumSHA256"))

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def list_keys(self, prefix: str) -> list[str]:
        """All keys under a prefix (retention purge of derived page files)."""
        keys: list[str] = []
        for page in self._client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return keys

    def presign_put(self, key: str, content_type: str, sha256_b64: str) -> str:
        # The signed checksum binds the upload to the declared bytes; the server re-verifies anyway.
        return self._client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
                "ChecksumSHA256": sha256_b64,
            },  # fmt: skip
            ExpiresIn=self.ttl,
        )

    def presign_get(self, key: str, download_name: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        if download_name:
            safe = "".join(c for c in download_name if c.isalnum() or c in "._- ")[:120] or "download"
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        return self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=self.ttl)

    def ping(self) -> None:
        self._client.head_bucket(Bucket=self.bucket)


@lru_cache
def get_storage() -> ObjectStorage:
    return S3Storage()
