"""The daily production sheet ("SULZER PROD. REPORT"): every section and row, which values are written by hand and
which are calculated, and the calculation rules.

Taken from the company's sheet (SULZER PROD 02.10.2026.xls) and checked against its values:
- Total column: the sum of the three shifts, or their average (agg="avg"), as in the sheet.
- To date: the average of the daily totals from the 1st of the month up to and including this day.
- Loom tables: theoretical picks, loss of pick, the three efficiency % and picks/hour are calculated from running
  looms and picks with each table's parameters (installed looms, picks/hour rate, and the rate the sheet uses for
  the theoretical-picks row). Parameters are editable by administrators (targets page), defaults from the sheet.
- Warping metres/min = metres / (working hours x 60).
- "total" rows are the sum of the rows above them.
Workers write only the shift values of input rows (decision: "shift values only").
"""

from dataclasses import dataclass, field
from decimal import Decimal

SHIFTS = ("I", "II", "III")
DAY = ("D",)


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    agg: str = "sum"  # sum | avg (Total column)
    kind: str = "input"  # input | derived
    target: str | None = None  # default target from the company's sheet
    unit: str = ""
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    metrics: tuple[Metric, ...]
    shifts: tuple[str, ...] = SHIFTS
    group: str = "Weaving"
    params: dict[str, str] = field(default_factory=dict)  # loom tables: installed, rate, theo_rate
    headings: tuple[str, ...] = ()  # how the section is titled on a page

    def metric(self, key: str) -> Metric | None:
        return next((m for m in self.metrics if m.key == key), None)


def _loom(targets: dict[str, str]) -> tuple[Metric, ...]:
    t = targets.get
    return (
        Metric(
            "running_looms",
            "No. of running looms/days",
            "avg",
            target=t("running_looms"),
            synonyms=(
                "no. of running looms",
                "no of running looms",
                "running loom/day",
                "running loom",
                "running looms",
                "no. of looms/days",
                "no of looms/days",
                "no. of looms",
                "no of looms",
                "looms/days",
            ),
        ),
        Metric(
            "theoretical_picks",
            "Picks (theoretical, running m/c)",
            "sum",
            "derived",
            t("theoretical_picks"),
            synonyms=("picks(", "picks (", "theoretical picks"),
        ),
        Metric("picks", "Picks", "sum", target=t("picks"), synonyms=("picks",)),
        Metric("loss_of_pick", "Loss of Pick", "sum", "derived", synonyms=("loss of pick", "loss of picks")),
        Metric(
            "production_m",
            "Production in Meters",
            "sum",
            target=t("production_m"),
            unit="m",
            synonyms=(
                "production in meters",
                "production in metres",
                "production meters",
                "prod meters",
                "production in mtr",
                "production (mtr)",
                "production (mtrs)",
                "production (meters)",
                "production (m)",
            ),
        ),
        Metric(
            "production_kg",
            "Production in Kg.",
            "sum",
            target=t("production_kg"),
            unit="kg",
            synonyms=(
                "production in kg",
                "production kg",
                "prod kg",
                "production (kg)",
                "production (kgs)",
                "total weight",
                "weight",
            ),
        ),
        Metric(
            "meters_per_loom",
            "Meters/loom/day",
            "sum",
            synonyms=("meters/loom/day", "meter/loom/day", "mtrs/loom/day", "meters per loom"),
        ),
        Metric(
            "utilization_pct",
            "Loom Utilization efficiency (%)",
            "avg",
            "derived",
            t("utilization_pct"),
            "%",
            ("loom utilization", "utilization efficiency", "utilization", "utilisation"),
        ),
        Metric(
            "working_pct",
            "Loom Working efficiency (%)",
            "avg",
            "derived",
            t("working_pct"),
            "%",
            ("loom working", "working efficiency"),
        ),
        Metric(
            "total_eff_pct", "Total efficiency (%)", "avg", "derived", t("total_eff_pct"), "%", ("total efficiency",)
        ),
        Metric(
            "picks_per_hour",
            "Cumulative picks/hour",
            "avg",
            "derived",
            t("picks_per_hour"),
            synonyms=("cumulative picks/hour", "picks/hour", "picks per hour"),
        ),
        Metric("avg_width", "Avg width", "avg", target=t("avg_width"), synonyms=("avg width", "average width")),
    )


def _rows(prefix_total: str, rows: list[tuple[str, str, str | None, tuple[str, ...]]], unit: str = "") -> tuple:
    out = tuple(Metric(k, label, target=target, unit=unit, synonyms=syn) for k, label, target, syn in rows)
    total_syn = (prefix_total.lower(), "total downtime", "total")
    return out + (Metric("total", prefix_total, kind="derived", unit=unit, synonyms=total_syn),)


