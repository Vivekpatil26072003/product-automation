"""The owner's batch report PDF: company, when, what was processed, customer-wise orders, totals where they are
mathematically valid, what needs attention, and the production entries of the same batch.

Same template rules as the order PDF (app.orders.pdf): escaped text, standard PDF fonts so the file stays small
enough for an EmailJS attachment, and characters outside the font replaced and counted, never dropped silently.
Totals: the order value is the sum of the totals actually written (orders without a total are counted and
named, not guessed); quantities are not summed across different packages.
"""

import io
from decimal import Decimal
from typing import Any

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.orders.pdf import _STYLES, BOLD, _Text
from app.reports.pdf import HEAD_BG, RULE


def _money(v: Any) -> str:
    return "" if v is None else f"{Decimal(v):,.2f}"


def _qty(v: Any) -> str:
    return "" if v is None else f"{Decimal(v).normalize():,f}"


def _day(v: Any) -> str:
    return "" if v is None else f"{v.day} {v:%b %Y}"


def file_name(batch_ref: str, version: int) -> str:
    return f"Diary_Report_{batch_ref}_v{version}.pdf"


def _table(rows: list[list[Any]], widths: list[float], numeric: set[int]) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("FONTNAME", (0, 0), (-1, 0), BOLD),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    style += [("ALIGN", (col, 0), (col, -1), "RIGHT") for col in numeric]
    table.setStyle(TableStyle(style))
    return table


def render(c: dict[str, Any]) -> bytes:
    tx, st = _Text(), _STYLES
    P = lambda text, style="cell": Paragraph(tx(text), st[style])  # noqa: E731
    counts = c["counts"]
    story: list[Any] = [
        P(f"Diary report - {c['company']}", "title"),
        P(
            f"Batch {c['batch_ref']} (report version {c['version']}) · {c['department']} · generated "
            f"{c['generated_at']:%d %b %Y %H:%M} ({c['timezone']})",
            "sub",
        ),
        Spacer(1, 4 * mm),
    ]
    totals = c["totals"]
    facts = [
        [
            "Uploaded",
            f"{c['uploaded_at']:%d %b %Y %H:%M} by {c['uploaded_by'] or 'unknown'} · {c['files']} file(s), "
            f"{c['pages']} page(s)",
        ],
        [
            "Diary entries processed",
            f"{counts['orders_approved'] + counts['entries_approved']} approved, "
            f"{counts['orders_rejected'] + counts['entries_rejected']} rejected, "
            f"{counts['orders_waiting'] + counts['entries_waiting']} still waiting",
        ],
        ["Customers / orders", f"{c['customer_count']} customer(s), {c['order_count']} order(s)"],
        [
            "Total order value",
            (_money(totals["total"]) if totals["total"] is not None else "Not available")
            + (
                f" ({totals['orders_without_total']} order(s) without a written total, not included)"
                if totals["orders_without_total"]
                else ""
            ),
        ],
        ["Advance received / remaining", f"{_money(totals['advance'])} / {_money(totals['remaining'])}"],
        ["Production entries", str(len(c["records"]))],
    ]
    story.append(
        _table([[P("Summary", "key"), P("")]] + [[P(k, "key"), P(v)] for k, v in facts], [60 * mm, 200 * mm], set())
    )

    if c["customers"]:
        story += [Spacer(1, 5 * mm), P("Orders by customer", "title")]
        head = ["Order", "Order date", "Delivery", "Package", "Quantity", "Rate", "Total", "Status"]
        widths = [32 * mm, 24 * mm, 24 * mm, 70 * mm, 22 * mm, 22 * mm, 26 * mm, 40 * mm]
        for g in c["customers"]:
            rows = [[P(h, "key") for h in head]]
            for x in g["orders"]:
                rows.append(
                    [
                        P(f"{x['ref']} r{x['revision']}"),
                        P(_day(x["order_date"])),
                        P(_day(x["delivery_date"])),
                        P(x["package"]),
                        P(_qty(x["quantity"])),
                        P(_money(x["rate"])),
                        P(_money(x["total"])),
                        P(x["status"]),
                    ]
                )
                if x["extra"]:
                    rows.append(
                        [
                            P(""),
                            P("Also noted: " + "; ".join(f"{e['label']}: {e['value']}" for e in x["extra"]), "sub"),
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                        ]
                    )
            title = g["name"] + (f" · {g['mobile']}" if g["mobile"] else "")
            subtotal = f"Customer total: {_money(g['total'])}" if g["has_total"] else "No totals written"
            table = _table(rows, widths, {4, 5, 6})
            spans = [("SPAN", (1, i), (-1, i)) for i, r in enumerate(rows) if r[2] == ""]
            if spans:
                table.setStyle(TableStyle(spans))
            story.append(KeepTogether([Spacer(1, 3 * mm), P(title, "key"), table, P(subtotal, "sub")]))

    if c["attention"]:
        story += [Spacer(1, 5 * mm), P("Needs attention (missing or uncertain information)", "title")]
        story.append(
            _table(
                [[P("Order", "key"), P("Customer", "key"), P("What", "key")]]
                + [[P(a["ref"]), P(a["customer"]), P(a["text"])] for a in c["attention"]],
                [32 * mm, 60 * mm, 168 * mm],
                set(),
            )
        )

    if c["records"]:
        story += [Spacer(1, 5 * mm), P("Production entries", "title")]
        head = ["Date", "Department", "Machine", "Operator", "Production", "Target", "Status"]
        rows = [[P(h, "key") for h in head]] + [
            [
                P(_day(r["production_date"])),
                P(r["department"]),
                P(r["machine"]),
                P(r["operator_name"]),
                P(f"{_qty(r['production_qty'])} {r['unit']}"),
                P(f"{_qty(r['target_qty'])} {r['unit']}"),
                P(r["status"]),
            ]
            for r in c["records"]
        ]
        story.append(_table(rows, [26 * mm, 45 * mm, 30 * mm, 50 * mm, 36 * mm, 36 * mm, 37 * mm], {4, 5}))

    story.append(Spacer(1, 6 * mm))
    if tx.replaced:
        story.append(
            P(
                f"{tx.replaced} character(s) (for example Gujarati or Hindi script) cannot be printed in this "
                "PDF font and appear as ?. The saved records keep them exactly; open the order in the "
                "application to read them.",
                "sub",
            )
        )
    story.append(
        P(
            f"Values are the saved, reviewed records at the time of this report ({c['batch_ref']} v{c['version']}).",
            "sub",
        )
    )
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"Diary report {c['batch_ref']}",
        author=c["company"],
        invariant=1,
        pageCompression=1,
    )
    doc.build(story)
    return buf.getvalue()


