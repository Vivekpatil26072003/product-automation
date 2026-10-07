"""Pick reading register (WGS-02): reading the page lines, the calculated totals and the arithmetic checks.

PAGE_SHIFT_III and PAGE_SHIFT_II are the two real handwritten pages of 2 Oct 2026 as transcribed from the photos
(F/WGS/201 Rev. No. 05): every reading, the picks written under it, the stop marks and the worker's column totals.
"?" marks the two numbers that are overwritten / unclear on the photo.
"""

from decimal import Decimal
from types import SimpleNamespace

from app.pick_registers import compute, layout
from app.pick_registers.reader import read_register

PAGE_SHIFT_III = [
    "F/WGS/201 Rev. No. 05",
    "HOURLY PRODUCTION READING REGISTER (WGS-02)",
    "DATE : 02/10/26   PICK - READING",
    "M/c No./Time | 24-00 | 02-00 | 04-00 | 06-00 | 08-00 | Total",
    "26 | B.fall | - | | | |",
    "27 | 2230 | 2282 24 | 2342 28 | 2386? 18 | 2444 27 |",
    "28 | 2259 | 2267 08 | 2287 20 | 2299 12 | Bfm |",
    "29 | 1306 | 1329 23 | 1356 27 | 1383 27 | 1410 27 |",
    "30 | 2462 | 2491 29 | 2516 25 | 2541 25 | 2569 28 |",
    "31 | B.fall | - | | 00 | 09 09 |",
    "32 | 1815 S/C | | 1823 08 | 1845 22 | 1869 24 |",
    "33 | 1167 | 1186 19 | 1212 26 | 1236 24 | 1259 23 |",
    "34 | 1218 | 1245 27 | 1268 23 | 1288 20 | 1316 28 |",
    "35 | 426 | 454 28 | 476 22 | 497 21 | 515 18 |",
    "36 | B.fall | - | | | |",
    "37 | B.fall | - | | | |",
    "38 | 1441 | 1467 26 | 1495 28 | 1518 23 | 1542 24 |",
    "39 | 1629 | 1648 19 | 1668 20 | 1689 21 | 1708 19 |",
    "40 | 3184 | 3209 25 | 3230 21 | 3241 11 | 3258 17 |",
    "41 | 3166 | 3190 24 | 3218 28 | 3245 27 | 3278 33 |",
    "42 | 2992 | 3017 25 | 3044 27 | 3068 24 | 3094 26 |",
    "43 | 3162 | 3186 24 | 3210 24 | 3229 19 | 3240 11 |",
    "44 | 1990 | 2016 26 | 2038 22 | 2063 25 | 2099 36 |",
    "45 | B.fall | - | | | |",
    "46 | 1358 | 1380 22 | 1402 22 | 1427 25 | 1455 28 |",
    "47 | 125 | 155 30 | 187 32 | 218 31 | 244 26 |",
    "48 | 976 | 1004 28 | 1024 20 | 1054 30 | 1087 33 |",
    "49 | 2067 | 2092 25 | 2120 28 | 2143 23 | 2169 26 |",
    "50 | 815 | 828 13 | 855 27 | 886 31 | 920 34 |",
    "51 | 2403 | 2433 30 | 2464 31 | 2490 26 | 2518 28 |",
    "52 | 379 | 408 29 | 430 22 | 460 30 | 496 36 |",
    "53 | 1730 | 1754 24 | 1780 26 | 1814 34 | 1846 32 |",
    "54 | 206 | 231 25 | 255 24 | 283 28 | 311 28 |",
    "55 | 315 | 345 30 | 374 29 | 400 26 | 425 25 |",
    "Total | 687 | 583 | 590 | 603 | 646 |",
    "02",
]

