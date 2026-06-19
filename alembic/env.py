"""Alembic environment — targets the tracking ORM metadata; DB url from MasterSettings."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

import algops.tracking.models  # noqa: F401  (register models on Base.metadata)
from algops.core import MasterSettings
from algops.tracking.db import Base

target_metadata = Base.metadata


def _url() -> str:
    return MasterSettings().db_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url())
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
