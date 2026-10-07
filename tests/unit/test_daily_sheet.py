"""Daily production sheet: reading rows from page text, and the calculations, checked against the company's sheet
of 2 Oct 2026 (SULZER PROD 02.10.2026.xls)."""

from decimal import Decimal as D

import pytest

from app.shift_reports.catalog import SECTIONS, default_target, input_cells, section
from app.shift_reports.compute import section_grid, to_date
from app.shift_reports.reader import read_sheet

TARGETS = {(s.key, m.key): default_target(s.key, m.key) for s in SECTIONS for m in s.metrics}

# A notebook page as a worker writes it: shift values only.
NOTEBOOK = [
    "Date : 2-Oct-26",
    "Shift I supervisor : Ramesh Patel",
    "Supervisor II - Suresh",
    "SULZER PROD",
    "No. of running looms 67.91 68.46 68.44",
    "Picks 7026 7087 7071",
    "Production in Meters 52104 52949 52919",
    "Production in Kg 8919 8981 8955",
    "Avg width 3.33 3.33 3.33",
    "SULZER FABRIC Wastage",
    "(a) Bobbin Wastage 2 1 1",
    "(b) Start up Wastage 3.5 3.5 4",
    "Downtime",
    "a) Mechanical 169.54 123.30 110.97",
    "c) Weft cut 36.99 33.91",
    "WARPING",
    "PRASHANT",
    "Meters 9700 13300 10800",
    "Working Hours 4 5.25 4.75",
    "B/F= 75,37,59,73,28,86",
]

# Lines as the printed sheet gives them: target, I, II, III, total, to date, then the ground-cover columns.
PRINTED = [
    "2-Oct-26 1 2 1215 137",
    "No. of running looms/days 80.04 67.91 68.46 68.44 68.27 69.26 2.61 0.13 0.66 1.25 0.68 0.34 82.65",
    "Production in Meters 174940 52104 52949 52919 157972.00 159449.50 6459.26 49.00 367.00 799.00 1215.00 607.50",
    "Picks 24264 7026 7087 7071 21184.00 21514.00 772.04 7.00 53.00 104.00 164.00 82.00 21348.00",
    "Meters 54800 9700 13300 10800 33800 35550.00 10500 11000 7400 28900 22200.00 62700",
    "SMM 0.80 1.00 0.50 MAINT 1.36 1.36 1.34",
    "NO BEAM 0 12.38 14.92 ROLL CUT 0.53 0.52 0.51",
]


def page(lines):
    return {"page_no": 1, "spans": [{"id": f"p1-s{i}", "text": t} for i, t in enumerate(lines, start=1)]}


def cells(reading):
    return {(c.section, c.metric, c.shift): c for c in reading.cells}


def test_notebook_page_fills_the_right_cells():
    r = read_sheet(page(NOTEBOOK), TARGETS)
    c = cells(r)
    assert r.is_sheet and str(r.report_date) == "2026-10-02"
    assert r.supervisors == {"I": "Ramesh Patel", "II": "Suresh"}
    assert [c[("sulzer", "picks", sh)].value for sh in ("I", "II", "III")] == [D(7026), D(7087), D(7071)]
    assert c[("sulzer", "production_m", "II")].value == D(52949) and not c[("sulzer", "production_m", "II")].uncertain
    assert c[("fabric_wastage", "startup", "III")].value == D(4)
    assert c[("downtime", "mechanical", "I")].value == D("169.54")
    assert c[("warping_prashant", "working_hours", "II")].value == D("5.25")
    assert c[("sulzer", "picks", "I")].span_ids == ["p1-s6"]  # every value keeps its source line
    weft = c[("downtime", "weft_cut", "I")]  # only two values written: never guessed which shifts
    assert weft.uncertain and "Only 2" in weft.note and ("downtime", "weft_cut", "III") not in c
    assert r.notes[0]["label"] == "Beam fall m/c no." and "75,37" in r.notes[0]["text"]


