"""Quarantine inspection, manifest limits and native parsers (FR02, FR04, FR25; TC03–TC05, TC09)."""

import pytest

from app.ingestion import limits, parsers, sandbox
from app.ingestion.limits import DeclaredFile, validate_manifest
from app.ingestion.sniff import Rejected, inspect
from tests import filegen

SHA = "a" * 64


# --- manifest limits (TC04) ------------------------------------------------------------------


def test_valid_manifest():
    assert validate_manifest([DeclaredFile("note.JPG", 1000, SHA), DeclaredFile("r.xlsx", 5, SHA)]) == []


@pytest.mark.parametrize(
    ("files", "code", "field"),
    [
        ([DeclaredFile(f"f{i}.txt", 1, SHA) for i in range(21)], "TOO_MANY_FILES", "files"),
        ([DeclaredFile("big.pdf", limits.MAX_FILE_BYTES + 1, SHA)], "FILE_TOO_LARGE", "files.0.bytes"),
        ([DeclaredFile(f"f{i}.pdf", 19 * 1024 * 1024, SHA) for i in range(6)], "BATCH_TOO_LARGE", "files"),
        ([DeclaredFile("legacy.doc", 10, SHA)], "UNSUPPORTED_TYPE", "files.0.name"),
        ([DeclaredFile("macro.xlsm", 10, SHA)], "UNSUPPORTED_TYPE", "files.0.name"),
        ([DeclaredFile("photo.heic", 10, SHA)], "UNSUPPORTED_TYPE", "files.0.name"),
        ([DeclaredFile("empty.txt", 0, SHA)], "EMPTY_FILE", "files.0.bytes"),
        ([DeclaredFile("x.txt", 5, "nothex")], "BAD_CHECKSUM", "files.0.sha256"),
        ([], "NO_FILES", "files"),
    ],
)
def test_manifest_limits(files, code, field):
    issues = validate_manifest(files)
    assert (code, field) in {(i.code, i.field) for i in issues}


def test_display_name_never_carries_a_path():
    assert limits.display_name("C:\\Users\\x\\..\\note.pdf") == "note.pdf"
    assert limits.display_name("../../etc/passwd\x00.txt") == "passwd.txt"


# --- inspection (TC05) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "ext", "kind", "pages"),
    [
        (filegen.txt(), "txt", "txt", 1),
        (filegen.xlsx(), "xlsx", "xlsx", 2),
        (filegen.docx(), "docx", "docx", 1),
        (filegen.pdf(text_pages=2, image_pages=1), "pdf", "pdf", 3),
        (filegen.png(), "png", "png", 1),
        (filegen.jpeg(), "jpg", "jpeg", 1),
    ],
    ids=["txt", "xlsx", "docx", "pdf", "png", "jpg"],
)
def test_accepted_formats(data, ext, kind, pages):  # TC03
    info = inspect(data, ext)
    assert (info.detected_type, info.page_count) == (kind, pages)


@pytest.mark.parametrize(
    ("data", "ext", "code"),
    [
        (filegen.executable_as_jpg(), "jpg", "SPOOFED_TYPE"),
        (filegen.pdf(), "png", "SPOOFED_TYPE"),
        (filegen.docx(), "xlsx", "SPOOFED_TYPE"),
        (filegen.png(), "txt", "SPOOFED_TYPE"),
        (filegen.corrupt_pdf(), "pdf", "CORRUPT_FILE"),
        (filegen.encrypted_pdf(), "pdf", "PROTECTED_OR_LEGACY"),
        (filegen.legacy_cfb(), "docx", "PROTECTED_OR_LEGACY"),
        (filegen.macro_xlsx(), "xlsx", "MACROS_NOT_ALLOWED"),
        (filegen.zip_bomb_docx(), "docx", "ARCHIVE_BOMB"),
        (filegen.huge_image_header(), "png", "IMAGE_TOO_LARGE"),
        (filegen.not_utf8_txt(), "txt", "NOT_UTF8_TEXT"),
        (filegen.pdf(text_pages=51), "pdf", "TOO_MANY_PAGES"),
    ],
    ids=lambda v: v if isinstance(v, str) else "file",
)
def test_unsafe_or_unsupported_content_is_rejected(data, ext, code):
    with pytest.raises(Rejected) as exc:
        inspect(data, ext)
    assert exc.value.code == code


