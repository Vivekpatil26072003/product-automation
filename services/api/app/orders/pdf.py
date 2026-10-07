"""One-page PDF of one saved order revision (same template rules as report PDFs: escaped text, embedded font,
reproducible bytes). The PDF is rendered from the stored revision only, so a corrected order produces a new
PDF (new revision number in the file name) and an older revision always renders to the same bytes.
It uses the standard PDF fonts (Helvetica, built into every PDF reader, not embedded) so the file stays about
2 KB and fits EmailJS's request size limit as an attachment. Characters outside that font's Western European
character set are replaced ("₹" -> "Rs", others -> "?") and counted on the page, never dropped silently.
"""

import io
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.reports.pdf import HEAD_BG, INK, MUTED, RULE

FONT, BOLD = "Helvetica", "Helvetica-Bold"
_STYLES = {
    "base": ParagraphStyle("o-base", fontName=FONT, fontSize=10, leading=13, textColor=INK),
    "cell": ParagraphStyle("o-cell", fontName=FONT, fontSize=9.5, leading=12.5, textColor=INK),
    "key": ParagraphStyle("o-key", fontName=BOLD, fontSize=9.5, leading=12.5, textColor=INK),
    "title": ParagraphStyle("o-title", fontName=BOLD, fontSize=18, leading=22, textColor=INK),
    "sub": ParagraphStyle("o-sub", fontName=FONT, fontSize=9, leading=12, textColor=MUTED),
}


class _Text:
    """Escapes markup and keeps only characters the standard font can draw (cp1252)."""

    def __init__(self) -> None:
        self.replaced = 0

    def __call__(self, value: Any) -> str:
        out = []
        for ch in "" if value is None else str(value).replace("₹", "Rs "):
            if ch in "\n\t":
                out.append(" ")
                continue
            try:
                ch.encode("cp1252")
                out.append(ch)
            except UnicodeEncodeError:
                out.append("?")
                self.replaced += 1
        return escape("".join(out))


def _money(v: Any) -> str:
    return "" if v is None else f"{v:,.2f}"


def _qty(v: Any) -> str:
    return "" if v is None else f"{v.normalize():,f}"


def _day(v: Any) -> str:
    return "" if v is None else f"{v.day} {v:%b %Y}"


def file_name(order_ref: str, number: int) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in order_ref)[:60] or "order"
    return f"Order_{safe}_r{number}.pdf"


def render(order: dict[str, Any], rev: Any, company: str) -> bytes:
    """order: {order_ref, department}; rev: an order_revision row."""
    tx, st = _Text(), _STYLES
    P = lambda text, style="base": Paragraph(tx(text), st[style])  # noqa: E731
    rows = [
        ("Customer", rev.customer_name),
        ("Customer number", rev.customer_number),
        ("Email", rev.customer_email),
        ("Mobile", rev.mobile),
        ("Order number", rev.order_number),
        ("Order date", _day(rev.order_date)),
        ("Delivery date", _day(rev.delivery_date)),
        ("Package", rev.package),
        ("Size", rev.size),
        ("Material", rev.material),
        ("Quantity", _qty(rev.quantity)),
        ("Rate", _money(rev.rate)),
        ("Total", _money(rev.total)),
        ("Advance", _money(rev.advance)),
        ("Remaining", _money(rev.remaining)),
        ("Priority", rev.priority),
        ("Remarks", rev.remarks),
        ("Employee", rev.employee),
    ]
    table = Table(
        [[P(k, "key"), P(v, "cell")] for k, v in rows if v not in (None, "")],
        colWidths=[45 * mm, 125 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), HEAD_BG),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story: list[Any] = [
        P(f"Order {order['order_ref']}", "title"),
        P(f"{company} · {order['department']} · revision {rev.number}", "sub"),
        Spacer(1, 6 * mm),
        table,
        Spacer(1, 6 * mm),
    ]
    if tx.replaced:
        story.append(P(f"{tx.replaced} character(s) could not be shown in this font and appear as ?.", "sub"))
    story.append(
        Paragraph(
            tx(f"Saved {rev.created_at:%d %b %Y %H:%M} UTC · reference {order['order_ref']} r{rev.number}"),
            st["sub"],
        )
    )
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=f"Order {order['order_ref']}",
        author=company,
        invariant=1,
        pageCompression=1,
    )
    doc.build(story)
    return buf.getvalue()
