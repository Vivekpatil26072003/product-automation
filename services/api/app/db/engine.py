"""Engines and transaction scopes.

Every runtime transaction declares its scope before touching data:
- tenant_tx(tenant_id): ordinary work; RLS limits every business table to that tenant.
- auth_tx(): session/sign-in resolution before the tenant is known (tenant, membership, auth_session).
- dispatcher_tx(): outbox dispatch and job claiming across tenants (outbox, job, job_attempt only).
The settings are transaction-local (set_config(..., true)), so they cannot leak between pooled uses.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine, text

from app.core.config import get_settings


@lru_cache(maxsize=8)
def engine_for(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10, future=True)


def app_engine() -> Engine:
    return engine_for(get_settings().database_url)


def _set(conn: Connection, tenant_id: uuid.UUID | None, scope: str) -> None:
    conn.execute(
        text("SELECT set_config('app.tenant_id', :t, true), set_config('app.scope', :s, true)"),
        {"t": str(tenant_id) if tenant_id else "", "s": scope},
    )


def set_tenant(conn: Connection, tenant_id: uuid.UUID) -> None:
    """Switch an open transaction to a single tenant and drop any cross-tenant scope."""
    _set(conn, tenant_id, "")


@contextmanager
def tenant_tx(tenant_id: uuid.UUID, engine: Engine | None = None, isolation: str | None = None) -> Iterator[Connection]:
    """isolation="REPEATABLE READ" gives every statement one snapshot (report snapshots, spec §12)."""
    eng = engine or app_engine()
    if isolation:
        eng = eng.execution_options(isolation_level=isolation)
    with eng.begin() as conn:
        _set(conn, tenant_id, "")
        yield conn


@contextmanager
def auth_tx(engine: Engine | None = None) -> Iterator[Connection]:
    with (engine or app_engine()).begin() as conn:
        _set(conn, None, "auth")
        yield conn


@contextmanager
def dispatcher_tx(engine: Engine | None = None) -> Iterator[Connection]:
    with (engine or app_engine()).begin() as conn:
        _set(conn, None, "dispatcher")
        yield conn