SECTIONS: tuple[Section, ...] = (
    Section(
        "sulzer",
        "Sulzer production",
        _loom(
            {
                "running_looms": "80.04",
                "theoretical_picks": "28545",
                "picks": "24264",
                "production_m": "174940",
                "production_kg": "31139",
                "utilization_pct": "87",
                "working_pct": "85",
                "total_eff_pct": "73.95",
                "picks_per_hour": "12.90",
                "avg_width": "3.45",
            }
        ),
        params={"installed": "92", "rate": "14.86", "theo_rate": "14.86"},
        headings=("sulzer prod", "sulzer production", "sulzer fibc report", "sulzer prod. report", "sulzer"),
    ),
    Section(
        "ground_cover",
        "Ground cover",
        _loom(
            {
                "running_looms": "2.61",
                "theoretical_picks": "908.28",
                "picks": "772.04",
                "production_m": "6459.26",
                "production_kg": "645.93",
                "utilization_pct": "87",
                "working_pct": "85",
                "total_eff_pct": "73.95",
                "picks_per_hour": "12.63",
                "avg_width": "3.53",
            }
        ),
        params={"installed": "3", "rate": "14.86", "theo_rate": "9.6"},
        headings=("ground cover", "g.cover", "g cover"),
    ),
    Section(
        "normal_condv",
        "Normal CONDV",
        _loom(
            {
                "running_looms": "16.53",
                "theoretical_picks": "5574",
                "picks": "3902",
                "production_m": "31867",
                "production_kg": "5959",
                "utilization_pct": "87",
                "working_pct": "85",
                "total_eff_pct": "73.95",
                "picks_per_hour": "12.63",
                "avg_width": "3.00",
            }
        ),
        params={"installed": "19", "rate": "14.70", "theo_rate": "14"},
        headings=("normal condv", "condv"),
    ),
    Section(
        "normal_fibc",
        "Normal FIBC",
        _loom(
            {
                "running_looms": "63.51",
                "theoretical_picks": "21416",
                "picks": "18203",
                "production_m": "144247",
                "production_kg": "24955",
                "utilization_pct": "87",
                "working_pct": "85",
                "total_eff_pct": "73.95",
                "picks_per_hour": "12.63",
                "avg_width": "3.53",
            }
        ),
        params={"installed": "73", "rate": "14.86", "theo_rate": "14"},
        headings=("normal fibc",),
    ),
    Section(
        "machine_status",
        "Machine status (day)",
        (
            Metric("manpower_per_ton", "Manpower / ton", "avg", synonyms=("manpower / ton", "manpower/ton")),
            Metric(
                "turnaround_hrs",
                "Turn around time (hrs)",
                "avg",
                target="191.04",
                synonyms=("turn around time", "turnaround time"),
            ),
            Metric(
                "hrs_per_machine_start",
                "Hrs per machine to start",
                "avg",
                target="12",
                synonyms=("hrs per machine to start",),
            ),
            Metric("start_mc", "No. of start m/c", synonyms=("no. of start m/c", "no of start m/c", "start m/c")),
            Metric("beam_fall_mc", "No. of beam fall m/c", synonyms=("no of beam fall m/c", "beam fall m/c")),
        ),
        shifts=DAY,
        headings=("machine status",),
    ),
    Section(
        "fabric_wastage",
        "Sulzer fabric wastage (kg)",
        _rows(
            "Total wastage",
            [
                ("bobbin", "(a) Bobbin wastage", "4.29", ("bobbin wastage",)),
                ("startup", "(b) Start up wastage (fabric)", "16.76", ("start up wast", "start up wastage", "startup")),
                ("new_beam", "(c) New beam (drawing)", "5.11", ("new beam",)),
                ("beam", "(d) Beam wastage", "13.00", ("beam wastage",)),
            ],
            "kg",
        ),
        group="Wastage & manpower",
        headings=("sulzer fabric wastage", "fabric wastage"),
    ),
    Section(
        "sulzer_wastage",
        "Sulzer wastage (kg)",
        _rows(
            "Total wastage",
            [
                ("bobbin", "(a) Bobbin wastage", "0.30", ("bobbin wastage",)),
                ("startup", "(b) Start up wastage (fabric)", "0.63", ("start up wast", "start up wastage", "startup")),
                ("new_beam", "(c) New beam (drawing)", "0.30", ("new beam",)),
                ("beam", "(d) Beam wastage", "1.00", ("beam wastage",)),
                ("black_fabric", "Black fabric / rewinding wastage", "2.79", ("black fabric", "rewinding wastage")),
            ],
            "kg",
        ),
        group="Wastage & manpower",
        headings=("sulzer wastage",),
    ),
    Section(
        "manpower",
        "Sulzer manpower",
        _rows(
            "Total",
            [
                ("mandays", "(a) Mandays", None, ("sulzer mandays", "sulzer man days", "mandays", "man days")),
                ("training", "(b) Training", None, ("training",)),
            ],
        ),
        group="Wastage & manpower",
        headings=("sulzer- manpower", "sulzer manpower", "sulzer - manpower"),
    ),
    Section(
        "downtime",
        "Downtime (hours)",
        _rows(
            "Total",
            [
                ("mechanical", "a) Mechanical", None, ("mechinical", "mechanical")),
                ("no_operator", "b) No operator", None, ("no operator",)),
                ("weft_cut", "c) Weft cut", None, ("weft cut",)),
                ("selvedge", "d) Selvedge", None, ("selvedge",)),
                ("no_weft", "e) No weft / leno yarn", None, ("no weft",)),
                ("warp_cut", "f) Warp cut", None, ("warp cut",)),
                ("roll_cut", "g) Roll cut", None, ("roll cut",)),
                ("electrical", "h) Electrical", None, ("electrical",)),
                ("power_failure", "i) Power failure", None, ("power failure",)),
                ("bad_beam", "j) Bad beam / missing end", None, ("bad beam", "missing end")),
                ("vacuum", "k) Vacuum cleaning", None, ("vacuum cleaning", "vacuum")),
                ("bad_bobbin", "l) Bad bobbin", None, ("bad bobbin",)),
            ],
            "h",
        ),
        group="Downtime",
        headings=("downtime", "down time", "downtime / wastage", "downtime/wastage"),
    ),
    Section(
        "low_running",
        "Reason for low running m/cs (day)",
        (
            Metric("running_mcs", "Running m/cs", "avg", target="82.65", synonyms=("running m/cs", "running mcs")),
            Metric("smm", "SMM", target="0.80", synonyms=("smm",)),
            Metric("revision", "Revision", target="0.11", synonyms=("revision",)),
            Metric("overhauling", "Overhauling", target="0.06", synonyms=("overhauling",)),
            Metric("major_breakdown", "Major breakdown", target="0.35", synonyms=("major breakdown",)),
            Metric("no_beam", "No beam", target="0", synonyms=("no beam",)),
            Metric("bfall_start", "B.fall & start", target="6.83", synonyms=("b.fall & start", "b fall & start")),
            Metric("mech_others", "Mech. + others", target="4.20", synonyms=("mech. + others", "mech + others")),
            Metric("conductivity_check", "Conductivity check", target="0", synonyms=("condcuctivity", "conductivity")),
            Metric("no_yarn_chromic", "No yarn chromic", target="0", synonyms=("no yarn chromic",)),
            Metric("develop", "Develop", target="0", synonyms=("devlop", "develop")),
            Metric("rain_stop", "Due to rain m/c stop", target="0", synonyms=("due to rain",)),
            Metric("no_plan", "No plan", target="0", synonyms=("no plan",)),
            Metric("no_spare", "No spare", target="0", synonyms=("no spare",)),
            Metric("warping_prob", "Warping prob", target="0", synonyms=("warping prob",)),
            Metric("total_mcs", "Total m/cs", "avg", target="95", synonyms=("total m/cs", "total mcs")),
        ),
        shifts=DAY,
        group="Downtime",
        headings=("reason for low running",),
    ),
    Section(
        "mech_detail",
        "Mech. & others detail (day)",
        _rows(
            "Total",
            [
                ("maint", "Maint", "1.36", ("maint",)),
                ("weft_cut", "Weft cut", "0.41", ("weft cut",)),
                ("selvedge", "Selvedge", "0.35", ("selvedge",)),
                ("warp_cut", "Warp cut", "0.47", ("warp cut",)),
                ("roll_cut", "Roll cut", "0.53", ("roll cut",)),
                ("no_weft", "No weft", "0", ("no weft",)),
                ("power_cut", "Power cut", "0", ("power cut",)),
                ("no_operator", "No operator", "0", ("no operator",)),
                ("electrical", "Electrical", "0.35", ("electrical",)),
                ("bad_beam", "Bad beam", "0.38", ("bad beam",)),
                ("vacuum", "Vacuum", "0.36", ("vaccum", "vacuum")),
            ],
        ),
        shifts=DAY,
        group="Downtime",
        headings=("mech. & others detail", "mech & others detail"),
    ),
)

