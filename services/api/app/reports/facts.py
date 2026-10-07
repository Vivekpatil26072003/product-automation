"""Report facts, the deterministic summary and the grounding validator (FR17, addendum A4).

Facts are computed once from the frozen report snapshot. The PDF, the summary, the Excel snapshot and the
email's numeric summary all read the same facts, so they can never disagree.

The summary is a list of sentences, each listing the fact IDs it states. The deterministic template is the
default. Any other text (e.g. an AI paraphrase, or an email the Sender edited) is checked here: every
number must equal a referenced fact, and cause, blame or recommendation language is refused.
"""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.domain.enums import Status

TEMPLATE_VERSION = "report-v1"
STATUS_LABEL = {"RUNNING": "running", "COMPLETED": "completed", "PENDING": "pending", "HOLD": "on hold"}
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


# --- formatting -----------------------------------------------------------------------------------


def group_in(digits: str) -> str:
    """Indian digit grouping, as the web app shows it (en-IN): 4,830 · 1,00,000."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def fmt_number(value: Decimal | int | str, signed: bool = False) -> str:
    """Grouped, trailing zeros dropped (up to three decimals). ASCII minus so text stays copyable."""
    d = Decimal(str(value))
    sign = "-" if d < 0 else ("+" if signed and d > 0 else "")
    d = abs(d).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    whole, _, frac = format(d, "f").partition(".")
    frac = frac.rstrip("0")
    return sign + group_in(whole) + (f".{frac}" if frac else "")


def fmt_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def period_label(date_from: date, date_to: date) -> str:
    return fmt_date(date_from) if date_from == date_to else f"{fmt_date(date_from)} to {fmt_date(date_to)}"


# --- facts ----------------------------------------------------------------------------------------


def build_facts(
    *,
    date_from: date,
    date_to: date,
    timezone: str,
    metrics: dict[str, Any],
    department_count: int,
    excluded_pending: int,
) -> dict[str, Any]:
    return {
        "period": {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "label": period_label(date_from, date_to),
            "timezone": timezone,
        },
        "record_count": metrics["record_count"],
        "department_count": department_count,
        "units": [
            {
                "unit": m["unit"],
                "production": m["production_qty"],
                "target": m["target_qty"],
                "achievement_pct": m["achievement_pct"],
                "variance": m["variance"],
                "record_count": m["record_count"],
            }
            for m in metrics["metrics"]
        ],
        "status_counts": metrics["status_counts"],
        "stop_total_minutes": metrics["stop_total_minutes"],
        "excluded_pending": excluded_pending,
    }


def fact_values(facts: dict[str, Any]) -> dict[str, list[Decimal]]:
    """fact ID -> the numbers a sentence referencing it may state."""
    out: dict[str, list[Decimal]] = {
        "record_count": [Decimal(facts["record_count"])],
        "department_count": [Decimal(facts["department_count"])],
        "stop_total_minutes": [Decimal(facts["stop_total_minutes"])],
        "excluded_pending": [Decimal(facts["excluded_pending"])],
    }
    for key in ("date_from", "date_to"):
        d = date.fromisoformat(facts["period"][key])
        out[f"period.{key}"] = [Decimal(d.day), Decimal(d.month), Decimal(d.year)]
    for s in Status:
        out[f"status.{s.value}"] = [Decimal(facts["status_counts"].get(s.value, 0))]
    for u in facts["units"]:
        p = f"unit.{u['unit']}"
        out[f"{p}.production"] = [Decimal(u["production"])]
        out[f"{p}.target"] = [Decimal(u["target"])]
        out[f"{p}.variance"] = [Decimal(u["variance"]), abs(Decimal(u["variance"]))]
        out[f"{p}.record_count"] = [Decimal(u["record_count"])]
        out[f"{p}.achievement_pct"] = [] if u["achievement_pct"] is None else [Decimal(u["achievement_pct"])]
    return out


# --- deterministic summary ------------------------------------------------------------------------


def template_summary(facts: dict[str, Any]) -> list[dict[str, Any]]:
    period = facts["period"]["label"]
    both = ["period.date_from", "period.date_to"]
    if facts["record_count"] == 0:
        return [
            {
                "text": f"No approved records were found for {period}. Production and target are 0 and "
                "achievement is N/A.",
                "facts": [*both, "record_count"],
            }
        ]
    out: list[dict[str, Any]] = []
    for u in facts["units"]:
        unit, p = u["unit"], f"unit.{u['unit']}"
        prod, target = fmt_number(u["production"]), fmt_number(u["target"])
        if u["achievement_pct"] is None:
            out.append(
                {
                    "text": f"Production for {period} was {prod} {unit}. No target was recorded, so "
                    "achievement is N/A.",
                    "facts": [*both, f"{p}.production", f"{p}.target"],
                }
            )
        else:
            out.append(
                {
                    "text": f"Production for {period} was {prod} {unit} against a target of {target} {unit}, "
                    f"achieving {u['achievement_pct']}%.",
                    "facts": [*both, f"{p}.production", f"{p}.target", f"{p}.achievement_pct"],
                }
            )
            out.append(
                {
                    "text": f"The variance against target is {fmt_number(u['variance'], signed=True)} {unit}.",
                    "facts": [f"{p}.variance"],
                }
            )
    n, d = facts["record_count"], facts["department_count"]
    out.append(
        {
            "text": f"This report includes {n} approved record{'s' if n != 1 else ''} from {d} "
            f"department{'s' if d != 1 else ''}.",
            "facts": ["record_count", "department_count"],
        }
    )
    # Fixed order: stored JSON (jsonb) does not keep key order.
    counts = [(s.value, facts["status_counts"].get(s.value, 0)) for s in Status]
    parts = [f"{c} {STATUS_LABEL[s]}" for s, c in counts if c]
    if parts:
        out.append(
            {
                "text": "Status at the time of recording: " + ", ".join(parts) + ".",
                "facts": [f"status.{s}" for s, c in counts if c],
            }
        )
    out.append(
        {
            "text": f"Record downtime totals {fmt_number(facts['stop_total_minutes'])} minutes (the sum of stop "
            "minutes entered per record; not plant downtime).",
            "facts": ["stop_total_minutes"],
        }
    )
    return out


def email_summary(facts: dict[str, Any]) -> str:
    """The numeric paragraph of the default email body: the template's first sentences."""
    s = template_summary(facts)
    keep = [x["text"] for x in s if not x["text"].startswith(("Status", "Record downtime", "The variance"))]
    return " ".join(keep)


