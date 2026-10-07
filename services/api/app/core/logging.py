"""Safe logging: never log secrets, full documents or signed URLs (spec §13, FR25)."""

import logging
import re

_SIGNED_URL = re.compile(r"https?://\S*(X-Amz-Signature|Signature=|sig=)\S*", re.IGNORECASE)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_KV = re.compile(r"(?i)((?:secret|password|token|api[_-]?key)\s*[=:]\s*)\S+")


def redact(text: str) -> str:
    text = _SIGNED_URL.sub("[redacted-signed-url]", text)
    text = _BEARER.sub(r"\1[redacted]", text)
    return _SECRET_KV.sub(r"\1[redacted]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.getMessage()))
        record.args = ()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.addFilter(RedactingFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
