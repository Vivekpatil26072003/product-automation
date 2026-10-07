"""EmailJS template variables describing the diary data itself, shared by order emails and owner reports.

Besides the project's own variables (subject, message, pdf_file, ...) every email carries the data as:
- the single-order names an order template typically uses: order_number, customer_name, order_date,
  delivery_date, quantity, rate, total, additional_details, title, name, email;
- the whole table: orders_text (plain lines, for {{orders_text}}) and orders_html (an HTML table, for
  {{{orders_html}}} - triple braces so EmailJS does not escape it), plus order_count.
When an email covers several orders, the single-order names hold the first order only when there is exactly
one; otherwise order_number is the report reference, total the sum of written totals and the others are empty,
and every order is in additional_details / orders_text / orders_html. All values are text, never null.
"""

from decimal import Decimal
from html import escape
from typing import Any

COLUMNS = [
    ("ref", "Order"),
    ("customer", "Customer"),
    ("mobile", "Mobile"),
    ("order_date", "Order date"),
    ("delivery_date", "Delivery"),
    ("package", "Package"),
    ("quantity", "Quantity"),
    ("rate", "Rate"),
    ("total", "Total"),
]


def _day(v: Any) -> str:
    return f"{v.day} {v:%b %Y}" if v else ""


def _money(v: Any) -> str:
    return f"{Decimal(v):,.2f}" if v is not None else ""


def _qty(v: Any) -> str:
    return f"{Decimal(v).normalize():,f}" if v is not None else ""


def row(ref: str, rev: Any) -> dict[str, str]:
    """One order (an order_revision-like object) as display text."""
    return {
        "ref": ref,
        "customer": rev.customer_name or "",
        "mobile": rev.mobile or "",
        "order_date": _day(rev.order_date),
        "delivery_date": _day(rev.delivery_date),
        "package": " ".join(x for x in (rev.package, rev.size, rev.material) if x),
        "quantity": _qty(rev.quantity),
        "rate": _money(rev.rate),
        "total": _money(rev.total),
        "details": "; ".join(
            [
                f"{label}: {value}"
                for label, value in (
                    ("Priority", rev.priority),
                    ("Payment", rev.payment_status),
                    ("Production", rev.production_status),
                    ("Delivery status", rev.delivery_status),
                    ("Advance", _money(rev.advance) if rev.advance is not None else None),
                    ("Remaining", _money(rev.remaining) if rev.remaining is not None else None),
                    ("Remarks", rev.remarks),
                )
                if value
            ]
            + [f"{x['label']}: {x['value']}" for x in (rev.extra or [])]
        ),
    }


def variables(
    rows: list[dict[str, str]],
    *,
    title: str,
    reference: str,
    to_email: str,
    company: str,
    totals: Decimal | None = None,
) -> dict[str, str]:
    one = rows[0] if len(rows) == 1 else None
    lines = [
        " | ".join(f"{label}: {r[key]}" for key, label in COLUMNS if r[key])
        + (f" | {r['details']}" if r["details"] else "")
        for r in rows
    ]
    head = "".join(
        f'<th style="text-align:left;padding:4px 8px;border-bottom:1px solid #ccc">{escape(label)}</th>'
        for _, label in COLUMNS
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td style="padding:4px 8px;border-bottom:1px solid #eee">{escape(r[key])}</td>' for key, _ in COLUMNS
        )
        + "</tr>"
        for r in rows
    )
    if totals is None:
        written = [Decimal(r["total"].replace(",", "")) for r in rows if r["total"]]
        totals = sum(written, Decimal(0)) if written else None
    return {
        "title": title,
        "name": company,
        "email": to_email,
        "order_number": one["ref"] if one else reference,
        "customer_name": one["customer"]
        if one
        else ", ".join(dict.fromkeys(r["customer"] for r in rows if r["customer"])),
        "order_date": one["order_date"] if one else "",
        "delivery_date": one["delivery_date"] if one else "",
        "quantity": one["quantity"] if one else "",
        "rate": one["rate"] if one else "",
        "total": one["total"] if one else (_money(totals) if totals is not None else ""),
        "additional_details": (one["details"] if one else "\n".join(lines)) or "",
        "order_count": str(len(rows)),
        "orders_text": "\n".join(lines),
        "orders_html": f'<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>",
    }