# --- grounding validator -----------------------------------------------------------------------------

NUMBER = re.compile(r"(?<![\w.])[-−+]?\d[\d,]*(?:\.\d+)?")
# Numbers presented as production claims in free text (used for Sender-edited email bodies).
QUANTITY = re.compile(r"(?<![\w.])([-−+]?\d[\d,]*(?:\.\d+)?)\s*(%|m\b|kg\b|pcs\b|minutes?\b|records?\b)", re.I)
UNSUPPORTED = re.compile(
    r"\b(because|due to|caused?|causing|as a result|owing to|reason|attributed|blame|fault|recommend\w*|should|"
    r"must|suggest\w*|advise\w*|likely|probably|improve\w*|expected to)\b",
    re.I,
)
MAX_SENTENCES, MAX_CHARS = 10, 2000


class Ungrounded(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _num(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "").replace("−", "-").lstrip("+"))
    except InvalidOperation:
        return None


def validate_sentences(sentences: Any, facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept only sentences whose every number equals a fact the sentence references. Raises Ungrounded."""
    allowed = fact_values(facts)
    if not isinstance(sentences, list) or not 1 <= len(sentences) <= MAX_SENTENCES:
        raise Ungrounded("BAD_SHAPE", "The summary must have 1 to 10 sentences.")
    if sum(len(str(s.get("text", ""))) for s in sentences if isinstance(s, dict)) > MAX_CHARS:
        raise Ungrounded("TOO_LONG", "The summary is too long.")
    clean = []
    for s in sentences:
        if not isinstance(s, dict) or not isinstance(s.get("text"), str) or not isinstance(s.get("facts"), list):
            raise Ungrounded("BAD_SHAPE", "Each sentence needs text and fact references.")
        text, refs = s["text"].strip(), [str(x) for x in s["facts"]]
        if not text or not refs:
            raise Ungrounded("NO_FACTS", "A sentence does not reference any fact.")
        if unknown := [r for r in refs if r not in allowed]:
            raise Ungrounded("UNKNOWN_FACT", f"Unknown fact reference {unknown[0]}.")
        if m := UNSUPPORTED.search(text):
            raise Ungrounded("UNSUPPORTED_CLAIM", f"Causes, blame or recommendations are not facts ({m.group(0)!r}).")
        values = {v for r in refs for v in allowed[r]}
        for token in NUMBER.findall(text):
            n = _num(token)
            if n is None or (n not in values and abs(n) not in values):
                raise Ungrounded("UNGROUNDED_NUMBER", f"{token} is not one of the referenced facts.")
        clean.append({"text": text, "facts": refs})
    return clean


def unverified_quantities(text: str, facts: dict[str, Any]) -> list[str]:
    """Quantities, percentages, minutes or record counts in free text that match no report fact."""
    values = {v for vs in fact_values(facts).values() for v in vs}
    out = []
    for token, unit in QUANTITY.findall(text):
        n = _num(token)
        if n is None or (n not in values and abs(n) not in values):
            out.append(f"{token}{'' if unit == '%' else ' '}{unit}")
    return out
