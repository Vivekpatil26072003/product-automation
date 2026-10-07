"""Unit normalization to canonical m / kg / pcs (spec §4).

Aliases are tenant master data (unit_alias table). DEFAULT_UNIT_ALIASES seeds new tenants and
backs pure tests; it is not an immutable list.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.enums import UNIT_DIMENSION, Unit
from app.domain.issues import FieldIssue
from app.domain.quantities import quantize

# alias (normalized) -> (canonical unit, multiplication factor)
DEFAULT_UNIT_ALIASES: dict[str, tuple[Unit, Decimal]] = {
    "m": (Unit.M, Decimal(1)),
    "meter": (Unit.M, Decimal(1)),
    "meters": (Unit.M, Decimal(1)),
    "metre": (Unit.M, Decimal(1)),
    "metres": (Unit.M, Decimal(1)),
    "mtr": (Unit.M, Decimal(1)),
    "mtrs": (Unit.M, Decimal(1)),
    "cm": (Unit.M, Decimal("0.01")),
    "mm": (Unit.M, Decimal("0.001")),
    "km": (Unit.M, Decimal(1000)),
    "kg": (Unit.KG, Decimal(1)),
    "kgs": (Unit.KG, Decimal(1)),
    "kilogram": (Unit.KG, Decimal(1)),
    "kilograms": (Unit.KG, Decimal(1)),
    "g": (Unit.KG, Decimal("0.001")),
    "gm": (Unit.KG, Decimal("0.001")),
    "gram": (Unit.KG, Decimal("0.001")),
    "grams": (Unit.KG, Decimal("0.001")),
    "pcs": (Unit.PCS, Decimal(1)),
    "pc": (Unit.PCS, Decimal(1)),
    "piece": (Unit.PCS, Decimal(1)),
    "pieces": (Unit.PCS, Decimal(1)),
    "nos": (Unit.PCS, Decimal(1)),
}


def normalize_alias(text: str) -> str:
    """Canonical lookup key for any master-data alias: lower-case, dots removed, single spaces."""
    return " ".join(text.strip().lower().replace(".", "").split())


@dataclass
class UnitResult:
    unit: Unit | None
    factor: Decimal | None
    issues: list[FieldIssue] = field(default_factory=list)


def resolve_unit(raw: str | None, aliases: Mapping[str, tuple[Unit, Decimal]] = DEFAULT_UNIT_ALIASES) -> UnitResult:
    if raw is None or not raw.strip():
        return UnitResult(None, None, [FieldIssue("unit", "MISSING_VALUE", "Unit is required.")])
    hit = aliases.get(normalize_alias(raw))
    if hit is None:
        return UnitResult(None, None, [FieldIssue("unit", "UNKNOWN_UNIT", f'"{raw}" is not a configured unit.')])
    return UnitResult(hit[0], hit[1])


@dataclass
class ConvertedPair:
    unit: Unit | None
    production_qty: Decimal | None
    target_qty: Decimal | None
    issues: list[FieldIssue] = field(default_factory=list)


def _convert(value: Decimal, factor: Decimal, field_name: str) -> tuple[Decimal, list[FieldIssue]]:
    exact = value * factor
    q = quantize(exact)
    if q != exact:
        return q, [FieldIssue(field_name, "PRECISION_LOST", f"Conversion rounded {exact} to {q}.", "warning")]
    return q, []


def convert_pair(
    production_qty: Decimal | None,
    production_unit: UnitResult,
    target_qty: Decimal | None,
    target_unit: UnitResult | None = None,
) -> ConvertedPair:
    """Convert a record's quantities to one canonical unit; reject dimension mismatches (TC13).

    target_unit defaults to production_unit when the note states a single unit for the row.
    """
    target_unit = target_unit or production_unit
    issues = [*production_unit.issues]
    if target_unit is not production_unit:
        issues.extend(target_unit.issues)
    if production_unit.unit is None or target_unit.unit is None:
        return ConvertedPair(None, None, None, issues)
    if UNIT_DIMENSION[production_unit.unit] != UNIT_DIMENSION[target_unit.unit]:
        issues.append(
            FieldIssue(
                "unit",
                "DIMENSION_MISMATCH",
                f"Production is in {production_unit.unit} but target is in {target_unit.unit}.",
            )
        )
        return ConvertedPair(None, None, None, issues)

    prod = targ = None
    if production_qty is not None:
        prod, extra = _convert(production_qty, production_unit.factor, "production_qty")
        issues.extend(extra)
    if target_qty is not None:
        targ, extra = _convert(target_qty, target_unit.factor, "target_qty")
        issues.extend(extra)

    unit = production_unit.unit
    if unit is Unit.PCS:
        for name, v in (("production_qty", prod), ("target_qty", targ)):
            if v is not None and v != v.to_integral_value():
                issues.append(FieldIssue(name, "FRACTIONAL_PCS", "Pieces must be a whole number."))
    return ConvertedPair(unit, prod, targ, issues)
