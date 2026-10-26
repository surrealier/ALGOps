"""The Alembic migrations produce exactly the ORM schema (no drift between models and versions)."""

from __future__ import annotations

import os

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

import algops.tracking.models  # noqa: F401
from algops.tracking.db import Base

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_migrations_match_models(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'm.sqlite'}"
    monkeypatch.setenv("ALGOPS_MASTER_DB_URL", url)
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models changed without a migration: {diff}"
