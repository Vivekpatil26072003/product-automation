"""Malware scanning (FR02, FR25). No file leaves quarantine without a verdict.

- ClamdScanner speaks clamd's INSTREAM protocol over TCP.
- DisabledScanner exists only for development/test. It yields scan_status SKIPPED_DEV, which the UI
  shows, and configuration validation refuses it in staging/production.
An unreachable scanner is a transient failure (the job retries); it is never treated as clean.
"""

import socket
import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings

CHUNK = 64 * 1024


@dataclass(frozen=True)
class Verdict:
    status: str  # CLEAN | INFECTED | SKIPPED_DEV
    scanner: str
    signature: str | None = None


class ScannerUnavailable(Exception):
    pass


class MalwareScanner(Protocol):
    def scan(self, data: bytes) -> Verdict: ...


class ClamdScanner:
    def __init__(self, host: str, port: int, timeout: float = 60.0):
        self.host, self.port, self.timeout = host, port, timeout

    def _command(self, payload: bytes, stream: bytes | None = None) -> str:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(payload)
                if stream is not None:
                    for i in range(0, len(stream), CHUNK):
                        chunk = stream[i : i + CHUNK]
                        sock.sendall(struct.pack(">I", len(chunk)) + chunk)
                    sock.sendall(struct.pack(">I", 0))
                reply = b""
                while not reply.endswith(b"\0"):
                    part = sock.recv(4096)
                    if not part:
                        break
                    reply += part
        except OSError as exc:
            raise ScannerUnavailable(type(exc).__name__) from exc
        return reply.rstrip(b"\0").decode("utf-8", "replace").strip()

    def version(self) -> str:
        return self._command(b"zVERSION\0")

    def scan(self, data: bytes) -> Verdict:
        reply = self._command(b"zINSTREAM\0", data)
        name = f"clamav {self.version().split('/')[0].removeprefix('ClamAV ').strip()}"
        if reply.endswith("OK"):
            return Verdict("CLEAN", name)
        if reply.endswith("FOUND"):
            return Verdict("INFECTED", name, reply.removeprefix("stream:").removesuffix("FOUND").strip())
        # "INSTREAM size limit exceeded" and other errors: never assume clean.
        raise ScannerUnavailable(f"scanner error: {reply[:120]}")


class DisabledScanner:
    def scan(self, data: bytes) -> Verdict:
        return Verdict("SKIPPED_DEV", "disabled (development only)")


@lru_cache
def get_scanner() -> MalwareScanner:
    s = get_settings()
    if s.malware_scanner == "clamav":
        return ClamdScanner(s.clamd_host, s.clamd_port)
    return DisabledScanner()
