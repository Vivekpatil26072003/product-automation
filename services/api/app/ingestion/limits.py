"""Upload limits and manifest validation (spec U1, FR02, TC04).

The UI shows these exact numbers; the server enforces them independently. Declared MIME types are
recorded but never trusted: the content type is derived from the extension, and the real type is
sniffed from the bytes during scanning.
"""

import re
import unicodedata
from dataclasses import dataclass

from app.core.errors import Issue

MAX_FILES = 20
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BATCH_BYTES = 100 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_OFFICE_ROWS = 10_000
MAX_IMAGE_PIXELS = 40_000_000
MAX_OOXML_UNCOMPRESSED = 200 * 1024 * 1024
MAX_OOXML_RATIO = 100
MAX_OOXML_ENTRIES = 10_000
UPLOAD_TTL_HOURS = 24
PIPELINE_VERSION = "ingest-1"

# extension -> canonical content type used for storage
CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
}
ACCEPTED_EXTENSIONS = tuple(CONTENT_TYPES)

LIMITS_PUBLIC = {
    "max_files": MAX_FILES,
    "max_file_bytes": MAX_FILE_BYTES,
    "max_batch_bytes": MAX_BATCH_BYTES,
    "max_pdf_pages": MAX_PDF_PAGES,
    "max_office_rows": MAX_OFFICE_ROWS,
    "accepted_extensions": [f".{e}" for e in ACCEPTED_EXTENSIONS],
}

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def display_name(raw: str) -> str:
    """Keep only the final path component, drop control characters, cap at 255 characters.

    The name is shown to people only. Storage keys are always generated (spec §13).
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in unicodedata.normalize("NFC", name) if unicodedata.category(c)[0] != "C")
    return name.strip()[:255]


def extension_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


@dataclass(frozen=True)
class DeclaredFile:
    name: str
    bytes: int
    sha256: str
    mime: str = ""


def validate_manifest(files: list[DeclaredFile]) -> list[Issue]:
    """Per-file and batch-level issues. Field paths are files.<index>.<field> so the UI can
    show the reason next to each file."""
    issues: list[Issue] = []
    if not files:
        return [Issue("NO_FILES", "Add at least one file.", "files")]
    if len(files) > MAX_FILES:
        issues.append(Issue("TOO_MANY_FILES", f"Select at most {MAX_FILES} files (you selected {len(files)}).",
                            "files"))  # fmt: skip
    total = 0
    for i, f in enumerate(files):
        where = f"files.{i}"
        name = display_name(f.name)
        ext = extension_of(name)
        if not name:
            issues.append(Issue("INVALID_NAME", "The file needs a name.", f"{where}.name"))
        elif ext not in CONTENT_TYPES:
            issues.append(Issue("UNSUPPORTED_TYPE",
                                f'"{name}" is not accepted. Use {", ".join(LIMITS_PUBLIC["accepted_extensions"])}.',
                                f"{where}.name"))  # fmt: skip
        if f.bytes <= 0:
            issues.append(Issue("EMPTY_FILE", f'"{name}" is empty.', f"{where}.bytes"))
        elif f.bytes > MAX_FILE_BYTES:
            issues.append(Issue("FILE_TOO_LARGE", f'"{name}" is larger than 20 MiB.', f"{where}.bytes"))
        if not _SHA256_HEX.match(f.sha256 or ""):
            issues.append(Issue("BAD_CHECKSUM", "sha256 must be 64 lower-case hex characters.", f"{where}.sha256"))
        total += max(f.bytes, 0)
    if total > MAX_BATCH_BYTES:
        issues.append(Issue("BATCH_TOO_LARGE", "The files together are larger than 100 MiB.", "files"))
    return issues