def test_printed_sheet_lines_skip_targets_and_check_totals():
    r = read_sheet(page(PRINTED), TARGETS)
    c = cells(r)
    assert str(r.report_date) == "2026-10-02"
    assert [c[("sulzer", "running_looms", sh)].value for sh in ("I", "II", "III")] == [
        D("67.91"),
        D("68.46"),
        D("68.44"),
    ]
    assert [c[("ground_cover", "production_m", sh)].value for sh in ("I", "II", "III")] == [D(49), D(367), D(799)]
    assert [c[("warping_hacoba", "meters", sh)].value for sh in ("I", "II", "III")] == [D(10500), D(11000), D(7400)]
    assert c[("low_running", "smm", "D")].value == D("1.00") and c[("mech_detail", "maint", "D")].value == D("1.36")
    assert c[("low_running", "no_beam", "D")].value == D("12.38") and c[("mech_detail", "roll_cut", "D")].value == D(
        "0.52"
    )
    assert not any(x.uncertain for x in r.cells)


def test_a_total_that_does_not_add_up_is_flagged():
    r = read_sheet(
        page(
            [
                "SULZER PROD",
                "Picks 7026 7087 7071 21000",
                "Production in Meters 1 2 3",
                "Avg width 3 3 3",
                "Production in Kg 1 1 1",
                "Meters/loom/day 1 1 1",
            ]
        ),
        TARGETS,
    )
    picks = cells(r)[("sulzer", "picks", "I")]
    assert picks.uncertain and "21000" in picks.note


def test_unclosed_label_bracket_is_not_a_value():
    r = read_sheet(
        page(["REASON FOR LOW RUNNING M/CS", "CONDCUCTIVITY CHECK(PER SHIFT 2 TIME", "CHECK) 0.00 0.32 0.32"]), TARGETS
    )
    assert ("low_running", "conductivity_check", "D") not in cells(r)


@pytest.mark.parametrize(
    ("key", "running", "picks", "expected"),
    [  # the company's sheet, 2 Oct 2026: (theoretical picks, loss, utilisation %, working %, total %, picks/hour)
        (
            "sulzer",
            ("67.908", "68.46", "68.442"),
            (7026, 7087, 7071),
            {
                "theoretical_picks": "24347.8",
                "utilization_pct": "74.207",
                "working_pct": "87.005",
                "total_eff_pct": "64.564",
                "picks_per_hour": "12.929",
            },
        ),
        (
            "normal_fibc",
            ("53.408", "54.335", "55.568"),
            (5620, 5727, 5834),
            {
                "theoretical_picks": "18290.8",
                "loss_of_pick": "1109.8",
                "utilization_pct": "74.571",
                "working_pct": "88.498",
                "total_eff_pct": "65.993",
            },
        ),
        (
            "normal_condv",
            ("14.5", "14.125", "12.875"),
            (1406, 1360, 1237),
            {
                "theoretical_picks": "4648",
                "loss_of_pick": "645",
                "utilization_pct": "72.807",
                "working_pct": "82.009",
                "total_eff_pct": "59.718",
                "picks_per_hour": "12.055",
            },
        ),
    ],
)
def test_calculations_match_the_company_sheet(key, running, picks, expected):
    s = section(key)
    values = {}
    for sh, rl, pk in zip(("I", "II", "III"), running, picks, strict=True):
        values[(key, "running_looms", sh)] = D(rl)
        values[(key, "picks", sh)] = D(pk)
    g = section_grid(s, values, {k: D(v) for k, v in s.params.items()})
    for metric, want in expected.items():
        assert abs(g[metric]["total"] - D(want)) < D("0.15"), (metric, g[metric]["total"])


def test_totals_averages_and_to_date():
    s = section("warping_prashant")
    v = {
        ("warping_prashant", "meters", sh): D(x) for sh, x in zip(("I", "II", "III"), (9700, 13300, 10800), strict=True)
    }
    v |= {
        ("warping_prashant", "working_hours", sh): D(x)
        for sh, x in zip(("I", "II", "III"), ("4", "5.25", "4.75"), strict=True)
    }
    g = section_grid(s, v, {})
    assert g["meters"]["total"] == D(33800)
    assert round(g["meters_per_min"]["I"], 3) == D("40.417")  # 9700 / (4 h x 60)
    assert round(g["meters_per_min"]["total"], 3) == D("40.178")  # average of the shifts, as on the sheet
    assert to_date(D(157972), [D(160927)]) == D("159449.5")  # the sheet's To date for 2 Oct
    missing = section_grid(
        section("sulzer"),
        {("sulzer", "running_looms", "I"): D(68)},
        {"installed": D(92), "rate": D("14.86"), "theo_rate": D("14.86")},
    )
    assert missing["working_pct"]["I"] is None and missing["running_looms"]["total"] is None  # never a guess