_WARP_PROD = (
    Metric(
        "meters",
        "Meters",
        target=None,
        unit="m",
        synonyms=("production (mtr)", "production (mtrs)", "production (meters)", "meters", "metres", "mtrs"),
    ),
    Metric(
        "production_kg",
        "Production (kg)",
        unit="kg",
        synonyms=("production (kgs.)", "production (kg", "weight", "kgs", "kg"),
    ),
    Metric("working_hours", "Working hours", unit="h", synonyms=("working hours",)),
    Metric(
        "meters_per_min", "Meters / min", "avg", "derived", synonyms=("meters /min", "meters/min", "mtrs/min", "speed")
    ),
    Metric("beams", "No. of beams", synonyms=("no of beams", "beams")),
    Metric("breakages", "No. of breakage", synonyms=("no of brakage", "no of breakage", "breakage")),
    Metric("bobbin_change", "No. of bobbin change", synonyms=("no. of bobbin change", "no of bobbin change")),
    Metric("warp_leasing", "No. of warp leasing", synonyms=("no. of warp leasing", "no of warp leasing")),
)
_WARP_DOWN = [
    ("leasing_doffing", "Leasing & doffing", ("leasing & doffing",)),
    ("breakage_bobbin", "Breakages & bobbin change", ("breakages & bobbin change",)),
    ("size_change", "Size change", ("size change",)),
    ("bobbin_cleaning", "Bobbin cleaning", ("bobbin cleaning",)),
    ("conversion", "Conversion", ("conversion",)),
    ("threading", "Threading", ("threading",)),
    ("mech_maint", "Mech. maint", ("mech.maint", "mech maint")),
    ("electrical", "Electrical", ("electrical",)),
    ("no_bobbin", "No bobbin", ("no bobbin",)),
    ("no_manpower", "No man power", ("no man power", "no manpower")),
    ("no_beam_pipe", "No beam pipe", ("no beam pipe",)),
    ("mc_shifting", "M/c shifting", ("m/c shitting", "m/c shifting")),
    ("conductive_setting", "Conductive setting", ("conductive setting",)),
    ("audit_housekeeping", "Audit work + house keeping", ("audit work",)),
    ("power_cut", "Power cut", ("power cut",)),
    ("no_planning", "No planning", ("no planing", "no planning")),
    ("recess", "Recess time", ("recces time", "recess time")),
]

