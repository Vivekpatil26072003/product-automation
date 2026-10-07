"""Reads a pick reading register page (WGS-02) from text lines: typed notes, a transcription, or OCR lines.

The page is recognised by its printed heading ("HOURLY PRODUCTION READING REGISTER", "PICK - READING", "WGS-02") or
by a row of column times with machine rows under it. The column times decide the shift (24-00 02-00 ... = III).

Machine rows: the machine number, then one cell per time. Cells are best separated by "|":
    27 | 2230 | 2282 24 | 2342 28 | 2386 18 | 2444 27
    26 | B.fall | - | | |
    32 | 1815 S/C | | 1823 8 | 1845 22 | 1869 24
A cell holds the meter reading, then the picks written under it ("2282 24" or "2282/24"), or a stop mark (B.fall,
B.F, S/C, ...), possibly next to a reading. "-" or an empty cell is nothing written. The first time is the start
reading (no picks). Without "|" the numbers are paired as reading + picks; when the pairing is not certain the
cells are marked for a person to check. "Total | 687 | 583 | 590 | 603 | 646" gives the totals the worker wrote.
Values are copied as written; machine totals, column totals and stopped machines are calculated, not read.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.dates import parse_production_date
from app.pick_registers.compute import rollover_diff
from app.pick_registers.layout import machine_key, shift_of_times, status_code, time_key
from app.shift_reports.reader import _DATE, _lines

MIN_ROWS = 3
_DIGITS = str.maketrans("૦૧૨૩૪૫૬૭૮૯०१२३४५६७८९", "01234567890123456789")
_HEADING = re.compile(
    r"hourly\s+production\s+reading|pick\s*[-–]?\s*reading|\bwgs\s*[-–]?\s*0?2\b|f\s*/\s*wgs\s*/", re.IGNORECASE
)
_SHIFT = re.compile(r"\bshift\s*(?:no\.?)?\s*[:=\-]?\s*(iii|ii|i|1|2|3)\b", re.IGNORECASE)
_SHIFT_OF = {"i": "I", "1": "I", "ii": "II", "2": "II", "iii": "III", "3": "III"}
_TIME = re.compile(r"\b\d{1,2}\s*[-:.]\s*00\b")
_PAIR = re.compile(r"^(\d+(?:\.\d+)?)\s*/\s*\(?(\d+(?:\.\d+)?)\)?$")
_EMPTY = {"", "-", "--", "—", "–", "/", "\\", "x", "nil"}
_MARK_WORDS = re.compile(r"\b(b)\s*[.,/]?\s*(f(?:all|al|m)?)\b\.?", re.IGNORECASE)


@dataclass
class RegCell:
    machine: str
    slot: int
    reading: Decimal | None
    picks: Decimal | None
    status: str | None
    raw: str
    span_ids: list[str]
    uncertain: bool = False
    note: str | None = None
    confidence: float | None = None
    unverified: bool = False  # read from the image only; cleared when its readings confirm it


@dataclass
class RegisterReading:
    heading: bool = False
    shift: str | None = None
    times: list[str] = field(default_factory=list)
    cells: list[RegCell] = field(default_factory=list)
    totals: dict[int, tuple[Decimal, str, list[str]]] = field(default_factory=dict)
    register_date: date | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)
    rows: int = 0

    @property
    def is_register(self) -> bool:
        return self.heading or (self.shift is not None and self.rows >= MIN_ROWS)


def _num(text: str) -> Decimal | None:
    t = text.strip().strip("().,;:?").translate(_DIGITS)
    if not re.fullmatch(r"\d{1,9}(?:\.\d{1,4})?", t):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def page_date(raw: str, date_order: str = "DMY") -> tuple[date | None, str | None]:
    """(the date, or None with the reason) for a date written on a page; implausible dates are never used."""
    parsed = parse_production_date(raw, date(9999, 12, 31), date_order)
    if parsed.value is None:
        return None, None
    today = date.today()
    if (parsed.value - today).days > 31 or (today - parsed.value).days > 1100:
        return None, f'Date read as "{raw}" ({parsed.value:%d %b %Y}) looks wrong; confirm the date.'
    return parsed.value, None


def _date(low: str, ids: list[str], out: RegisterReading, date_order: str) -> None:
    dm = _DATE.search(low)
    if not dm:
        return
    day, why = page_date(dm.group(1), date_order)
    if why:
        out.notes.append({"label": "Date not used", "text": why, "span_ids": ids})
    out.register_date = day


def read_register(page: dict[str, Any], date_order: str = "DMY") -> RegisterReading:
    out = RegisterReading()
    for text, ids, conf in _lines(page):
        clean = " ".join(text.translate(_DIGITS).split())
        low = clean.lower()
        if not low:
            continue
        if _HEADING.search(low):
            out.heading = True
        if out.register_date is None and "date" in low:
            _date(low, ids, out, date_order)
            if out.register_date is not None:
                continue
        times = _TIME.findall(clean)
        if len(times) >= 3:
            keys = [time_key(x) for x in times]
            shift = shift_of_times([k for k in keys if k])
            if shift:
                out.shift, out.times = shift, [k for k in keys if k]
            else:
                out.notes.append({"label": "Times", "text": f'Column times "{clean[:120]}" do not fit one shift.',
                                  "span_ids": ids})  # fmt: skip
            continue
        sm = _SHIFT.search(low)
        if sm and out.shift is None and not re.match(r"^\s*\d", low):
            out.shift = _SHIFT_OF[sm.group(1).lower()]
            continue
        if re.match(r"^\s*total\b", low):
            _totals(clean, ids, out)
            continue
        if re.match(r"^\s*m\s*/\s*c\s*stop", low):
            continue  # stopped machines are counted from the marks
        if out.shift is None and not out.heading:
            continue
        m = re.match(r"^\s*\|?\s*([^\s|]+)\s*(.*)$", clean)
        machine = machine_key(m.group(1)) if m else None
        if machine is None or not m.group(2).strip(" |"):
            if out.heading and not _HEADING.search(low) and re.search(r"[a-z]{3}", low) and "m/c" not in low:
                out.notes.append({"label": "Page", "text": clean[:300], "span_ids": ids})
            continue
        cells = _row(machine, m.group(2), ids, conf)
        if cells:
            out.cells.extend(cells)
            out.rows += 1
    return out


def _totals(clean: str, ids: list[str], out: RegisterReading) -> None:
    body = re.sub(r"^\s*total\s*[:\-]?\s*", "", clean, flags=re.IGNORECASE)
    if "|" in body:
        parts = [p.strip() for p in body.strip().strip("|").split("|")]
        found = {slot: (v, p, ids) for slot, p in enumerate(parts[:5]) if (v := _num(p)) is not None}
        if sorted(found) == [0, 1, 2, 3]:  # four totals with no empty first cell: they are the four picks columns
            found = {slot + 1: t for slot, t in found.items()}
        out.totals.update(found)
        return
    nums = [v for v in (_num(x) for x in body.split()) if v is not None]
    if len(nums) == 5:
        out.totals.update({i: (v, str(v), ids) for i, v in enumerate(nums)})
    elif len(nums) == 4:
        out.totals.update({i + 1: (v, str(v), ids) for i, v in enumerate(nums)})
    elif nums:
        out.notes.append({"label": "Totals", "text": f"Totals written: {body[:200]} (columns not certain).",
                          "span_ids": ids})  # fmt: skip


def _cell(machine: str, slot: int, text: str, ids: list[str], conf: float | None) -> RegCell | None:
    t = _MARK_WORDS.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text.strip())
    if t.lower() in _EMPTY:
        return None
    pair = _PAIR.match(t)
    tokens = [pair.group(1), pair.group(2)] if pair else t.replace("(", " ").replace(")", " ").split()
    nums: list[Decimal] = []
    status: str | None = None
    unknown: list[str] = []
    for tok in tokens:
        v = _num(tok)
        if v is not None:
            nums.append(v)
            continue
        code = status_code(tok)
        if code:
            status = code
        elif tok.lower() not in _EMPTY:
            unknown.append(tok)
    uncertain, note = False, None
    if unknown and not nums and status is None:
        status, uncertain, note = " ".join(unknown)[:40].upper(), True, f'"{" ".join(unknown)}" is not a known mark.'
    elif unknown:
        uncertain, note = True, f'Also written: "{" ".join(unknown)[:80]}".'
    reading = nums[0] if nums else None
    picks = nums[1] if len(nums) > 1 and slot > 0 else None
    if len(nums) > (2 if slot else 1):
        uncertain, note = True, f'More numbers than expected in this cell: "{text.strip()[:80]}".'
    if reading is None and picks is None and status is None:
        return None
    low_conf = conf is not None and conf < 0.9
    note = note or ("Read with low confidence; check against the photo." if low_conf else None)
    return RegCell(machine, slot, reading, picks, status, text.strip()[:200], ids, uncertain or low_conf, note, conf)


def _row(machine: str, rest: str, ids: list[str], conf: float | None) -> list[RegCell]:
    if "|" in rest:
        rest = rest.strip()
        parts = rest[1:].split("|") if rest.startswith("|") else rest.split("|")
        cells = [c for c in (_cell(machine, slot, p, ids, conf) for slot, p in enumerate(parts[:5])) if c]
    else:
        cells = _row_tokens(machine, rest, ids, conf)
    for c in cells:  # "2386?": the person who typed / transcribed the page was not sure
        if "?" in c.raw and not c.uncertain:
            c.uncertain, c.note = True, "Marked unclear (?) when written down; check against the photo."
    return cells


def _row_tokens(machine: str, rest: str, ids: list[str], conf: float | None) -> list[RegCell]:
    """No cell separators: slot 0 is the first reading / mark, then reading + picks pairs."""
    text = _MARK_WORDS.sub(lambda m: f"{m.group(1)}.{m.group(2)}", rest)
    items: list[tuple[str, Any, str]] = []
    for tok in text.replace("(", " ").replace(")", " ").split():
        pair = _PAIR.match(tok)
        if pair:
            items.append(("pair", (Decimal(pair.group(1)), Decimal(pair.group(2))), tok))
        elif (v := _num(tok)) is not None:
            items.append(("num", v, tok))
        elif code := status_code(tok):
            items.append(("mark", code, tok))
        elif tok.lower() in _EMPTY:
            items.append(("empty", None, tok))
        else:
            items.append(("word", tok, tok))
    out: list[RegCell] = []
    i = 0
    reading = status = None  # slot 0: the start reading and / or a mark
    raw: list[str] = []
    if items and items[0][0] == "empty":
        i = 1
    else:
        while i < len(items) and len(raw) < 2:
            kind, v, tok = items[i]
            if kind == "num" and reading is None:
                reading = v
            elif kind == "mark" and status is None:
                status = v
            else:
                break
            raw.append(tok)
            i += 1
    if reading is not None or status is not None:
        out.append(RegCell(machine, 0, reading, None, status, " ".join(raw), ids, False, None, conf))
    prev = reading
    slot = 1
    plain = items[i:]
    even = all(k == "num" for k, _, _ in plain) and len(plain) % 2 == 0 and len(plain) // 2 <= 4
    while i < len(items) and slot <= 4:
        kind, v, tok = items[i]
        if kind == "empty":
            i, slot = i + 1, slot + 1
            continue
        if kind == "mark":
            out.append(RegCell(machine, slot, None, None, v, tok, ids, False, None, conf))
            i, slot = i + 1, slot + 1
            continue
        if kind == "pair":
            out.append(RegCell(machine, slot, v[0], v[1], None, tok, ids, False, None, conf))
            prev = v[0]
            i, slot = i + 1, slot + 1
            continue
        if kind == "word":
            out.append(RegCell(machine, slot, None, None, str(v)[:40].upper(), tok, ids, True,
                               f'"{tok}" is not a known mark.', conf))  # fmt: skip
            i, slot = i + 1, slot + 1
            continue
        nxt = items[i + 1] if i + 1 < len(items) else None
        picks = None
        sure = True
        if nxt and nxt[0] == "num":
            fits = prev is not None and rollover_diff(prev, v) == nxt[1]
            if even or fits:
                picks, i = nxt[1], i + 1
            elif nxt[1] < v and nxt[1] < 100:
                picks, i, sure = nxt[1], i + 1, False
        out.append(
            RegCell(machine, slot, v, picks, None, tok if picks is None else f"{tok} {picks}", ids, not sure,
                    None if sure else "Reading and picks not separated on the page; check the pairing.", conf)
        )  # fmt: skip
        prev = v
        i, slot = i + 1, slot + 1
    if i < len(items):
        extra = " ".join(t for _, _, t in items[i:])[:80]
        if out:
            out[-1].uncertain, out[-1].note = True, f'More written in this row than five times: "{extra}".'
    return out