def test_every_input_cell_is_addressable():
    keys = input_cells()
    assert ("sulzer", "picks", "II") in keys and ("low_running", "smm", "D") in keys
    assert ("sulzer", "working_pct", "I") not in keys  # calculated cells are never entered


# The company's real handwritten diary page (Sulzer, 2 Oct 2026, shift 1), as transcribed from the photo: one shift
# per page, two "label : value" columns, numbered headings, calculated values written too.
REAL_DIARY = [
    "SULZER - Production Report",
    "Date : 02/10/2026",
    "Shift : 1",
    "1. Sulzer",
    "Running loom/day : 80.04",
    "Production (mtr) : 52,104",
    "Production (kg) : 8,919",
    "Picks : 7,026",
    "Loss of pick : 1,047",
    "Loom utilization : 73.81%",
    "Working efficiency : 87.03%",
    "Total efficiency : 64.24%",
    "Avg width : 3.33",
    "2. Normal FIBC",
    "Running loom/day : 53.41",
    "Production (mtr) : 42,193",
    "Production (kg) : 7,064",
    "Picks : 5,620",
    "Loss of pick : 362",
    "Utilization : 73.16%",
    "Working efficiency : 88.52%",
    "Total efficiency : 64.76%",
    "3. Normal CONDV",
    "Production (mtr) : 9,911",
    "Total weight : 1,855 kg",
    "Picks : 1,406",
    "Loss of pick : 218",
    "Loom utilization : 76.32%",
    "Working efficiency : 82.45%",
    "4. Warping production",
    "Production (mtr) : 9,700",
    "Weight : 2,006 kg",
    "Working hours : 4",
    "Speed : 40.42 m/min",
    "No. of beams : 2",
    "5. Manpower",
    "Sulzer mandays : 42",
    "Training : 3",
    "Warping mandays : 20",
    "6. Downtime / Wastage (Shift 1)",
    "Mechanical : 169.54 hrs",
    "Weft cut : 36.99 hrs",
    "Selvedge : 36.50 hrs",
    "Warp cut : 24.66 hrs",
    "Electrical : 25.65 hrs",
    "Power failure : 0.00 hrs",
    "No operator : 0.00 hrs",
    "Total downtime : 403.81 hrs (approx)",
]


def test_real_diary_page_one_shift_per_page():
    r = read_sheet(page(REAL_DIARY), TARGETS)
    c = cells(r)
    assert r.page_shift == "I" and str(r.report_date) == "2026-10-02"
    assert all(k[2] == "I" for k in c)  # every value goes to the page's shift, none to II / III
    assert len(c) == 26
    assert c[("sulzer", "production_kg", "I")].value == D(8919)  # second column of a line
    assert c[("normal_condv", "production_kg", "I")].value == D(1855)  # "Total weight : 1,855 kg"
    assert c[("normal_condv", "picks", "I")].value == D(1406)  # the unit "kg" did not move the table
    assert c[("warping_manpower", "mandays", "I")].value == D(20) and c[("manpower", "mandays", "I")].value == D(42)
    assert not c[("sulzer", "picks", "I")].uncertain
    machine = c[("warping_prashant", "meters", "I")]
    assert machine.uncertain and "Prashant or Hacoba" in machine.note  # the page does not name the machine
    written = {(w["section"], w["metric"]): w["value"] for w in r.written_calc}
    assert written[("sulzer", "working_pct")] == D("87.03") and written[("downtime", "total")] == D("403.81")


def test_misread_dates_and_split_numbers_are_highlighted_not_guessed():
    r = read_sheet(
        page(
            [
                "Date : 02/04/2066",
                "Shift : 1",
                "1. Sulzer",
                "Production (mtr) : 52, 104",
                "Picks : 7026",
                "Production (kg) : 8919",
                "Avg width : 3.33",
                "Running loom/day : 67.9",
            ]
        ),
        TARGETS,
    )
    assert r.report_date is None and r.notes[0]["label"] == "Date not used"
    assert cells(r)[("sulzer", "production_m", "I")].value == D(52104)  # "52, 104" is one number on a 1-shift page
    r = read_sheet(
        page(
            [
                "SULZER PROD",
                "Production in Meters 52 104 3",
                "Picks 7026 7087 7071",
                "Avg width 3 3 3",
                "Production in Kg 1 1 1",
                "Meters/loom/day 1 1 1",
            ]
        ),
        TARGETS,
    )
    prod = cells(r)[("sulzer", "production_m", "I")]
    assert prod.uncertain and "very different" in prod.note
