"""Calculated parts of a pick reading register and the arithmetic checks.

Calculated (never stored): machine total per shift (sum of its picks), column totals (sum of the picks of a time),
machines stopped per time, shift totals and the day total.

Checks, so a misread or miswritten number is shown to a person instead of being saved silently:
- picks = reading - previous reading (the latest earlier reading of that machine in the shift; a counter that went
  past 9999 / 999 starts again from 0). A mismatch points at the cell (reading, picks or the previous reading).
- the column total the worker wrote = the sum of the picks of that time. A mismatch points at the picks that no
  reading confirms; when every picks value is confirmed by its readings, the written total itself is wrong: shown
  as a note.
- the start reading of a shift = the last reading of the previous shift (I -> II -> III; shift I against the
  previous day's shift III).
- a machine whose readings rise by the same multiple of the written picks in every slot (a counter in other units)
  is reported once as a note, not as a mismatch in every cell.
Each blocking check has a signature (code and the numbers involved). A person who looked at it and pressed OK stores
the signature on the cell; the check stays accepted only while those numbers are unchanged.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.pick_registers.layout import PREVIOUS, SHIFTS, SLOTS, TIMES, machine_sort

RATIO_SPREAD = Decimal("1.25")
MAX_SUSPECTS = 3


@dataclass
class Issue:
    code: str
    text: str
    signature: str


@dataclass
class Result:
    machines: dict[str, list[str]] = field(default_factory=dict)  # shift -> machines in order
    machine_total: dict[tuple[str, str], Decimal | None] = field(default_factory=dict)
    column_total: dict[tuple[str, int], Decimal | None] = field(default_factory=dict)
    stopped: dict[tuple[str, int], int] = field(default_factory=dict)
    shift_total: dict[str, Decimal | None] = field(default_factory=dict)
    day_total: Decimal | None = None
    issues: dict[tuple[str, str, int], list[Issue]] = field(default_factory=dict)  # blocking, per cell
    vouched: set[tuple[str, str, int]] = field(default_factory=set)
    notes: list[dict[str, str]] = field(default_factory=list)  # non-blocking findings
    total_match: dict[tuple[str, int], bool | None] = field(default_factory=dict)
    total_issues: dict[tuple[str, int], Issue] = field(default_factory=dict)  # blocking, per written column total


def _n(v: Decimal) -> str:
    return format(v.normalize(), "f")


def rollover_diff(prev: Decimal, cur: Decimal) -> Decimal:
    diff = cur - prev
    if diff < 0 and prev >= 0:
        diff += Decimal(10) ** len(str(int(prev)))  # the counter went past its last digit and started again
    return diff


def _sum(values: list[Decimal | None]) -> Decimal | None:
    present = [v for v in values if v is not None]
    return sum(present, Decimal(0)) if present else None


def calculate(
    values: dict[tuple[str, str, int], Any],
    written: dict[tuple[str, int], Decimal | None],
    previous_end: dict[str, Decimal] | None = None,
) -> Result:
    """values: (shift, machine, slot) -> object with reading, picks, status. written: (shift, slot) -> total."""
    out = Result()
    get = values.get
    for sh in SHIFTS:
        machines = sorted({m for (s, m, _) in values if s == sh}, key=machine_sort)
        out.machines[sh] = machines
        for m in machines:
            out.machine_total[(sh, m)] = _sum([getattr(get((sh, m, k)), "picks", None) for k in SLOTS[1:]])
        for k in SLOTS:
            out.column_total[(sh, k)] = _sum([getattr(get((sh, m, k)), "picks", None) for m in machines]) if k else None
            out.stopped[(sh, k)] = sum(1 for m in machines if _stopped(values, sh, m, k))
        out.shift_total[sh] = _sum([out.column_total[(sh, k)] for k in SLOTS[1:]])
    out.day_total = _sum(list(out.shift_total.values()))

    ratio_machines = _differences(values, out)
    _continuity(values, out, previous_end or {})
    _columns(values, written, out)
    for (sh, m), ratio in sorted(ratio_machines.items(), key=lambda x: (SHIFTS.index(x[0][0]), machine_sort(x[0][1]))):
        out.notes.append(
            {
                "label": "Meter",
                "text": f"Machine {m}, shift {sh}: the meter readings rise about {ratio} times the written picks in "
                "every time slot (a counter in other units?). The picks are taken as written.",
            }
        )
    return out


def _stopped(values: dict, sh: str, m: str, k: int) -> bool:
    """No reading at this time, and the latest mark before (or at) it is a stop mark, not a reading."""
    v = values.get((sh, m, k))
    if v is not None and v.reading is not None:
        return False
    for j in range(k, -1, -1):
        w = values.get((sh, m, j))
        if w is None:
            continue
        if w.status:
            return True
        if w.reading is not None:
            return False
    return False


def _prev_reading(values: dict, sh: str, m: str, k: int) -> tuple[int, Decimal] | None:
    for j in range(k - 1, -1, -1):
        w = values.get((sh, m, j))
        if w is not None and w.reading is not None:
            return j, w.reading
        if w is not None and w.status and j > 0:
            return None  # the machine stopped in between: no difference to check
    return None


def _differences(values: dict, out: Result) -> dict[tuple[str, str], Decimal]:
    pending: dict[tuple[str, str], list[tuple[int, Decimal, Decimal, Decimal, int]]] = {}
    for (sh, m, k), v in values.items():
        if k == 0 or v.reading is None:
            continue
        prev = _prev_reading(values, sh, m, k)
        if prev is None:
            continue
        j, before = prev
        diff = rollover_diff(before, v.reading)
        if v.picks is None:
            if not v.status:  # a running machine: the picks belong under the reading
                out.issues.setdefault((sh, m, k), []).append(
                    Issue(
                        "MISSING_PICKS",
                        f"Reading {_n(v.reading)} has no picks under it ({_n(v.reading)} - {_n(before)} = {_n(diff)}). "
                        "The picks may have been read into another cell.",
                        f"nopicks:{_n(before)}:{_n(v.reading)}",
                    )
                )
            continue
        if diff == v.picks:
            out.vouched.update({(sh, m, k), (sh, m, j)})
        else:
            pending.setdefault((sh, m), []).append((k, before, diff, v.picks, j))
    ratios: dict[tuple[str, str], Decimal] = {}
    for (sh, m), bad in pending.items():
        checked = sum(1 for (s, mm, k), v in values.items() if s == sh and mm == m and k and v.picks is not None)
        rs = [d / p for (_, _, d, p, _) in bad if p > 0 and d > 0]
        if len(bad) >= 3 and len(rs) == len(bad) == checked and max(rs) <= min(rs) * RATIO_SPREAD:
            mean = sum(rs, Decimal(0)) / len(rs)
            if mean >= Decimal("1.5") or mean <= Decimal("0.67"):
                ratios[(sh, m)] = mean.quantize(Decimal("0.1"))
                continue
        for k, before, diff, picks, j in bad:
            reading = values[(sh, m, k)].reading
            out.issues.setdefault((sh, m, k), []).append(
                Issue(
                    "PICKS_DIFFERENCE",
                    f"{_n(reading)} - {_n(before)} ({TIMES[sh][j]}) = {_n(diff)}, but {_n(picks)} picks are written. "
                    "One of these numbers is misread or miswritten.",
                    f"diff:{_n(before)}:{_n(reading)}:{_n(picks)}",
                )
            )
    return ratios


def _last_reading(values: dict, sh: str, m: str) -> Decimal | None:
    for k in range(4, -1, -1):
        v = values.get((sh, m, k))
        if v is not None and v.reading is not None:
            return v.reading
    return None


def _continuity(values: dict, out: Result, previous_end: dict[str, Decimal]) -> None:
    for (sh, m, k), v in values.items():
        if k != 0 or v.reading is None:
            continue
        if sh in PREVIOUS:
            before, where = _last_reading(values, PREVIOUS[sh], m), f"shift {PREVIOUS[sh]}"
        else:
            before, where = previous_end.get(m), "the previous day's shift III"
        if before is None:
            continue
        if before == v.reading:
            out.vouched.add((sh, m, 0))
            continue
        out.issues.setdefault((sh, m, 0), []).append(
            Issue(
                "START_READING",
                f"{where[0].upper()}{where[1:]} ends at {_n(before)} for machine {m}; "
                f"this shift starts at {_n(v.reading)}. One of them is misread or miswritten.",
                f"start:{_n(before)}:{_n(v.reading)}",
            )
        )


def _columns(values: dict, written: dict[tuple[str, int], Decimal | None], out: Result) -> None:
    for sh in SHIFTS:
        for k in SLOTS:
            w = written.get((sh, k))
            if w is None:
                out.total_match[(sh, k)] = None
                continue
            if k == 0:  # figure under the start column: the shift / day total
                calc = out.shift_total[sh]
                out.total_match[(sh, 0)] = calc == w if calc is not None else None
                if calc is not None and calc != w:
                    day = out.day_total
                    out.notes.append(
                        {
                            "label": "Total",
                            "text": f"Shift {sh}: {_n(w)} is written under the first column (shift / day total). "
                            f"The picks of shift {sh} add up to {_n(calc)}"
                            + (f" and the day's to {_n(day)}." if day is not None and day != calc else ".")
                            + " Check what this figure is.",
                        }
                    )
                continue
            calc = out.column_total[(sh, k)]
            out.total_match[(sh, k)] = calc == w
            if calc == w:
                out.vouched.update({(sh, m, k) for m in out.machines[sh] if values.get((sh, m, k)) is not None})
                continue
            suspects = [
                m
                for m in out.machines[sh]
                if (v := values.get((sh, m, k))) is not None
                and v.picks is not None
                and (sh, m, k) not in out.vouched
                and not out.issues.get((sh, m, k))
            ]
            text = (
                f"The total written for {TIMES[sh][k]} (shift {sh}) is {_n(w)}; the picks of that column add up to "
                f"{_n(calc) if calc is not None else 'nothing'}."
            )
            out.total_issues[(sh, k)] = Issue(
                "COLUMN_TOTAL_MISMATCH",
                text
                + (
                    " Every picks value in it matches its meter readings: the written total may be wrong, or a"
                    " reading and its picks were both misread."
                    if not suspects
                    else ""
                ),
                f"tot:{_n(w)}:{calc}",
            )
            if 0 < len(suspects) <= MAX_SUSPECTS:
                for m in suspects:
                    picks = values[(sh, m, k)].picks
                    fit = picks + w - (calc or 0)
                    out.issues.setdefault((sh, m, k), []).append(
                        Issue(
                            "COLUMN_TOTAL",
                            text
                            + " No reading confirms this picks value"
                            + (f"; the column would match if it were {_n(fit)}." if fit >= 0 else "."),
                            f"col:{_n(w)}:{calc}:{_n(picks)}",
                        )
                    )


def open_issues(issues: list[Issue], accepted: str | None) -> list[Issue]:
    done = set((accepted or "").split("|"))
    return [i for i in issues if i.signature not in done]


def signatures(issues: list[Issue]) -> str:
    return "|".join(i.signature for i in issues)[:600]
