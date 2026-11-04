"""Declarative base and engine/session factory.

Every SQLite connection gets: ``foreign_keys=ON`` (SQLite disables FK
enforcement by default; we need it for AC1), ``journal_mode=WAL`` and a
``busy_timeout`` so that the web process and the MCP process can write the same
``cairn.db`` without tripping over each other's locks.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Base class for all cairn ORM models."""


def make_engine(db_url: str = "sqlite://") -> Engine:
    """Create an engine; sets the SQLite pragmas every connection needs.

    On the NAS the web app and the MCP server are two processes sharing one
    ``cairn.db``. WAL lets a reader proceed while the other process writes, and
    ``busy_timeout`` makes a second writer wait for the lock instead of failing
    immediately with "database is locked". ``synchronous=NORMAL`` is the
    durability/throughput trade-off SQLite recommends for WAL.
    """
    engine = create_engine(db_url, future=True)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, connection_record) -> None:
            cursor = dbapi_connection.cursor()
            # The journal mode has to be switched before the sync level, and it
            # silently stays "memory" for in-memory databases (tests).
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory bound to *engine*."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """Create missing tables, then bring an existing DB up to the current schema.

    ``create_all`` only creates tables it does not already know about — it never
    adds a column to an existing table. So every schema change must ship a
    migration in ``cairn.migrations`` or old databases silently keep the old
    schema. A brand-new DB is stamped straight to the current version; an
    existing one walks the migration chain.
    """
    from sqlalchemy import inspect

    from cairn import migrations, models  # noqa: F401  (register mappers)

    inspector = inspect(engine)
    # "fresh" means literally no cairn table yet. Checking one table (e.g.
    # learner) would mis-classify a DB created by an older/partial build and
    # skip the very migration that was supposed to fix it.
    fresh = not any(inspector.has_table(table.name) for table in Base.metadata.sorted_tables)
    Base.metadata.create_all(engine)
    if fresh:
        migrations.stamp_version(engine)
        # 新库不跑迁移链，默认策略种子行在这里补（老库由 v12 迁移补），
        # 保证两种建库路径的 strategy 目录一致。
        with engine.begin() as conn:
            migrations.ensure_default_strategy_row(conn)
    else:
        migrations.apply_migrations(engine)
