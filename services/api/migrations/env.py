"""Alembic environment. Migrations run as the owner role; the app runs as a non-privileged role."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings


def _url() -> str:
    return context.get_x_argument(as_dictionary=True).get("url") or os.environ.get(
        "DATABASE_OWNER_URL", get_settings().database_owner_url
    )


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
