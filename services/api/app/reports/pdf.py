"""PDF rendering of a frozen report snapshot (FR16, spec §12 "PDF and email content").

- Input is only the stored report (snapshot metadata, metrics, facts, summary, items); never live data.
- Output is byte-for-byte reproducible (reportlab invariant mode): a retried render of the same snapshot
  yields the same checksum.
- Strict template: all text is escaped before it reaches reportlab's paragraph markup, so notes or remarks
  can never inject markup. Fonts are embedded (Bitstream Vera, shipped with reportlab). Characters the font
  cannot show are replaced with "?" and counted on the last page, never dropped silently.
- Page order: title and identity; period, time zone, snapshot time, data version, scope; production vs
  target per unit; departments; status; record downtime; summary; optional detail; exclusions and references.
"""

import io
import os
from typing import Any
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.reports.facts import STATUS_LABEL, fmt_number

FONT, BOLD = "ReportSans", "ReportSans-Bold"
INK, MUTED, RULE, HEAD_BG = (
    colors.HexColor("#172033"),
    colors.HexColor("#526174"),
    colors.HexColor("#D5DDE8"),
    colors.HexColor("#EEF2F7"),
)
_font_dir = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
_registered = False


def _fonts() -> TTFont:
    global _registered
    if not _registered:
        pdfmetrics.registerFont(TTFont(FONT, os.path.join(_font_dir, "Vera.ttf")))
        pdfmetrics.registerFont(TTFont(BOLD, os.path.join(_font_dir, "VeraBd.ttf")))
        _registered = True
    return pdfmetrics.getFont(FONT)


class _Text:
    """Escapes text and replaces characters the embedded font cannot draw."""

    def __init__(self) -> None:
        self.cmap = _fonts().face.charToGlyph
        self.replaced = 0

    def __call__(self, value: Any) -> str:
        out = []
        for ch in "" if value is None else str(value):
            if ch in "\n\t":
                out.append(" ")
            elif ord(ch) in self.cmap:
                out.append(ch)
            else:
                out.append("?")
                self.replaced += 1
        return escape("".join(out))


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName=FONT, fontSize=9.5, leading=13, textColor=INK)
    return {
        "base": base,
        "title": ParagraphStyle("title", parent=base, fontName=BOLD, fontSize=18, leading=22, spaceAfter=2),
        "sub": ParagraphStyle("sub", parent=base, textColor=MUTED, fontSize=9),
        "h2": ParagraphStyle(
            "h2", parent=base, fontName=BOLD, fontSize=12, leading=16, spaceBefore=10, spaceAfter=4, keepWithNext=1
        ),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.5, leading=11),
        "cellr": ParagraphStyle("cellr", parent=base, fontSize=8.5, leading=11, alignment=2),
        "head": ParagraphStyle("head", parent=base, fontName=BOLD, fontSize=8.5, leading=11),
        # Record detail: smaller type so dates, names and headings fit without breaking inside words.
        "dcell": ParagraphStyle("dcell", parent=base, fontSize=7.5, leading=9.5),
        "dcellr": ParagraphStyle("dcellr", parent=base, fontSize=7.5, leading=9.5, alignment=2),
        "dhead": ParagraphStyle("dhead", parent=base, fontName=BOLD, fontSize=7.5, leading=9.5),
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=11, textColor=MUTED),
    }


def _table(rows: list[list[Any]], widths: list[float], numeric_from: int) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1)  # headings repeat on every page
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"),
            ]
        )
    )
    return t


class _NumberedCanvas(rl_canvas.Canvas):
    """Adds "Page n of N" and the report identity to every page."""

    footer = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pages: list[dict[str, Any]] = []

    def showPage(self) -> None:  # noqa: N802 - reportlab API
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont(FONT, 7.5)
            self.setFillColor(MUTED)
            self.drawString(18 * mm, 10 * mm, self.footer)
            self.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def attachment_name(report: dict[str, Any]) -> str:
    return f"Production_{report['date_from']}_{report['code']}_v{report['version']}.pdf"


