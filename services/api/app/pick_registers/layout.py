"""Layout of the hourly production reading register (WGS-02, pick reading; form F/WGS/201).

One page per shift. Columns: machine number, five times, Total. The first time is the start of the shift: its cell
is the meter reading carried over from the previous shift (slot 0). Each later time (slots 1-4) has the meter
reading and, written under it, the picks of those two hours. Shifts as the company works them: I 08-16, II 16-24,
III 00-08. The production day runs 08:00 to 08:00, so shift III (the night after shift II) belongs to the same
register date.
"""

import re

SHIFTS = ("I", "II", "III")
SLOTS = (0, 1, 2, 3, 4)
TIMES = {
    "I": ("08-00", "10-00", "12-00", "14-00", "16-00"),
    "II": ("16-00", "18-00", "20-00", "22-00", "24-00"),
    "III": ("24-00", "02-00", "04-00", "06-00", "08-00"),
}
SHIFT_HOURS = {"I": "08:00-16:00", "II": "16:00-24:00", "III": "00:00-08:00"}
PREVIOUS = {"II": "I", "III": "II"}  # the shift whose last reading starts this one (same register)
FORM = "WGS-02"

# What workers write instead of a reading when a machine is stopped. Kept as written; the label explains it.
STATUS_LABEL = {
    "B.FALL": "Beam fall",
    "S/C": "S/C",
    "STOP": "Stopped",
}
_STATUS = (
    (re.compile(r"^b\s*[./,]?\s*(?:f(?:all|al|a|m)?|fall)\.?$", re.IGNORECASE), "B.FALL"),
    (re.compile(r"^s\s*[/|.]\s*c$", re.IGNORECASE), "S/C"),
    (re.compile(r"^(?:stop|stopped|off|band)$", re.IGNORECASE), "STOP"),
)
_HOUR = re.compile(r"^(\d{1,2})\s*[-:.]\s*(\d{2})$")


def status_code(text: str) -> str | None:
    """'B.fall', 'B.F', 'Bfall', 'B/F', 'Bfm' -> 'B.FALL'; 'S/C', 'S/c' -> 'S/C'. None if it is not a known mark."""
    t = text.strip().strip("()")
    for pattern, code in _STATUS:
        if pattern.match(t):
            return code
    return None


def status_label(status: str | None) -> str:
    if not status:
        return ""
    return STATUS_LABEL.get(status, status)


def time_key(text: str) -> str | None:
    """'24-00', '24:00', '0-00', '2.00' -> '24-00', '02-00'. None if not a column time."""
    m = _HOUR.match(text.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute != 0 or hour > 24 or hour % 2:
        return None
    return f"{24 if hour == 0 else hour:02d}-00"


def shift_of_times(times: list[str]) -> str | None:
    """The shift whose columns these times are (the first time decides; the others must not contradict it)."""
    keys = [k for k in (time_key(x) for x in times) if k]
    if not keys:
        return None
    for sh, cols in TIMES.items():
        if keys[0] == cols[0] and all(k in cols for k in keys):
            return sh
    for sh, cols in TIMES.items():  # header cut off at the start: the remaining times still fit one shift
        if all(k in cols[1:] for k in keys) and len(keys) >= 2:
            return sh
    return None


def slot_of(shift: str, time: str) -> int | None:
    key = time_key(time)
    if key is None:
        return None
    cols = TIMES[shift]
    return cols.index(key) if key in cols else None


def machine_key(text: str) -> str | None:
    """'27', '027', 'M/C 27', 'm/c-27' -> '27'; letters kept for machines like '12A'."""
    t = re.sub(r"^(?:m\s*/?\s*c\.?\s*(?:no\.?)?\s*[-:]?\s*)", "", text.strip(), flags=re.IGNORECASE)
    m = re.fullmatch(r"0*(\d{1,4}[A-Za-z]?)", t)
    return m.group(1).upper() if m else None


def machine_sort(machine: str) -> tuple[int, str]:
    m = re.match(r"\d+", machine)
    return (int(m.group()) if m else 10**6, machine)
