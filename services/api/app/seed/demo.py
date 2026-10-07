"""Demo/test seed: one company, the seven initial departments (configurable examples only), machines,
aliases, unit aliases, development memberships and the F1 fixture as approved records.

Runs on the owner connection. All values are ILLUSTRATIVE; nothing here is verified production data.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Connection, insert, select, update

from app.audit import service as audit
from app.db import tables as t
from app.domain.units import DEFAULT_UNIT_ALIASES, normalize_alias

F1_PATH = Path(__file__).resolve().parents[4] / "packages/contracts/fixtures/f1_records.json"

# (code, name, machines). Initial examples from spec A6, subject to company confirmation.
DEPARTMENTS = [
    ("TAPELINE", "Tapeline", ["T-01", "T-02", "T-03", "T-04"]),
    ("WARPING", "Warping", ["W-01", "W-02"]),
    ("SULZER_FABRIC", "Sulzer Fabric", ["SF-01", "SF-02"]),
    ("LAMINATION", "Lamination", ["L-01", "L-02"]),
    ("MULTIFILAMENT", "Multifilament", ["MF-01", "MF-02", "MF-03"]),
    ("DISPATCH", "Dispatch", ["D-01"]),
    ("PURCHASE", "Purchase", []),
]
DEPARTMENT_ALIASES = {"Tape line": "TAPELINE", "Tapline": "TAPELINE", "Sulzer": "SULZER_FABRIC",
                      "Multi filament": "MULTIFILAMENT", "Lamination dept": "LAMINATION"}  # fmt: skip
OPERATORS = {"Rajesh": "TAPELINE", "Suresh": "WARPING", "Meena": "LAMINATION", "Imran": "DISPATCH",
             "Kavita": "MULTIFILAMENT"}  # fmt: skip

ALL = "*"
# subject -> (display name, roles, department codes). Development identities only.
DEV_USERS = {
    "dev-admin": ("Dev Administrator", ["ADMIN"], ALL),
    "dev-reviewer": ("Dev Reviewer", ["REVIEWER", "UPLOADER"], ALL),
    "dev-uploader": ("Dev Uploader (Tapeline)", ["UPLOADER"], ["TAPELINE"]),
    "dev-sender": ("Dev Sender", ["SENDER"], ALL),
    "dev-viewer": ("Dev Viewer (Tapeline, Warping)", ["VIEWER"], ["TAPELINE", "WARPING"]),
}


@dataclass
class SeedResult:
    tenant_id: uuid.UUID
    departments: dict[str, uuid.UUID] = field(default_factory=dict)
    machines: dict[str, uuid.UUID] = field(default_factory=dict)
    users: dict[str, uuid.UUID] = field(default_factory=dict)
    records: list[uuid.UUID] = field(default_factory=list)


def seed_tenant(conn: Connection, name: str, *, subject_prefix: str = "", with_f1: bool = True) -> SeedResult:
    tenant_id = uuid.uuid4()
    conn.execute(insert(t.tenant).values(id=tenant_id, name=name, timezone="Asia/Kolkata", date_order="DMY"))
    res = SeedResult(tenant_id)

    for order, (code, dname, machines) in enumerate(DEPARTMENTS):
        dep_id = uuid.uuid4()
        res.departments[code] = dep_id
        conn.execute(insert(t.department).values(id=dep_id, tenant_id=tenant_id, code=code, name=dname,
                                                 sort_order=order * 10))  # fmt: skip
        for mcode in machines:
            m_id = uuid.uuid4()
            res.machines[mcode] = m_id
            conn.execute(insert(t.machine).values(id=m_id, tenant_id=tenant_id, department_id=dep_id, code=mcode))
            conn.execute(insert(t.master_alias).values(
                id=uuid.uuid4(), tenant_id=tenant_id, alias=mcode.replace("-", ""),
                alias_key=normalize_alias(mcode.replace("-", "")), machine_id=m_id))  # fmt: skip

    for alias, code in DEPARTMENT_ALIASES.items():
        conn.execute(insert(t.master_alias).values(id=uuid.uuid4(), tenant_id=tenant_id, alias=alias,
                                                   alias_key=normalize_alias(alias),
                                                   department_id=res.departments[code]))  # fmt: skip
    for oname, code in OPERATORS.items():
        conn.execute(insert(t.operator).values(id=uuid.uuid4(), tenant_id=tenant_id, name=oname,
                                               department_id=res.departments[code]))  # fmt: skip
    conn.execute(
        insert(t.unit_alias),
        [{"id": uuid.uuid4(), "tenant_id": tenant_id, "alias": k, "alias_key": k, "unit": u.value, "factor": f}
         for k, (u, f) in DEFAULT_UNIT_ALIASES.items()],
    )  # fmt: skip

    for subject, (display, roles, codes) in DEV_USERS.items():
        user_id = uuid.uuid4()
        res.users[subject] = user_id
        conn.execute(insert(t.membership).values(
            id=user_id, tenant_id=tenant_id, subject=subject_prefix + subject, display_name=display,
            email=f"{subject}@example.invalid", roles=roles))  # fmt: skip
        dept_ids = res.departments.values() if codes == ALL else [res.departments[c] for c in codes]
        conn.execute(
            insert(t.membership_department),
            [{"tenant_id": tenant_id, "membership_id": user_id, "department_id": d} for d in dept_ids],
        )

    if with_f1:
        res.records = seed_f1(conn, res)
    return res


def seed_f1(conn: Connection, res: SeedResult) -> list[uuid.UUID]:
    fixture = json.loads(F1_PATH.read_text(encoding="utf-8"))
    approver = res.users["dev-reviewer"]
    approved_at = datetime.now(UTC)
    ids = []
    for rec in fixture["records"]:
        record_id, revision_id = uuid.uuid4(), uuid.uuid4()
        dep = res.departments[rec["department_code"]]
        prod_date = date.fromisoformat(fixture["production_date"])
        conn.execute(insert(t.production_record).values(
            id=record_id, tenant_id=res.tenant_id, department_id=dep, current_revision_id=revision_id,
            production_date=prod_date, created_by=approver))  # fmt: skip
        conn.execute(insert(t.record_revision).values(
            id=revision_id, tenant_id=res.tenant_id, record_id=record_id, number=1, production_date=prod_date,
            department_id=dep, machine_id=res.machines[rec["machine_code"]], operator_name=rec["operator_name"],
            production_qty=Decimal(rec["production_qty"]), target_qty=Decimal(rec["target_qty"]),
            unit=fixture["unit"], status=rec["status"], stop_minutes=rec["stop_minutes"], remarks=rec["remarks"],
            provenance={"source": "seed:F1", "illustrative": True}, approval_state="APPROVED",
            created_by=approver, approved_by=approver, approved_at=approved_at))  # fmt: skip
        audit.record(conn, tenant_id=res.tenant_id, actor=audit.SYSTEM, action="RECORD_SEEDED",
                     object_type="production_record", object_id=record_id, object_revision=1,
                     after={"fixture": "F1", "department": rec["department_code"]})  # fmt: skip
        ids.append(record_id)
    conn.execute(update(t.tenant).where(t.tenant.c.id == res.tenant_id)
                 .values(data_version=t.tenant.c.data_version + 1))  # fmt: skip
    return ids


def tenant_exists(conn: Connection, name: str) -> bool:
    return conn.execute(select(t.tenant.c.id).where(t.tenant.c.name == name)).first() is not None
