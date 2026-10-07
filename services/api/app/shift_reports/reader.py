"""Reads daily-sheet values from page text lines (typed notes, a text PDF of the sheet, or OCR lines of a photo).

Each line that starts with a known row label ("Production in Meters 52104 52949 52919") gives that row's shift
values. Section headings ("NORMAL FIBC", "Downtime", "PRASHANT", ...) decide which table a repeated label belongs
to. Rules, so that numbers are never put in the wrong cell silently:
- Three numbers -> shifts I, II, III. A leading number equal to the row's target is the target, not shift I.
- When a total follows the three shift values on the line, it is used to confirm them (sum, or average for
  average rows); a total that does not match makes the values uncertain.
- One or two numbers only -> filled in order and marked uncertain (which shift is not certain).
- Day rows (target / actual / to date) take the actual value.
- Values are copied as written; calculated rows (theoretical picks, efficiencies, ...) are not read: they are
  calculated from the written values.
- A page written for one shift ("Shift : 1" in the header, or "(Shift 1)" after a heading) puts each single value
  in that shift. Two "label : value" pairs on one line (two columns) are read as two rows.
- Calculated values the worker also wrote (efficiencies, loss of pick, speed, totals) are returned in
  written_calc so the sheet can compare them with its own calculation and highlight the values behind a mismatch.
Nothing found -> the page is not a daily sheet (other readers handle it).
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.dates import parse_production_date
from app.shift_reports.catalog import BY_KEY, SECTIONS, Section

MIN_ROWS = 5
_DIGITS = str.maketrans("૦૧૨૩૪૫૬૭૮૯०१२३४५६७८९", "01234567890123456789")
_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")
_ENUM = re.compile(r"^\s*(?:\(\s*[a-l]\s*\)|[a-l]\s*\)|\(\s*\d\s*\)|\d+\s*[.)])\s*", re.IGNORECASE)
_DATE = re.compile(
    r"\b(\d{1,2}[-/.](?:\d{1,2}|[a-z]{3,9})[-/.]\d{2,4}|\d{1,2}\s+[a-z]{3,9}\.?\s+\d{2,4})\b", re.IGNORECASE
)
# "Shift I supervisor: Ramesh", "I shift incharge - Ramesh", "Supervisor II: Suresh", "Shift 3 : Mahesh"
_SUPERVISOR = (
    re.compile(
        r"^(?:shift\s*)?(?P<shift>iii|ii|i|1st|2nd|3rd|1|2|3)\s*(?:shift\s*)?(?:supervisor|incharge|in-charge)"
        r"\s*[:=\-]\s*(?P<name>[a-z].{0,60})$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:supervisor|incharge|in-charge)\s*(?:shift\s*)?(?P<shift>iii|ii|i|1|2|3)\s*[:=\-]\s*"
        r"(?P<name>[a-z].{0,60})$",
        re.IGNORECASE,
    ),
    re.compile(r"^shift\s*(?P<shift>iii|ii|i|1|2|3)\s*[:=\-]\s*(?P<name>[a-z][a-z .]{1,60})$", re.IGNORECASE),
)
_PAGE_SHIFT = re.compile(r"^shift\s*(?:no\.?)?\s*[:=\-]?\s*(?P<shift>iii|ii|i|1st|2nd|3rd|1|2|3)\s*$", re.IGNORECASE)
_HEADING_SHIFT = re.compile(r"\(?\s*shift\s*[:\-]?\s*(?P<shift>iii|ii|i|1|2|3)\s*\)?\s*$", re.IGNORECASE)
_SHIFT_OF = {"i": "I", "1": "I", "1st": "I", "ii": "II", "2": "II", "2nd": "II", "iii": "III", "3": "III", "3rd": "III"}
_NOTE = re.compile(r"(b/f\s*=|new\s*=|b/g\s*=|no beam\s*=|smm/maint\s*=|no spare\s*=|m/c\s*no)", re.IGNORECASE)
_DAY_PAIRS = ("low_running", "mech_detail")


@dataclass
class Cell:
    section: str
    metric: str
    shift: str
    value: Decimal
    raw: str
    span_ids: list[str]
    uncertain: bool = False
    note: str | None = None
    confidence: float | None = None


@dataclass
class SheetReading:
    cells: list[Cell] = field(default_factory=list)
    report_date: date | None = None
    date_raw: str | None = None
    date_span: str | None = None
    supervisors: dict[str, str] = field(default_factory=dict)
    notes: list[dict[str, Any]] = field(default_factory=list)
    rows: int = 0
    page_shift: str | None = None
    written_calc: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_sheet(self) -> bool:
        return self.rows >= MIN_ROWS


def _norm(text: str) -> str:
    return " ".join(text.translate(_DIGITS).replace(" ", " ").split())


def _numbers(text: str) -> list[Decimal]:
    text = re.sub(r"\([^)]*\)", " ", text)  # "( 54 kgs)", "(14.86 picks/hrs ...)" are part of the label
    text = re.sub(r"\([^)]*$", " ", text)  # a bracket the line break left open: part of the label too
    out = []
    for m in _NUM.finditer(text):
        try:
            out.append(Decimal(m.group().replace(",", "")))
        except InvalidOperation:
            continue
    return out


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(Decimal("0.02"), abs(b) * Decimal("0.005"))


def _total(vals: list[Decimal], agg: str) -> Decimal:
    s = sum(vals, Decimal(0))
    return s / len(vals) if agg == "avg" else s


def _labels(sections: list[Section]) -> list[tuple[str, Section, Any]]:
    """(label text, section, metric), longest first. Calculated rows are matched too, so their numbers are never
    taken for another row; they are only kept as a cross-check (written_calc)."""
    out = []
    for s in sections:
        for m in s.metrics:
            syns = m.synonyms if m.kind == "derived" else (*m.synonyms, m.label.lower())
            for syn in syns:
                out.append((syn.lower(), s, m))
    return sorted(out, key=lambda x: -len(x[0]))


def _is_input(m: Any) -> bool:
    return m is not None and m.kind == "input"


def _any_input_match(label: str) -> bool:
    return any((h := _match(label, k)) is not None and _is_input(h[1]) for k in _LABELS)


_LABELS = {s.key: _labels([s]) for s in SECTIONS}
_DAY_LABELS = _labels([BY_KEY[k] for k in _DAY_PAIRS])


def _match(label_text: str, section_key: str) -> tuple[Section, Any, str] | None:
    for syn, s, m in _LABELS[section_key]:
        if label_text.startswith(syn):
            return s, m, label_text[len(syn) :]
    return None


class _Context:
    """Which table the following lines belong to."""

    def __init__(self) -> None:
        self.section = "sulzer"
        self.warping = False
        self.unnamed_warping = False  # "Warping production" without Prashant / Hacoba

    def heading(self, low: str) -> bool:
        numbers = len(_numbers(low))
        if numbers > 2 or (numbers and _any_input_match(low)):
            return False
        if low.startswith("warping") and "wastage" not in low and "manpower" not in low:
            self.warping, self.section, self.unnamed_warping = True, "warping_prashant", True
            return True
        if self.warping and low.startswith("wastage"):
            self.section = "warping_wastage"
            return True
        if self.warping and (low.startswith("manpower") or low.startswith("man power")):
            self.section = "warping_manpower"
            return True
        best = max(
            ((len(h), s) for s in SECTIONS for h in s.headings if low.startswith(h)), default=None, key=lambda x: x[0]
        )
        if best is None:
            return False
        self.section = best[1].key
        self.warping = best[1].group == "Warping"
        self.unnamed_warping = False
        return True


def read_sheet(
    page: dict[str, Any], targets: dict[tuple[str, str], Decimal | None], date_order: str = "DMY"
) -> SheetReading:
    out = SheetReading()
    ctx = _Context()
    seen: set[tuple[str, str, str]] = set()
    for text, ids, conf in _lines(page):
        clean = _norm(text)
        low = clean.lower()
        if not low:
            continue
        dm = _DATE.search(low) if out.report_date is None else None
        if dm and (dm.start() == 0 or len(_numbers(_DATE.sub(" ", low))) <= 3):
            parsed = parse_production_date(dm.group(1), date(9999, 12, 31), date_order)
            if parsed.value is not None:
                today = date.today()
                if (parsed.value - today).days > 31 or (today - parsed.value).days > 1100:
                    # Probably misread (e.g. "2026" read as "2066"): never taken as the sheet's date.
                    out.notes.append(
                        {
                            "label": "Date not used",
                            "text": f'Date read as "{dm.group(1)}" '
                            f"({parsed.value:%d %b %Y}) looks wrong; confirm the date.",
                            "span_ids": ids,
                        }
                    )
                else:
                    out.report_date, out.date_raw, out.date_span = parsed.value, dm.group(1), ids[0]
                continue
        sm = next((x for rx in _SUPERVISOR if (x := rx.match(clean))), None)
        if sm:
            out.supervisors[_SHIFT_OF[sm.group("shift").lower()]] = sm.group("name").strip()[:80]
            continue
        if _NOTE.search(low):
            out.notes.append({"label": _note_label(low, ctx.section), "text": clean[:300], "span_ids": ids})
            continue
        if ps := _PAGE_SHIFT.match(low):
            out.page_shift = _SHIFT_OF[ps.group("shift").lower()]
            continue
        label = _ENUM.sub("", low)
        if (hs := _HEADING_SHIFT.search(label)) and len(_numbers(label[: hs.start()])) == 0:
            out.page_shift = out.page_shift or _SHIFT_OF[hs.group("shift").lower()]
            label = label[: hs.start()].strip()
        if ctx.heading(label):
            continue
        if out.page_shift:  # one shift per page: OCR's "52, 104" is one number, not two shifts
            label = re.sub(r"(?<![\d.])(\d{1,3}),\s+(\d{3})(?![\d.,])", r"\1,\2", label)
        for seg in _segments(label, ctx):
            _read_segment(out, seen, ctx, seg, text, ids, conf, targets)
    return out


def _segments(label: str, ctx: "_Context") -> list[str]:
    """Two columns on one line ("Production (mtr) : 52,104   Production (kg) : 8,919") are two rows: split where
    another row label of the same table starts after a number and is itself followed by a number. Text inside
    brackets, units ("8500 kg.") and the same row label again never start a second row."""
    head = _match(label, ctx.section)
    if head is None:
        return [label]
    if head[0].key in _DAY_PAIRS:
        return [label]  # day tables share lines differently: _day_segments splits them
    depth, seen_number = 0, False
    for i, ch in enumerate(label):
        depth += (ch == "(") - (ch == ")")
        if depth == 0 and ch.isdigit() and i >= len(label) - len(head[2]):
            seen_number = True
        if not (seen_number and depth == 0 and ch.isalpha() and i and not label[i - 1].isalnum()):
            continue
        tail = label[i:]
        if _NUM.search(re.sub(r"\([^)]*\)?", " ", tail)) is None:
            break  # a unit after the number ("1,855 kg"), not a second column
        nxt = _match(tail, ctx.section)
        if nxt is not None and nxt[1] is not head[1]:
            return [label[:i].strip(), *_segments(tail, ctx)]
    return [label]


def _read_segment(
    out: "SheetReading",
    seen: set,
    ctx: "_Context",
    label: str,
    text: str,
    ids: list[str],
    conf: float | None,
    targets: dict,
) -> None:
    hit = _match(label, ctx.section)
    if hit is None:
        hit = _unique_match(label)
        if hit is not None:  # printed sheets do not always keep a heading above its rows
            ctx.section, ctx.warping = hit[0].key, hit[0].group == "Warping"
            ctx.unnamed_warping = False
    if hit is not None and hit[0].key in _DAY_PAIRS:
        for s, m, rest in _day_segments(label):
            _assign(out, seen, s, m, _numbers(rest), text, ids, conf, targets)
        return
    if hit is None:
        return
    s, m, rest = hit
    nums = _numbers(rest)
    if not _is_input(m):
        out.rows += 1  # a calculated row recognised: it confirms the table; its value is only a cross-check
        if len(nums) == 1:
            out.written_calc.append(
                {
                    "section": s.key,
                    "metric": m.key,
                    "shift": out.page_shift or "I",
                    "value": nums[0],
                    "label": m.label,
                    "span_ids": ids,
                    "shift_known": out.page_shift is not None,
                }
            )
        return
    if True:
        if s.key in ("warping_prashant", "warping_hacoba") and len(nums) >= 8:
            _warping_pair(out, seen, m, nums, text, ids, conf, targets)
            return
        used = _assign(
            out,
            seen,
            s,
            m,
            nums,
            text,
            ids,
            conf,
            targets,
            page_shift=out.page_shift,
            machine_unknown=ctx.unnamed_warping and s.key == "warping_prashant",
        )
        if s.key == "sulzer" and used and len(nums) > used + 2:
            gc = BY_KEY["ground_cover"].metric(m.key)
            if gc is not None:  # the printed sheet has the ground-cover columns on the same line
                _assign(
                    out,
                    seen,
                    BY_KEY["ground_cover"],
                    gc,
                    nums[used + 2 :],
                    text,
                    ids,
                    conf,
                    targets,
                    require_total=True,
                )


def _unique_match(label: str) -> tuple[Section, Any, str] | None:
    """A label found in exactly one table (Prashant/Hacoba and the two day tables count as one)."""
    hits = {k: h for k in _LABELS if (h := _match(label, k)) is not None and _is_input(h[1])}
    if {"warping_prashant", "warping_hacoba"} <= set(hits):
        hits.pop("warping_hacoba")
    if {"low_running", "mech_detail"} <= set(hits):
        hits.pop("mech_detail")
    return next(iter(hits.values())) if len(hits) == 1 else None


def _lines(page: dict[str, Any]) -> list[tuple[str, list[str], float | None]]:
    spans = page.get("spans", [])
    if spans and any(s.get("cell") for s in spans):  # spreadsheet cells: rebuild rows
        rows: dict[tuple, list[dict]] = {}
        for s in spans:
            m = re.match(r"^([A-Z]+)(\d+)$", s.get("cell") or "")
            if m:
                rows.setdefault((s.get("sheet"), int(m[2])), []).append(s | {"_col": m[1]})
        out = []
        for key in sorted(rows, key=lambda k: (str(k[0]), k[1])):
            cells = sorted(rows[key], key=lambda c: (len(c["_col"]), c["_col"]))
            out.append((" ".join(c["text"] for c in cells), [c["id"] for c in cells], None))
        return out
    return [(s["text"], [s["id"]], s.get("confidence")) for s in spans]


def _note_label(low: str, section: str) -> str:
    if "b/f" in low:
        return "Beam fall m/c no."
    if low.startswith("new") or "new=" in low.replace(" ", ""):
        return "New machine started"
    if "b/g" in low or "no beam" in low or "smm" in low or "no spare" in low:
        return "Stop machine for starting"
    if "m/c no" in low:
        return {"downtime": "Bad beam m/c no."}.get(section, "Machine numbers")
    return "Other"


def _day_segments(label: str) -> list[tuple[Section, Any, str]]:
    """Day tables may share a line ("SMM 0.80 1.00 0.50 MAINT 1.36 1.36 1.34"): split at each known label."""
    hits: list[tuple[int, int, Section, Any]] = []
    i = 0
    while i < len(label):
        for syn, s, m in _DAY_LABELS:
            if label.startswith(syn, i) and (i == 0 or not label[i - 1].isalnum()):
                hits.append((i, i + len(syn), s, m))
                i += len(syn)
                break
        else:
            i += 1
    out = []
    for n, (_start, end, s, m) in enumerate(hits):
        rest = label[end : hits[n + 1][0] if n + 1 < len(hits) else len(label)]
        if _is_input(m):
            out.append((s, m, rest))
    return out


def _put(
    out: SheetReading,
    seen: set,
    s: Section,
    m: Any,
    shift: str,
    v: Decimal,
    text: str,
    ids: list[str],
    conf: float | None,
    uncertain: bool,
    note: str | None,
) -> None:
    key = (s.key, m.key, shift)
    if key in seen:  # the first occurrence on a page wins; a second one is a conflict to review
        for c in out.cells:
            if (c.section, c.metric, c.shift) == key and c.value != v:
                c.uncertain, c.note = True, f"The page also shows {v} for this cell."
        return
    seen.add(key)
    out.cells.append(Cell(s.key, m.key, shift, v, text.strip()[:200], ids, uncertain, note, conf))


def _assign(
    out: SheetReading,
    seen: set,
    s: Section,
    m: Any,
    nums: list[Decimal],
    text: str,
    ids: list[str],
    conf: float | None,
    targets: dict,
    require_total: bool = False,
    page_shift: str | None = None,
    machine_unknown: bool = False,
) -> int:
    """Put a row's numbers into its cells; returns how many leading numbers were used (0 = none)."""
    if not nums:
        return 0
    target = targets.get((s.key, m.key))
    if page_shift and s.shifts != ("D",) and len(nums) == 1:
        note = "The page does not say whether this warping machine is Prashant or Hacoba." if machine_unknown else None
        _put(out, seen, s, m, page_shift, nums[0], text, ids, conf, machine_unknown, note)
        out.rows += 1
        return 1
    if s.shifts == ("D",):
        if len(nums) >= 2 and target is not None and _close(nums[0], target):
            _put(out, seen, s, m, "D", nums[1], text, ids, conf, False, None)
            out.rows += 1
            return 2
        unsure = len(nums) > 1
        _put(
            out,
            seen,
            s,
            m,
            "D",
            nums[0],
            text,
            ids,
            conf,
            unsure,
            "More than one number on this line; check which is today's value." if unsure else None,
        )
        out.rows += 1
        return 1
    offset = None
    for off in (0, 1):  # a written total right after the three shifts tells where they are
        sums_up = len(nums) >= off + 4 and _close(_total(nums[off : off + 3], m.agg), nums[off + 3])
        target_first = off == 0 and target is not None and _close(nums[0], target) and len(nums) >= 5
        if sums_up and not target_first:
            offset, confirmed = off, True
            break
    if offset is None:
        if require_total:
            return 0
        confirmed = False
        offset = 1 if len(nums) >= 4 and target is not None and _close(nums[0], target) else 0
    vals = nums[offset : offset + 3]
    if len(vals) == 3:
        mismatch = len(nums) > offset + 3 and not confirmed
        note = f"The written total {nums[offset + 3]} does not match these shift values." if mismatch else None
        if page_shift and not confirmed:
            mismatch = True
            note = f"This page is for shift {page_shift}, but this line has several numbers; check each value."
        positive = [v for v in vals if v > 0]
        if not confirmed and len(positive) >= 2 and max(positive) > 25 * min(positive):
            mismatch = True  # e.g. OCR split "52, 104" into 52 / 104: shift values are never that far apart
            note = "These shift values are very different from each other; check them against the photo."
        for sh, v in zip(s.shifts, vals, strict=True):
            _put(out, seen, s, m, sh, v, text, ids, conf, mismatch, note)
        out.rows += 1
        return offset + 3
    for sh, v in zip(s.shifts, vals, strict=False):
        _put(
            out,
            seen,
            s,
            m,
            sh,
            v,
            text,
            ids,
            conf,
            True,
            f"Only {len(vals)} value(s) on this line; check which shift they belong to.",
        )
    out.rows += 1
    return offset + len(vals)


def _warping_pair(
    out: SheetReading,
    seen: set,
    m: Any,
    nums: list[Decimal],
    text: str,
    ids: list[str],
    conf: float | None,
    targets: dict,
) -> None:
    """Printed warping rows hold Prashant and Hacoba side by side: [target] I II III total todate I II III total."""
    p = _assign(out, seen, BY_KEY["warping_prashant"], m, nums, text, ids, conf, targets)
    if p and len(nums) >= p + 5:
        _assign(out, seen, BY_KEY["warping_hacoba"], m, nums[p + 2 :], text, ids, conf, targets, require_total=True)
