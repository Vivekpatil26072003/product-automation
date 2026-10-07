"""Candidate normalization and validation (FR06, FR08; spec §4, §8 "Normalization").

Input per field is either an extracted source string (with evidence IDs) or a canonical value typed by
a reviewer. Output is the stored candidate field state plus field-level issues. Rules:

- Unknown or unreadable values stay null with a blocking issue; nothing is defaulted or guessed.
- Department and machine resolve only by exact match of code, name or a reviewed alias (compared after
  removing case, spaces and punctuation). No similarity match ever becomes a master ID.
- A machine must belong to the chosen department.
- Quantities are converted with the M1 decimal and unit rules; totals are never computed here.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.dates import parse_production_date
from app.domain.durations import parse_stop_minutes
from app.domain.enums import Status, Unit
from app.domain.issues import FieldIssue
from app.domain.quantities import canonical_decimal, decimal_string, parse_quantity
from app.domain.status import normalize_status
from app.domain.units import DEFAULT_UNIT_ALIASES, convert_pair, resolve_unit

# Canonical record fields (API RecordFields) and the extraction schema field feeding each.
FIELDS = [
    "production_date",
    "department_id",
    "operator_name",
    "machine_id",
    "production_qty",
    "target_qty",
    "unit",
    "status",
    "stop_minutes",
    "remarks",
]
EXTRACTED_NAME = {"department_id": "department", "machine_id": "machine"}
REQUIRED = [f for f in FIELDS if f != "remarks"]
CRITICAL = {"production_date", "machine_id", "production_qty", "target_qty", "unit"}  # spec §14


def compact_key(text: str) -> str:
    """Match key for master data: case, spaces and punctuation removed ("Tape line" == "TAPELINE")."""
    return re.sub(r"[\W_]+", "", text.casefold())


@dataclass
class MasterContext:
    departments: dict[uuid.UUID, tuple[str, str]]  # id -> (code, name)
    machines: dict[uuid.UUID, tuple[str, uuid.UUID]]  # id -> (code, department_id)
    department_keys: dict[str, uuid.UUID]
    machine_keys: dict[str, uuid.UUID]
    unit_aliases: Mapping[str, tuple[Unit, Decimal]] = field(default_factory=lambda: DEFAULT_UNIT_ALIASES)
    date_order: str = "DMY"
    today: date = field(default_factory=date.today)


@dataclass
class FieldInput:
    """One field before normalization. `raw` for extracted text; `value` for reviewer-entered canonical."""

    raw: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    extractor_issues: list[str] = field(default_factory=list)
    source: str = "extracted"  # extracted | reviewer | upload_context | manual
    value: Any = None


_QTY_UNIT = re.compile(r"^\s*(?P<num>[+-]?[\d][\d.,\s]*?)\s*(?P<unit>[A-Za-z][A-Za-z. ]{0,14})?\s*$")


def split_quantity(raw: str | None) -> tuple[str | None, str | None]:
    """ "1250 m" -> ("1250", "m"); "1,250" -> ("1,250", None); unparseable text is returned unchanged."""
    if raw is None:
        return None, None
    m = _QTY_UNIT.match(raw)
    if not m:
        return raw, None
    unit = (m["unit"] or "").strip() or None
    return m["num"].strip(), unit


def _issue(field_name: str, code: str, message: str, severity: str = "error") -> FieldIssue:
    return FieldIssue(field_name, code, message, severity)


@dataclass
class Normalized:
    fields: dict[str, dict[str, Any]]
    issues: list[FieldIssue]

    @property
    def blocking(self) -> list[FieldIssue]:
        return [i for i in self.issues if i.blocking]

    def issues_json(self) -> list[dict[str, str | None]]:
        return [{"field": i.field, "code": i.code, "message": i.message, "severity": i.severity} for i in self.issues]


def _state(inp: FieldInput, value: Any, display: str | None) -> dict[str, Any]:
    return {
        "raw": inp.raw,
        "value": value,
        "display": display,
        "evidence_ids": list(inp.evidence_ids),
        "source": inp.source,
    }


def normalize(inputs: Mapping[str, FieldInput], ctx: MasterContext) -> Normalized:
    """Normalize every canonical field. Missing inputs are treated as absent (null)."""
    get = lambda name: inputs.get(name) or FieldInput()  # noqa: E731
    issues: list[FieldIssue] = []
    out: dict[str, dict[str, Any]] = {}

    for name in FIELDS:
        inp = get(name)
        for code in inp.extractor_issues:
            if code != "MISSING_VALUE":  # missing values are reported by the field rule below
                issues.append(_issue(name, code, "The extractor flagged this value for review.", "warning"))

    # --- date ---
    inp = get("production_date")
    if inp.source == "reviewer":
        try:
            d = date.fromisoformat(str(inp.value))
            if d > ctx.today:
                issues.append(_issue("production_date", "FUTURE_DATE", "Production date cannot be in the future."))
            out["production_date"] = _state(inp, d.isoformat(), d.isoformat())
        except (TypeError, ValueError):
            issues.append(_issue("production_date", "INVALID_DATE", "Enter a date as YYYY-MM-DD."))
            out["production_date"] = _state(inp, None, None)
    else:
        r = parse_production_date(inp.raw, ctx.today, ctx.date_order)  # type: ignore[arg-type]
        issues.extend(r.issues)
        out["production_date"] = _state(
            inp, r.value.isoformat() if r.value else None, r.value.isoformat() if r.value else None
        )

    # --- department ---
    inp = get("department_id")
    dept_id = _resolve_master(inp, "department_id", ctx.department_keys, ctx.departments, issues, "department")
    if inp.source == "upload_context" and dept_id:
        issues.append(
            _issue(
                "department_id",
                "FROM_UPLOAD_CONTEXT",
                "The note does not name a department; the upload's department is proposed.",
                "warning",
            )
        )
    code_name = ctx.departments.get(dept_id) if dept_id else None
    out["department_id"] = _state(inp, str(dept_id) if dept_id else None, code_name[1] if code_name else None)

    # --- machine ---
    inp = get("machine_id")
    machine_id = _resolve_master(inp, "machine_id", ctx.machine_keys, ctx.machines, issues, "machine")
    machine = ctx.machines.get(machine_id) if machine_id else None
    if machine and dept_id and machine[1] != dept_id:
        issues.append(
            _issue(
                "machine_id",
                "MACHINE_DEPARTMENT_MISMATCH",
                f"Machine {machine[0]} does not belong to the chosen department.",
            )
        )
    out["machine_id"] = _state(inp, str(machine_id) if machine_id else None, machine[0] if machine else None)

    # --- operator ---
    inp = get("operator_name")
    text = (inp.value if inp.source == "reviewer" else inp.raw) or ""
    text = " ".join(str(text).split())
    if not text:
        issues.append(_issue("operator_name", "MISSING_VALUE", "Operator name is required."))
    elif len(text) > 120:
        issues.append(_issue("operator_name", "TOO_LONG", "Operator name must be at most 120 characters."))
    out["operator_name"] = _state(inp, text or None, text or None)

    # --- quantities and unit ---
    _quantities(get("production_qty"), get("target_qty"), get("unit"), ctx, issues, out)

    # --- status ---
    inp = get("status")
    if inp.source == "reviewer":
        s = inp.value if inp.value in {x.value for x in Status} else None
        if s is None:
            issues.append(_issue("status", "UNKNOWN_STATUS", "Choose RUNNING, COMPLETED, PENDING or HOLD."))
    else:
        r = normalize_status(inp.raw)
        issues.extend(r.issues)
        s = r.value.value if r.value else None
    out["status"] = _state(inp, s, s)

    # --- stop minutes ---
    inp = get("stop_minutes")
    if inp.source == "reviewer":
        val = inp.value
        is_int = isinstance(val, int) and not isinstance(val, bool)
        r = parse_stop_minutes(val if is_int else (None if val is None else str(val)))
    else:
        r = parse_stop_minutes(inp.raw)
    issues.extend(r.issues)
    out["stop_minutes"] = _state(inp, r.value, None if r.value is None else f"{r.value} min")

    # --- remarks ---
    inp = get("remarks")
    remarks = str((inp.value if inp.source == "reviewer" else inp.raw) or "").strip()
    if len(remarks) > 2000:
        issues.append(_issue("remarks", "TOO_LONG", "Remarks must be at most 2000 characters."))
    out["remarks"] = _state(inp, remarks, remarks)

    return Normalized(out, _dedupe(issues))


def _resolve_master(
    inp: FieldInput, name: str, keys: Mapping[str, uuid.UUID], known: Mapping, issues: list, label: str
) -> uuid.UUID | None:
    if inp.source in ("reviewer", "upload_context"):
        try:
            candidate = uuid.UUID(str(inp.value))
        except (TypeError, ValueError):
            candidate = None
        if candidate in known:
            return candidate
        issues.append(
            _issue(
                name,
                "MISSING_VALUE" if inp.value is None else "UNKNOWN_" + label.upper(),
                f"Choose a {label} from the list.",
            )
        )
        return None
    if not inp.raw or not inp.raw.strip():
        issues.append(_issue(name, "MISSING_VALUE", f"The {label} is required."))
        return None
    hit = keys.get(compact_key(inp.raw))
    if hit is None:
        message = f'"{inp.raw}" is not a known {label}. Choose one, or ask an administrator to add an alias.'
        issues.append(_issue(name, "UNKNOWN_" + label.upper(), message))
    return hit


def _quantities(p: FieldInput, t: FieldInput, u: FieldInput, ctx: MasterContext, issues: list, out: dict) -> None:
    """Extracted text gives the canonical baseline; reviewer-entered fields then replace their part of it."""
    base_issues: list[FieldIssue] = []
    _extracted_quantities(p, t, u, ctx, base_issues, out)
    overridden = {
        name for name, inp in (("production_qty", p), ("target_qty", t), ("unit", u)) if inp.source == "reviewer"
    }
    if not overridden:
        issues.extend(base_issues)
        return
    # Problems of overridden fields no longer apply; a unit/dimension problem is re-judged below.
    issues.extend(i for i in base_issues if i.field not in overridden and i.code != "DIMENSION_MISMATCH")
    unit = u.value if "unit" in overridden else out["unit"]["value"]
    if unit not in {x.value for x in Unit}:
        issues.append(_issue("unit", "MISSING_VALUE" if unit is None else "UNKNOWN_UNIT", "Choose m, kg or pcs."))
        unit = None
    out["unit"] = _state(u, unit, unit)
    for name, inp in (("production_qty", p), ("target_qty", t)):
        if name not in overridden:
            continue
        value, res = inp.value, None
        if value is None or (isinstance(value, str) and not value.strip()):
            issues.append(_issue(name, "MISSING_VALUE", "A value is required."))
        elif isinstance(value, float):
            issues.append(_issue(name, "NOT_A_NUMBER", "Send quantities as decimal strings."))
        else:
            try:
                res = canonical_decimal(Decimal(str(value).replace(",", "")), name)
            except InvalidOperation:
                issues.append(_issue(name, "NOT_A_NUMBER", f'"{value}" is not a number.'))
        if res is not None:
            issues.extend(res.issues)
        out[name] = _state(inp, _dec(res.value) if res else None, _dec(res.value) if res else None)
    if unit == Unit.PCS.value:
        for name in ("production_qty", "target_qty"):
            v = out[name]["value"]
            if v is not None and Decimal(v) != Decimal(v).to_integral_value():
                issues.append(_issue(name, "FRACTIONAL_PCS", "Pieces must be a whole number."))


def _extracted_quantities(
    p: FieldInput, t: FieldInput, u: FieldInput, ctx: MasterContext, issues: list, out: dict
) -> None:
    p_num, p_unit = split_quantity(p.raw)
    t_num, t_unit = split_quantity(t.raw)
    unit_raw = (u.raw or "").strip() or None
    pq = parse_quantity(p_num, "production_qty")
    tq = parse_quantity(t_num, "target_qty")
    if tq.value is None and any(i.code == "MISSING_VALUE" for i in tq.issues):
        message = "Target quantity is required; use 0 only if the source or reviewer confirms no target."
        tq.issues = [_issue("target_qty", "MISSING_VALUE", message)]
    issues.extend(pq.issues + tq.issues)

    prod_unit = resolve_unit(p_unit or unit_raw or t_unit, ctx.unit_aliases)
    targ_unit = resolve_unit(t_unit or unit_raw or p_unit, ctx.unit_aliases)
    pair = convert_pair(pq.value, prod_unit, tq.value, targ_unit)  # each side converts with its own factor
    issues.extend(_dedupe_units(pair.issues))

    unit_value = pair.unit.value if pair.unit else None
    out["production_qty"] = _state(p, _dec(pair.production_qty), _dec(pair.production_qty))
    out["target_qty"] = _state(t, _dec(pair.target_qty), _dec(pair.target_qty))
    out["unit"] = _state(
        u if u.raw else FieldInput(raw=p_unit or t_unit, evidence_ids=p.evidence_ids), unit_value, unit_value
    )


def _dec(v: Decimal | None) -> str | None:
    return None if v is None else decimal_string(v)


def _dedupe_units(items: list[FieldIssue]) -> list[FieldIssue]:
    return [i for i in items if not (i.field == "unit" and i.code == "MISSING_VALUE")] + (
        [_issue("unit", "MISSING_VALUE", "Unit is required (m, kg or pcs).")]
        if any(i.field == "unit" and i.code == "MISSING_VALUE" for i in items)
        else []
    )


def _dedupe(items: list[FieldIssue]) -> list[FieldIssue]:
    seen, out = set(), []
    for i in items:
        if (i.field, i.code) not in seen:
            seen.add((i.field, i.code))
            out.append(i)
    return out


def record_values(fields: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Canonical values for record_revision columns (call only when there are no blocking issues)."""
    v = {name: fields[name]["value"] for name in FIELDS}
    return {
        "production_date": date.fromisoformat(v["production_date"]),
        "department_id": uuid.UUID(v["department_id"]),
        "machine_id": uuid.UUID(v["machine_id"]),
        "operator_name": v["operator_name"],
        "production_qty": Decimal(v["production_qty"]),
        "target_qty": Decimal(v["target_qty"]),
        "unit": v["unit"],
        "status": v["status"],
        "stop_minutes": int(v["stop_minutes"]),
        "remarks": v["remarks"] or "",
    }