# --- native parsers (TC09) -------------------------------------------------------------------


def test_txt_spans_have_exact_offsets():
    (page,) = parsers.parse_txt(filegen.txt())
    assert page["parser"] == "native-txt" and not page["needs_ocr"]
    assert [s["text"] for s in page["spans"]] == filegen.NOTE_LINES
    for s in page["spans"]:
        assert page["text"][s["char_start"] : s["char_end"]] == s["text"]
    assert page["spans"][3]["line"] == 4


def test_xlsx_keeps_cells_and_never_evaluates_formulas():
    pages = parsers.parse_xlsx(filegen.xlsx(formula_without_cache=True))
    assert len(pages) == 2
    cells = {s["cell"]: s for s in pages[0]["spans"]}
    assert cells["E2"]["text"] == "1250" and cells["E3"]["text"] == "980.5"  # no float noise
    assert cells["D2"]["text"] == "T-04" and cells["D2"]["sheet"] == "Production"
    assert "FORMULA_WITHOUT_CACHED_VALUE" in pages[0]["warnings"]
    assert cells["J2"]["issue"] == "FORMULA_WITHOUT_CACHED_VALUE" and cells["J2"]["text"] == ""
    hyperlink = pages[1]["spans"]
    assert all("evil" not in s["text"] or s.get("formula_cached") is None for s in hyperlink)
    for s in pages[0]["spans"]:
        assert pages[0]["text"][s["char_start"] : s["char_end"]] == s["text"]


def test_xlsx_row_limit():
    with pytest.raises(parsers.ParseError) as exc:
        parsers.parse_xlsx(filegen.xlsx(rows=limits.MAX_OFFICE_ROWS))
    assert exc.value.code == "ROW_LIMIT"


def test_docx_paragraphs_and_table_cells():
    (page,) = parsers.parse_docx(filegen.docx())
    tables = [s for s in page["spans"] if "table" in s]
    assert [(s["row"], s["col"], s["text"]) for s in tables][3:] == [(2, 1, "Tapeline"), (2, 2, "1250"), (2, 3, "1500")]
    # Injected instructions are just text: they appear as data, never as a command.
    assert any("Ignore previous instructions" in s["text"] for s in page["spans"])


def test_pdf_text_pages_parse_natively_and_image_pages_need_ocr():
    pages = parsers.parse_pdf(filegen.pdf(text_pages=2, image_pages=1))
    assert [p["needs_ocr"] for p in pages] == [False, False, True]
    assert "Production 1250 m" in pages[0]["text"]
    assert all(s["polygon"] is None for s in pages[0]["spans"])  # page-level evidence, no invented boxes


def test_image_normalization_applies_exif_orientation_and_strips_metadata():
    from io import BytesIO

    from PIL import Image

    out = parsers.normalize_image(filegen.jpeg())
    with Image.open(BytesIO(out)) as im:
        assert im.size == (300, 400)  # 400x300 rotated by EXIF orientation 6
        assert not im.getexif()


def test_sandbox_runs_parsers_in_a_separate_process():
    (page,) = sandbox.run("txt", filegen.txt())
    assert page["spans"][0]["text"] == "Date 27/09/2026"
    with pytest.raises(Rejected):
        sandbox.run("inspect", filegen.executable_as_jpg(), "jpg")
    with pytest.raises(parsers.ParseError) as exc:
        sandbox.run("xlsx", b"not a workbook")
    assert exc.value.code == "PARSE_FAILED"
