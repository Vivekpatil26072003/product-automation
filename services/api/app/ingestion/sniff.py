"""Content inspection during quarantine (FR02, FR25, TC05).

Decides what a file really is from its bytes, never from its name or declared MIME type, and
rejects anything unsafe before any parser sees it: spoofed types, executables, encrypted or legacy
Office files, macros, archive bombs, oversized images and PDFs over the page limit.

Nothing here executes content: no macros, formulas, links or embedded objects.
"""

import io
import zipfile
from dataclasses import dataclass, field

from app.ingestion import limits

_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE compound file: legacy .doc/.xls or encrypted OOXML


class Rejected(Exception):  # noqa: N818 - reads as a verdict
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Inspection:
    detected_type: str  # jpeg | png | pdf | xlsx | docx | txt
    page_count: int
    warnings: list[str] = field(default_factory=list)


_FAMILY = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "pdf": "pdf", "xlsx": "xlsx", "docx": "docx",
           "txt": "txt"}  # fmt: skip


def detect(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:1024].lstrip().startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "zip"
    if data.startswith(_CFB):
        return "cfb"
    if data.startswith((b"MZ", b"\x7fELF", b"#!")) or data[:4] in (b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe"):
        return "executable"
    return "unknown"


def inspect(data: bytes, extension: str) -> Inspection:
    expected = _FAMILY[extension]
    kind = detect(data)

    if kind == "executable":
        raise Rejected("SPOOFED_TYPE", "This file is a program, not a document. It was not processed.")
    if kind == "cfb" and expected in ("xlsx", "docx"):
        raise Rejected("PROTECTED_OR_LEGACY", "This file is password-protected or a legacy .doc/.xls file. "
                                              "Save it as an unprotected .docx or .xlsx and upload again.")  # fmt: skip
    if expected == "txt":
        if kind != "unknown":
            raise Rejected("SPOOFED_TYPE", f"The content does not match the .{extension} extension.")
        return _inspect_text(data)
    if kind == "zip" and expected in ("xlsx", "docx"):
        return _inspect_ooxml(data, expected)
    if kind != expected:
        raise Rejected("SPOOFED_TYPE", f"The content does not match the .{extension} extension.")
    if kind in ("jpeg", "png"):
        return _inspect_image(data, kind)
    return _inspect_pdf(data)


def _inspect_text(data: bytes) -> Inspection:
    if b"\x00" in data:
        raise Rejected("NOT_UTF8_TEXT", "Text files must be UTF-8 text.")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Rejected("NOT_UTF8_TEXT", "Text files must be UTF-8 text.") from exc
    return Inspection("txt", 1)


def _inspect_image(data: bytes, kind: str) -> Inspection:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:  # reads the header only
            width, height = im.size
            if width * height > limits.MAX_IMAGE_PIXELS:
                raise Rejected("IMAGE_TOO_LARGE", "The image has too many pixels (limit 40 megapixels).")
            im.verify()
    except Rejected:
        raise
    except Exception as exc:  # noqa: BLE001 - any decoder failure means a corrupt image
        raise Rejected("CORRUPT_FILE", "The image is damaged and cannot be read.") from exc
    return Inspection(kind, 1)


def _inspect_pdf(data: bytes) -> Inspection:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise Rejected("PROTECTED_OR_LEGACY", "This PDF is password-protected. Upload an unprotected copy.")
        pages = len(reader.pages)
    except Rejected:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise Rejected("CORRUPT_FILE", "The PDF is damaged and cannot be read.") from exc
    if pages < 1:
        raise Rejected("CORRUPT_FILE", "The PDF has no pages.")
    if pages > limits.MAX_PDF_PAGES:
        raise Rejected("TOO_MANY_PAGES", f"The PDF has {pages} pages; the limit is {limits.MAX_PDF_PAGES}.")
    return Inspection("pdf", pages)


def _inspect_ooxml(data: bytes, expected: str) -> Inspection:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError) as exc:
        raise Rejected("CORRUPT_FILE", "The document is damaged and cannot be read.") from exc

    if len(infos) > limits.MAX_OOXML_ENTRIES:
        raise Rejected("ARCHIVE_BOMB", "The document contains too many parts.")
    uncompressed = sum(i.file_size for i in infos)
    compressed = max(sum(i.compress_size for i in infos), 1)
    if uncompressed > limits.MAX_OOXML_UNCOMPRESSED or uncompressed / compressed > limits.MAX_OOXML_RATIO:
        raise Rejected("ARCHIVE_BOMB", "The document expands to an unsafe size.")
    names = {i.filename for i in infos}
    if any(n.startswith("/") or ".." in n.split("/") for n in names):
        raise Rejected("CORRUPT_FILE", "The document contains invalid paths.")
    if any(i.flag_bits & 0x1 for i in infos):
        raise Rejected("PROTECTED_OR_LEGACY", "This document is password-protected. Upload an unprotected copy.")
    if "[Content_Types].xml" not in names:
        raise Rejected("CORRUPT_FILE", "The document is not a valid Office file.")

    main = "xl/workbook.xml" if expected == "xlsx" else "word/document.xml"
    if main not in names:
        raise Rejected("SPOOFED_TYPE", f"The content does not match the .{expected} extension.")
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise Rejected("MACROS_NOT_ALLOWED", "Files with macros are not accepted. Save as .docx/.xlsx without macros.")

    warnings = []
    if any(n.startswith("xl/externalLinks/") for n in names):
        warnings.append("EXTERNAL_LINKS_IGNORED")
    if any("/embeddings/" in n for n in names):
        warnings.append("EMBEDDED_OBJECTS_IGNORED")

    pages = 1
    if expected == "xlsx":
        pages = sum(1 for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if pages < 1:
            raise Rejected("CORRUPT_FILE", "The workbook has no worksheets.")
        if pages > limits.MAX_PDF_PAGES:
            raise Rejected("TOO_MANY_PAGES", f"The workbook has {pages} sheets; the limit is {limits.MAX_PDF_PAGES}.")
    return Inspection(expected, pages, warnings)