def render(report: dict[str, Any]) -> bytes:
    """report: {id, code, version, title, date_from, date_to, timezone, snapshot_at, data_version, departments,
    record_count, metrics, facts, summary, summary_source, include_detail, items, excluded_pending}."""
    tx, st = _Text(), _styles()
    P = lambda text, style="base": Paragraph(tx(text), st[style])  # noqa: E731
    facts, metrics = report["facts"], report["metrics"]
    story: list[Any] = [
        P(report["title"], "title"),
        P(f"Report {report['code']} · version {report['version']} · ID {report['id']}", "sub"),
        Spacer(1, 6),
    ]
    meta = [
        ("Period", facts["period"]["label"]),
        ("Time zone", report["timezone"]),
        ("Snapshot taken", report["snapshot_at"]),
        ("Data version", str(report["data_version"])),
        ("Scope", ", ".join(report["departments"]) or "None"),
        ("Records included", f"{report['record_count']} approved"),
    ]
    story.append(
        _table(
            [[P("Report details", "head"), P("", "head")]] + [[P(k, "cell"), P(v, "cell")] for k, v in meta],
            [45 * mm, 129 * mm],
            2,
        )
    )

    story.append(P("Production against target", "h2"))
    if not metrics["metrics"]:
        story.append(P("No approved records in this period. Production 0, target 0, achievement N/A."))
    else:
        head = ["Unit", "Production", "Target", "Achievement", "Variance", "Records"]
        rows = [[P(h, "head") for h in head]]
        for m in metrics["metrics"]:
            ach = "N/A" if m["achievement_pct"] is None else f"{m['achievement_pct']}%"
            rows.append(
                [P(m["unit"], "cell")]
                + [
                    P(v, "cellr")
                    for v in (
                        fmt_number(m["production_qty"]),
                        fmt_number(m["target_qty"]),
                        ach,
                        fmt_number(m["variance"], signed=True),
                        str(m["record_count"]),
                    )
                ]
            )
        story.append(_table(rows, [20 * mm, 32 * mm, 32 * mm, 30 * mm, 32 * mm, 28 * mm], 1))
        story.append(P("Units are reported separately; m, kg and pcs are never added together.", "small"))

    if metrics["departments"]:
        story.append(P("Departments", "h2"))
        head = ["Department", "Unit", "Production", "Target", "Achievement", "Variance", "Records"]
        rows = [[P(h, "head") for h in head]]
        for d in metrics["departments"]:
            ach = "N/A" if d["achievement_pct"] is None else f"{d['achievement_pct']}%"
            rows.append(
                [P(d["department_name"], "cell"), P(d["unit"], "cell")]
                + [
                    P(v, "cellr")
                    for v in (
                        fmt_number(d["production_qty"]),
                        fmt_number(d["target_qty"]),
                        ach,
                        fmt_number(d["variance"], signed=True),
                        str(d["record_count"]),
                    )
                ]
            )
        story.append(_table(rows, [38 * mm, 12 * mm, 24 * mm, 24 * mm, 28 * mm, 26 * mm, 22 * mm], 2))

    story.append(P("Status", "h2"))
    total = metrics["record_count"]
    rows = [[P(h, "head") for h in ("Status", "Records", "Share")]]
    for s in STATUS_LABEL:  # fixed order; stored JSON does not keep key order
        n = metrics["status_counts"].get(s, 0)
        share = "N/A" if not total else f"{metrics['status_shares'][s]}%"
        rows.append([P(STATUS_LABEL.get(s, s).capitalize(), "cell"), P(str(n), "cellr"), P(share, "cellr")])
    story.append(_table(rows, [60 * mm, 30 * mm, 30 * mm], 1))

    story.append(P("Record downtime", "h2"))
    story.append(
        P(
            f"{fmt_number(metrics['stop_total_minutes'])} minutes: the sum of stop minutes entered on each "
            "record. Stops on different machines may overlap; this is not plant downtime."
        )
    )

    story.append(P("Summary", "h2"))
    for s in report["summary"]:
        story.append(P(s["text"]))
    source = {
        "TEMPLATE": "Written from the report facts by a fixed template.",
        "AI": "AI-assisted wording; every number was checked against the report facts.",
        "TEMPLATE_FALLBACK": "Written from the report facts by a fixed template (AI wording was not used).",
    }
    story.append(P(source.get(report["summary_source"], ""), "small"))

    if report["include_detail"] and report["items"]:
        story.append(P("Records", "h2"))
        head = ["Date", "Department", "Machine", "Operator", "Production", "Target", "Status", "Stop (min)", "Remarks"]
        rows = [[P(h, "dhead") for h in head]]
        for it in report["items"]:
            f = it["fields"]
            rows.append(
                [
                    P(f["production_date"], "dcell"),
                    P(f["department_name"], "dcell"),
                    P(f["machine_code"], "dcell"),
                    P(f["operator_name"], "dcell"),
                    P(f"{fmt_number(f['production_qty'])} {f['unit']}", "dcellr"),
                    P(f"{fmt_number(f['target_qty'])} {f['unit']}", "dcellr"),
                    P(STATUS_LABEL.get(f["status"], "").capitalize(), "dcell"),
                    P(str(f["stop_minutes"]), "dcellr"),
                    P(f.get("remarks") or "", "dcell"),
                ]
            )
        widths = [19, 23, 16, 19, 20, 18, 18, 13, 28]
        detail = _table(rows, [w * mm for w in widths], 4)
        detail.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (6, 0), (6, -1), 8),
                ]
            )
        )
        story.append(detail)

    story.append(P("Exclusions and references", "h2"))
    notes = [
        "Only approved records are included, each at its approved revision when the snapshot was taken. "
        "Archived records and entries not yet approved are excluded.",
        f"Entries waiting for review in this period and scope at snapshot time: {report['excluded_pending']}.",
        "If a record in this period is corrected, added or archived later, this report is marked outdated and a new "
        "version must be generated; this file is never changed.",
        f"Record and revision references are listed in the Excel snapshot of report {report['code']} "
        f"version {report['version']}.",
    ]
    for n in notes:
        story.append(P(n, "small"))
    if tx.replaced:
        story.append(P(f"{tx.replaced} character(s) could not be shown in this font and appear as '?'.", "small"))

    buf = io.BytesIO()
    canvas_cls = type(
        "ReportCanvas", (_NumberedCanvas,), {"footer": f"{report['title']} · {report['code']} v{report['version']}"}
    )
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=report["title"],
        author="Production Automation",
        subject=f"{report['code']} v{report['version']}",
        creator="Production Automation",
        invariant=1,
    )
    doc.build(story, canvasmaker=canvas_cls)
    return buf.getvalue()