SECTIONS = (
    SECTIONS
    + tuple(
        Section(
            f"warping_{m}",
            f"Warping {name}",
            _WARP_PROD
            + tuple(Metric(f"down_{k}", f"Down time: {label}", unit="h", synonyms=syn) for k, label, syn in _WARP_DOWN),
            group="Warping",
            headings=(m, f"{m} (24 hrs)"),
        )
        for m, name in (("prashant", "Prashant"), ("hacoba", "Hacoba"))
    )
    + (
        Section(
            "warping_wastage",
            "Warping wastage (kg)",
            _rows(
                "Total wastage",
                [
                    ("bobbin_cutting", "Bobbin cutting wastage", None, ("bobbin cutting wastage",)),
                    ("bobbin_cleaning", "Bobbin cleaning wastage", None, ("bobbin cleaning wastage",)),
                    ("conversion", "Conversion wastage", None, ("conversion wastage",)),
                    ("black_bobbin", "Black bobbin cutting / bad beam", None, ("black bobbin",)),
                    ("five_s", "5'S bobbin cleaning", None, ("5's bobbin cleaning", "5s bobbin cleaning")),
                ],
                "kg",
            ),
            group="Warping",
            headings=("warping wastage",),
        ),
        Section(
            "warping_manpower",
            "Warping manpower",
            _rows(
                "Total",
                [
                    ("mandays", "a) Mandays", None, ("warping mandays", "warping man days", "mandays", "man days")),
                    ("overtime", "b) Overtime", None, ("overtime",)),
                    ("sectional", "Sectional warping", None, ("sectional warping",)),
                ],
            ),
            group="Warping",
            headings=("warping manpower",),
        ),
    )
)

BY_KEY = {s.key: s for s in SECTIONS}
NOTE_LABELS = (
    "Beam fall m/c no.",
    "New machine started",
    "Stop machine for starting",
    "Bad beam m/c no.",
    "Warp cut m/c no.",
    "Weft cut m/c no.",
    "Major breakdown",
    "Other",
)
PARAM_LABEL = {
    "installed": "Installed looms",
    "rate": "Picks/hour rate (efficiency)",
    "theo_rate": "Picks/hour rate (theoretical picks row)",
}


def section(key: str) -> Section:
    return BY_KEY[key]


def default_target(section_key: str, metric_key: str) -> Decimal | None:
    s = BY_KEY.get(section_key)
    if s is None:
        return None
    if metric_key.startswith("_"):
        v = s.params.get(metric_key[1:])
    else:
        m = s.metric(metric_key)
        v = m.target if m else None
    return Decimal(v) if v is not None else None


def input_cells() -> list[tuple[str, str, str]]:
    """Every (section, metric, shift) a worker may write."""
    return [(s.key, m.key, sh) for s in SECTIONS for m in s.metrics if m.kind == "input" for sh in s.shifts]
