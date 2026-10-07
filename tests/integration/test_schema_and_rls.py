"""Database invariants: schema drift, tenant isolation (RLS), immutability and referential rules.
Covers FR01/TC01 (isolation at the DB layer), FR10 (immutable revisions), FR23 (machine/department).
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import tables as t
from app.db.engine import app_engine, auth_tx, dispatcher_tx, tenant_tx

pytestmark = pytest.mark.db


def test_core_tables_match_migrated_schema(owner_engine):
    insp = inspect(owner_engine)
    for table in t.metadata.sorted_tables:
        db_cols = {c["name"] for c in insp.get_columns(table.name)}
        assert set(table.c.keys()) <= db_cols, f"{table.name} drifted: {set(table.c.keys()) - db_cols}"


def test_rls_enabled_and_forced_on_business_tables(owner_engine):
    with owner_engine.connect() as conn:
        rows = dict(conn.execute(text(
            "SELECT relname, relrowsecurity AND relforcerowsecurity FROM pg_class "
            "WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace")).all())  # fmt: skip
    for name in ("department", "machine", "membership", "production_record", "record_revision",
                 "audit_event", "outbox", "job", "idempotency_record", "tenant"):  # fmt: skip
        assert rows[name], f"RLS not enforced on {name}"


def test_runtime_role_is_not_privileged():
    with app_engine().connect() as conn:
        su, bypass = conn.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()  # fmt: skip
    assert not su and not bypass


def test_tenant_isolation(seeded, owner_engine):
    from app.seed.demo import seed_tenant

    with owner_engine.begin() as conn:
        other = seed_tenant(conn, f"Other {uuid.uuid4().hex[:6]}", subject_prefix=uuid.uuid4().hex[:6] + "-")

    with tenant_tx(seeded.tenant_id) as conn:
        dept_ids = set(conn.execute(select(t.department.c.id)).scalars())
        assert dept_ids == set(seeded.departments.values())
        assert conn.execute(select(t.production_record.c.id)).scalars().all()
        # Direct lookups of another tenant's IDs return nothing.
        assert (
            conn.execute(select(t.production_record).where(t.production_record.c.id == other.records[0])).first()
            is None
        )
        with pytest.raises(DBAPIError):  # WITH CHECK: cannot write rows for another tenant
            conn.execute(insert(t.department).values(id=uuid.uuid4(), tenant_id=other.tenant_id, code="X", name="X"))

    with app_engine().connect() as conn:  # no tenant context at all -> nothing visible
        assert conn.execute(select(t.department.c.id)).first() is None
        assert conn.execute(select(t.membership.c.id)).first() is None


def test_cross_scope_access_is_limited():
    with auth_tx() as conn:  # sign-in scope sees memberships but no business data
        assert conn.execute(select(t.membership.c.id)).first() is not None
        assert conn.execute(select(t.production_record.c.id)).first() is None
    with dispatcher_tx() as conn:  # dispatcher scope sees no business data either
        assert conn.execute(select(t.department.c.id)).first() is None
        assert conn.execute(select(t.membership.c.id)).first() is None


def test_audit_is_append_only_for_runtime_role(seeded):
    with pytest.raises(DBAPIError), tenant_tx(seeded.tenant_id) as conn:
        conn.execute(update(t.audit_event).values(action="TAMPERED"))
    with pytest.raises(DBAPIError), tenant_tx(seeded.tenant_id) as conn:
        conn.execute(t.audit_event.delete())


def test_approved_revision_is_immutable_and_undeletable(seeded):
    with pytest.raises(DBAPIError, match="immutable"), tenant_tx(seeded.tenant_id) as conn:
        conn.execute(update(t.record_revision).values(production_qty=Decimal("1300")))
    with pytest.raises(DBAPIError), tenant_tx(seeded.tenant_id) as conn:
        conn.execute(t.record_revision.delete())


def _revision_values(seeded, record_id, **overrides):
    values = dict(
        id=uuid.uuid4(), tenant_id=seeded.tenant_id, record_id=record_id, number=1, production_date=date(2026, 9, 26),
        department_id=seeded.departments["TAPELINE"], machine_id=seeded.machines["T-01"], operator_name="Test",
        production_qty=Decimal("10"), target_qty=Decimal("10"), unit="m", status="RUNNING", stop_minutes=0,
        created_by=seeded.users["dev-reviewer"],
    )  # fmt: skip
    return values | overrides


def test_machine_must_belong_to_revision_department(seeded):
    record_id = uuid.uuid4()
    with pytest.raises(IntegrityError), tenant_tx(seeded.tenant_id) as conn:
        conn.execute(insert(t.production_record).values(
            id=record_id, tenant_id=seeded.tenant_id, department_id=seeded.departments["TAPELINE"],
            production_date=date(2026, 9, 26), created_by=seeded.users["dev-reviewer"]))  # fmt: skip
        conn.execute(
            insert(t.record_revision).values(**_revision_values(seeded, record_id, machine_id=seeded.machines["W-01"]))
        )  # Warping machine


def test_active_record_requires_an_approved_revision(seeded):
    record_id, rev_id = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(IntegrityError, match="approved revision"), tenant_tx(seeded.tenant_id) as conn:
        conn.execute(
            insert(t.production_record).values(
                id=record_id,
                tenant_id=seeded.tenant_id,
                department_id=seeded.departments["TAPELINE"],
                current_revision_id=rev_id,
                production_date=date(2026, 9, 26),
                created_by=seeded.users["dev-reviewer"],
            )
        )
        conn.execute(insert(t.record_revision).values(**_revision_values(seeded, record_id, id=rev_id)))  # PENDING


@pytest.mark.parametrize(
    "overrides",
    [
        {"production_qty": Decimal("-1")},
        {"stop_minutes": 1441},
        {"unit": "yd"},
        {"status": "STOPPED"},
        {"unit": "pcs", "production_qty": Decimal("1.5")},
        {"operator_name": "  "},
    ],
)
def test_revision_check_constraints(seeded, overrides):  # TC13/TC14 at the DB layer
    record_id = uuid.uuid4()
    with pytest.raises(IntegrityError), tenant_tx(seeded.tenant_id) as conn:
        conn.execute(insert(t.production_record).values(
            id=record_id, tenant_id=seeded.tenant_id, department_id=seeded.departments["TAPELINE"],
            production_date=date(2026, 9, 26), created_by=seeded.users["dev-reviewer"]))  # fmt: skip
        conn.execute(insert(t.record_revision).values(**_revision_values(seeded, record_id, **overrides)))