PAGE_SHIFT_II = [
    "F/WGS/201 Rev. No. 05",
    "HOURLY PRODUCTION READING REGISTER (WGS-02)",
    "DATE : 02/10/26   PICK - READING",
    "M/c No./Time | 16-00 | 18-00 | 20-00 | 22-00 | 24-00 | Total",
    "26 | B.F | | | | |",
    "27 | 2038 | 2090 24 | 2130 18 | 2180 24 | 2230 24 |",
    "28 | 2169 | 2191 22 | 2210 19 | 2234 24 | 2259 25 |",
    "29 | 1206 | 1230 24 | 1256 26 | 1281 25 | 1306 25 |",
    "30 | 2357 | 2384 27 | 2411 27 | 2437 26 | 2462 25 |",
    "31 | B.F | | | | |",
    "32 | 1769 | 1794 25 | 1815 21 | S/C | |",
    "33 | 1094 | 1110 16 | 1129 19 | 1145 16 | 1167 22 |",
    "34 | 1112 | 1136 24 | 1166 30 | 1192 26 | 1218 26 |",
    "35 | 327 | 353 26 | 378 25 | 400 22 | 426 26 |",
    "36 | B.F | | | | |",
    "37 | B.F | | | | |",
    "38 | 1341 | 1364 23 | 1394 30 | 1419 25 | 1441 22 |",
    "39 | 1357 | 1575 18 | 1594 19 | 1612 18 | 1629 17 |",
    "40 | 3086 | 3108 22 | 3138 30 | 3164 26 | 3184 20 |",
    "41 | 3080 | 3105 25 | 3130 25 | 3140? 10 | 3166 26 |",
    "42 | 2885 | 2910 25 | 2938 28 | 2964 26 | 2992 28 |",
    "43 | 3050 | 3078 28 | 3108 30 | 3135 27 | 3162 27 |",
    "44 | 1869 | 1893 24 | 1927 34 | 1957 30 | 1990 33 |",
    "45 | B.F | | | | |",
    "46 | 1234 | 1264 30 | 1298 34 | 1329 31 | 1358 29 |",
    "47 | 18 | 45 27 | 70 25 | 96 26 | 125 29 |",
    "48 | 853 | 881 28 | 916 25 | 946 30 | 976 30 |",
    "49 | 1953 | 1981 28 | 2015 34 | 2043 28 | 2067 24 |",
    "50 | 718 | 748 30 | 774 26 | 798 24 | 815 17 |",
    "51 | 2282 | 2310 28 | 2343 33 | 2373 30 | 2403 30 |",
    "52 | 282 | 304 22 | 330 26 | 357 27 | 379 22 |",
    "53 | 1598 | 1630 32 | 1665 35 | 1698 33 | 1730 32 |",
    "54 | 85 | 115 30 | 144 29 | 174 30 | 206 32 |",
    "55 | 183 | 216 33 | 250 34 | 281 31 | 315 34 |",
    "Total | | 641 | 682 | 615 | 625 |",
    "02",
]


def page(lines: list[str], confidence: float | None = None) -> dict:
    spans = [{"id": f"p1-s{i}", "text": t, "confidence": confidence} for i, t in enumerate(lines)]
    return {"page_no": 1, "parser": "text", "spans": spans}


def as_values(*readings) -> dict:
    out = {}
    for r in readings:
        for c in r.cells:
            out[(r.shift, c.machine, c.slot)] = SimpleNamespace(reading=c.reading, picks=c.picks, status=c.status)
    return out


def written(*readings) -> dict:
    return {(r.shift, slot): v for r in readings for slot, (v, _, _) in r.totals.items()}


def cell(r, machine, slot):
    return next(c for c in r.cells if c.machine == machine and c.slot == slot)