def render_preview(c: dict[str, Any]) -> bytes:
    """The diary data of one batch as a table, right after reading. Rows not yet reviewed say so on every row
    and in the title, so this file is never mistaken for checked records."""
    tx, st = _Text(), _STYLES
    P = lambda text, style="cell": Paragraph(tx(text), st[style])  # noqa: E731
    title = "Diary data" if c["reviewed"] else "Diary data - DRAFT, not all values reviewed yet"
    story: list[Any] = [
        P(f"{title} - {c['company']}", "title"),
        P(
            f"Batch {c['batch_ref']} · {len(c['rows'])} order(s) · created {c['generated_at']:%d %b %Y %H:%M} "
            f"({c['timezone']})",
            "sub",
        ),
        Spacer(1, 4 * mm),
    ]
    head = ["Page", "Customer", "Mobile", "Order date", "Delivery", "Package", "Qty", "Rate", "Total", "Status"]
    rows = [[P(h, "key") for h in head]]
    for r in c["rows"]:
        rows.append(
            [
                P(r["order_ref"] or f"p{r['page']}"),
                P(r["customer"]),
                P(r["mobile"]),
                P(r["order_date"]),
                P(r["delivery_date"]),
                P(r["package"]),
                P(r["quantity"]),
                P(r["rate"]),
                P(r["total"]),
                P(r["status"]),
            ]
        )
        if r["details"]:
            rows.append([P(""), P(r["details"], "sub"), "", "", "", "", "", "", "", ""])
    table = _table(
        rows, [24 * mm, 38 * mm, 26 * mm, 22 * mm, 22 * mm, 46 * mm, 16 * mm, 20 * mm, 24 * mm, 30 * mm], {6, 7, 8}
    )
    spans = [("SPAN", (1, i), (-1, i)) for i, r in enumerate(rows) if r[2] == ""]
    if spans:
        table.setStyle(TableStyle(spans))
    story.append(table)
    story.append(Spacer(1, 4 * mm))
    if not c["reviewed"]:
        story.append(
            P(
                "Rows marked 'To review' show the values as read from the diary; they have not been checked "
                "yet and may contain reading mistakes.",
                "sub",
            )
        )
    if tx.replaced:
        story.append(
            P(
                f"{tx.replaced} character(s) (for example Gujarati or Hindi script) cannot be printed in this "
                "PDF font and appear as ?.",
                "sub",
            )
        )
    buf = io.BytesIO()
    SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"Diary data {c['batch_ref']}",
        author=c["company"],
        invariant=1,
        pageCompression=1,
    ).build(story)
    return buf.getvalue()
