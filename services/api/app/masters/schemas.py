"""Master-data input contracts (API operation 55). Codes are normalized to upper case."""

import uuid
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from app.domain.enums import Unit

Name80 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Name120 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


def _upper(v: object) -> object:
    # Normalize before the pattern check so "qa-lab" is accepted as "QA-LAB".
    return v.strip().upper() if isinstance(v, str) else v


DeptCode = Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9_-]{0,39}$")]
MachineCode = Annotated[str, BeforeValidator(_upper), StringConstraints(min_length=1, max_length=40)]


def _reject_float(v: object) -> object:
    # Binary floats cannot represent factors such as 0.01 exactly.
    if isinstance(v, float):
        raise ValueError('send factor as a decimal string, e.g. "0.01"')
    return v


Factor = Annotated[Decimal, BeforeValidator(_reject_float), Field(gt=0, max_digits=20, decimal_places=10)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DepartmentIn(_Strict):
    code: DeptCode
    name: Name80
    active: bool = True
    expected_daily_submission: bool = True
    sort_order: int = Field(default=0, ge=0, le=10_000)


class DepartmentPatch(_Strict):
    code: DeptCode | None = None
    name: Name80 | None = None
    active: bool | None = None
    expected_daily_submission: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)


class MachineIn(_Strict):
    code: MachineCode
    name: Name120 | None = None
    department_id: uuid.UUID
    active: bool = True


class MachinePatch(_Strict):
    code: MachineCode | None = None
    name: Name120 | None = None
    department_id: uuid.UUID | None = None
    active: bool | None = None


class OperatorIn(_Strict):
    name: Name120
    department_id: uuid.UUID | None = None
    active: bool = True


class OperatorPatch(_Strict):
    name: Name120 | None = None
    department_id: uuid.UUID | None = None
    active: bool | None = None


class UnitAliasIn(_Strict):
    alias: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    unit: Unit
    factor: Factor


class UnitAliasPatch(_Strict):
    unit: Unit | None = None
    factor: Factor | None = None
    active: bool | None = None


class AliasIn(_Strict):
    kind: Literal["department", "machine", "operator"]
    alias: Name120
    target_id: uuid.UUID


CREATE_MODELS = {
    "departments": DepartmentIn,
    "machines": MachineIn,
    "operators": OperatorIn,
    "unit-aliases": UnitAliasIn,
    "aliases": AliasIn,
}
PATCH_MODELS = {
    "departments": DepartmentPatch,
    "machines": MachinePatch,
    "operators": OperatorPatch,
    "unit-aliases": UnitAliasPatch,
}