def test_layout_times_shifts_marks_and_machines():
    assert layout.shift_of_times(["24-00", "02-00", "04-00", "06-00", "08-00"]) == "III"
    assert layout.shift_of_times(["16:00", "18:00", "20.00", "22-00", "24-00"]) == "II"
    assert layout.shift_of_times(["08-00", "10-00", "12-00"]) == "I"
    assert layout.shift_of_times(["02-00", "04-00", "06-00"]) == "III"  # first column cut off
    assert layout.shift_of_times(["24-00", "10-00", "12-00"]) is None
    assert layout.time_key("0-00") == "24-00" and layout.time_key("3-00") is None
    for mark in ("B.fall", "B.F", "Bfall", "B/F", "Bfm", "b.f."):
        assert layout.status_code(mark) == "B.FALL", mark
    assert layout.status_code("S/C") == "S/C" and layout.status_code("2282") is None
    assert layout.machine_key("027") == "27" and layout.machine_key("M/C 12a") == "12A"
    assert layout.machine_key("Total") is None
    assert compute.rollover_diff(Decimal(9990), Decimal(15)) == 25  # the counter started again from 0


def test_real_page_shift_iii_is_read_cell_by_cell():
    r = read_register(page(PAGE_SHIFT_III))
    assert r.is_register and r.shift == "III" and r.register_date.isoformat() == "2026-10-02"
    assert r.rows == 30 and {c.machine for c in r.cells} == {str(m) for m in range(26, 56)}
    c = cell(r, "27", 1)
    assert (c.reading, c.picks, c.status) == (Decimal(2282), Decimal(24), None)
    assert cell(r, "27", 0).reading == 2230 and cell(r, "27", 0).picks is None
    assert cell(r, "26", 0).status == "B.FALL" and not [c for c in r.cells if c.machine == "26" and c.slot > 0]
    assert cell(r, "28", 4).status == "B.FALL" and cell(r, "28", 1).picks == 8
    assert (cell(r, "32", 0).reading, cell(r, "32", 0).status) == (Decimal(1815), "S/C")
    assert cell(r, "31", 3).reading == 0 and cell(r, "31", 4).picks == 9
    assert cell(r, "27", 3).uncertain and "unclear" in cell(r, "27", 3).note  # overwritten on the page
    assert sum(1 for c in r.cells if c.uncertain) == 1
    assert {k: v[0] for k, v in r.totals.items()} == {0: 687, 1: 583, 2: 590, 3: 603, 4: 646}


def test_checks_on_the_two_real_pages():
    iii, ii = read_register(page(PAGE_SHIFT_III)), read_register(page(PAGE_SHIFT_II))
    res = compute.calculate(as_values(iii, ii), written(iii, ii))
    # Column totals the worker wrote: 7 of 8 equal the sum of the picks.
    assert [res.column_total[("II", k)] for k in (1, 2, 3, 4)] == [641, 682, 615, 625]
    assert [res.column_total[("III", k)] for k in (1, 2, 3, 4)] == [583, 610, 603, 646]
    assert res.total_match[("III", 2)] is False and res.total_match[("II", 2)] is True
    assert res.shift_total == {"I": None, "II": 2563, "III": 2442} and res.day_total == 5005
    assert res.machine_total[("III", "52")] == 117 and res.stopped[("III", 4)] == 5  # 26 28 36 37 45
    assert res.stopped[("III", 3)] == 4  # 31 restarted at 06-00 (reading 00)
    flagged = {k: [i.code for i in v] for k, v in res.issues.items()}
    assert flagged == {
        ("II", "39", 1): ["PICKS_DIFFERENCE"],  # 1575 - 1357 = 218, 18 written: start probably 1557
        ("II", "48", 2): ["PICKS_DIFFERENCE"],  # 916 - 881 = 35, 25 written (the total uses 25)
        ("III", "27", 2): ["COLUMN_TOTAL"],  # 04-00 adds up to 610, 590 written; only m/c 27 is unconfirmed
    }
    assert "1575 - 1357 (16-00) = 218, but 18 picks" in res.issues[("II", "39", 1)][0].text
    assert "would match if it were 8" in res.issues[("III", "27", 2)][0].text
    # A written column total that does not match blocks approval on its own, until a person checks it.
    assert list(res.total_issues) == [("III", 2)] and "590" in res.total_issues[("III", 2)].text
    notes = " ".join(n["text"] for n in res.notes)
    assert "Machine 27, shift II" in notes and "Machine 27, shift III" in notes  # counter in other units
    assert "687 is written under the first column" in notes
    # Shift III starts where shift II ended for every running machine (no continuity mismatch).
    assert not [k for k, v in res.issues.items() if any(i.code == "START_READING" for i in v)]
    assert ("III", "41", 0) in res.vouched and ("II", "30", 4) in res.vouched


