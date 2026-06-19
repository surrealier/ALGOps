"""Database engine / session wiring for the master tracking store.

Dev default is SQLite (no server required); prod uses PostgreSQL via the same ``db_url``.
``init_db`` uses ``create_all`` for the dev bootstrap; the Alembic env under ``/alembic`` is
the production migration path.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from algops.core import MasterSettings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def _ensure_sqlite_dir(db_url: str) -> None:
    prefix = "sqlite"
    if db_url.startswith(prefix):
        # sqlite+pysqlite:///./.algops/tracking.sqlite  ->  ./.algops/tracking.sqlite
        path = db_url.split(":///", 1)[-1]
        if path and path not in (":memory:",):
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)


def init_engine(settings: MasterSettings | None = None):
    """Idempotently build the engine + session factory and create tables (dev)."""
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine
    settings = settings or MasterSettings()
    _ensure_sqlite_dir(settings.db_url)
    connect_args = {"check_same_thread": False} if settings.db_url.startswith("sqlite") else {}
    _engine = create_engine(settings.db_url, connect_args=connect_args, future=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)

    # Import models so they register on Base.metadata, then create_all (dev bootstrap).
    from . import models  # noqa: F401

    Base.metadata.create_all(_engine)
    return _engine


def reset_engine() -> None:
    """Dispose the engine + session factory (test isolation between distinct DBs)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def get_session() -> Iterator[Session]:
    """Transactional session scope: commit on success, rollback on error."""
    factory = session_factory()
    sess = factory()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()
