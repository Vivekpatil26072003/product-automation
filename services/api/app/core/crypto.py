"""Encryption of integration credentials at rest (spec §13 "Secrets", FR22).

AES-256-GCM with keys from INTEGRATION_KEYS ("id:base64key,id2:base64key"; the first key encrypts, all keys
decrypt, so keys can be rotated without re-entering secrets). The connection ID is bound as associated data,
so a ciphertext copied onto another connection does not decrypt. Plaintext secrets exist only in memory
while a worker calls the provider; they are never returned by the API or written to logs.

Production should load INTEGRATION_KEYS from the managed secret store, not a file.
"""

import base64
import json
import os
import uuid
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings


class SecretsUnavailable(Exception):
    """No encryption key is configured, or the ciphertext cannot be decrypted with the known keys."""


@lru_cache
def _keys() -> list[tuple[str, bytes]]:
    raw = get_settings().integration_keys
    if not raw:
        return []
    keys = []
    for part in raw.split(","):
        key_id, _, b64 = part.strip().partition(":")
        key = base64.b64decode(b64)
        if not key_id or len(key) != 32:
            raise SecretsUnavailable("INTEGRATION_KEYS entries must be id:base64(32 bytes)")
        keys.append((key_id, key))
    return keys


def configured() -> bool:
    try:
        return bool(_keys())
    except (SecretsUnavailable, ValueError):
        return False


def encrypt(connection_id: uuid.UUID, secret: dict[str, Any]) -> tuple[bytes, str]:
    keys = _keys()
    if not keys:
        raise SecretsUnavailable("no integration encryption key is configured")
    key_id, key = keys[0]
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(secret).encode(), str(connection_id).encode())
    return nonce + ciphertext, key_id


def decrypt(connection_id: uuid.UUID, blob: bytes, key_id: str) -> dict[str, Any]:
    key = dict(_keys()).get(key_id)
    if key is None:
        raise SecretsUnavailable(f"encryption key {key_id!r} is not available")
    try:
        plain = AESGCM(key).decrypt(bytes(blob[:12]), bytes(blob[12:]), str(connection_id).encode())
    except Exception as exc:  # noqa: BLE001 - never reveal why decryption failed
        raise SecretsUnavailable("stored credentials could not be decrypted") from exc
    return json.loads(plain)


def new_key() -> str:
    """Helper for operators: a fresh key entry for INTEGRATION_KEYS."""
    return f"k{uuid.uuid4().hex[:6]}:{base64.b64encode(os.urandom(32)).decode()}"