def test_start_reading_mismatch_and_accepting_a_check():
    iii, ii = read_register(page(PAGE_SHIFT_III)), read_register(page(PAGE_SHIFT_II))
    values = as_values(iii, ii)
    values[("III", "40", 0)].reading = Decimal(3148)  # misread 3184
    res = compute.calculate(values, written(iii, ii))
    [issue] = res.issues[("III", "40", 0)]
    assert issue.code == "START_READING" and "ends at 3184" in issue.text
    assert compute.open_issues([issue], compute.signatures([issue])) == []
    values[("III", "40", 0)].reading = Decimal(3149)  # changed again: the earlier OK no longer covers it
    [again] = compute.calculate(values, written(iii, ii)).issues[("III", "40", 0)]
    assert compute.open_issues([again], issue.signature) == [again]
    prev_day = compute.calculate(
        {("I", "27", 0): SimpleNamespace(reading=Decimal(10), picks=None, status=None)}, {}, {"27": Decimal(12)}
    )
    assert "previous day's shift III ends at 12" in prev_day.issues[("I", "27", 0)][0].text


def test_lines_without_separators_and_unknown_pages():
    r = read_register(
        page(
            [
                "PICK - READING",
                "Date: 2/10/2026",
                "M/c 24-00 02-00 04-00 06-00 08-00",
                "27 2230 2282 24 2342 28",
                "47 125 155/30 187/32 B.fall",
                "31 B fall - - 00",
            ]
        )  # fmt: skip
    )
    assert r.shift == "III" and cell(r, "27", 2).picks == 28 and not cell(r, "27", 2).uncertain
    assert cell(r, "47", 2).reading == 187 and cell(r, "47", 3).status == "B.FALL"
    assert cell(r, "31", 0).status == "B.FALL" and cell(r, "31", 3).reading == 0
    loose = read_register(page(["Shift : 2", "16-00 18-00 20-00 22-00 24-00", "27 2038 2090 52 2130"]))
    assert loose.shift == "II" and not loose.is_register  # one row only, no printed heading
    assert not read_register(page(["Production 52104 52949", "Picks 7026 7087 7071"])).is_register
    low = read_register(page(PAGE_SHIFT_III, confidence=0.6))
    assert all(c.uncertain for c in low.cells)  # OCR not sure: every value is checked by a person


def test_reading_without_picks_on_a_running_machine_is_flagged():
    """Seen in the real Gemini test: the "9" picks of m/c 31 were put one column early (under 06-00)."""
    iii = read_register(page(PAGE_SHIFT_III))
    values = as_values(iii)
    values[("III", "31", 3)].picks, values[("III", "31", 4)].picks = Decimal(9), None
    res = compute.calculate(values, written(iii))
    assert [i.code for i in res.issues[("III", "31", 4)]] == ["MISSING_PICKS"]
    assert {k for k in res.total_issues} == {("III", 2), ("III", 3), ("III", 4)}  # 06-00 and 08-00 no longer add up


def test_four_totals_without_the_empty_first_cell_belong_to_the_picks_columns():
    """Seen in a real Gemini transcription: "Total | 641 | 682 | 615 | 625" without the empty start column."""
    lines = [x if not x.startswith("Total") else "Total | 641 | 682 | 615 | 625" for x in PAGE_SHIFT_II]
    assert {k: v[0] for k, v in read_register(page(lines)).totals.items()} == {1: 641, 2: 682, 3: 615, 4: 625}
    assert set(read_register(page(PAGE_SHIFT_III)).totals) == {0, 1, 2, 3, 4}  # five written: unchanged
