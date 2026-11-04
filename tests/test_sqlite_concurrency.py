"""Two processes, one cairn.db — the pragmas that make that safe.

On the NAS the web app and the MCP server are separate processes sharing a single
SQLite file. ``make_engine`` therefore has to put every connection into WAL mode
with a busy timeout, while keeping foreign-key enforcement on.
"""

from __future__ import annotations

from sqlalchemy import select

from cairn.models import Learner, init_db, make_engine, make_session_factory


def _url(tmp_path):
    return f"sqlite:///{tmp_path / 'cairn.db'}"


def test_journal_mode_is_wal(tmp_path):
    engine = make_engine(_url(tmp_path))
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
    finally:
        engine.dispose()


def test_two_engines_write_same_file(tmp_path):
    url = _url(tmp_path)
    web_engine = make_engine(url)
    init_db(web_engine)
    # A second engine has its own connection pool — the same shape as the MCP
    # process opening the file that the web process already created.
    mcp_engine = make_engine(url)
    try:
        for engine, name in ((web_engine, "网页进程"), (mcp_engine, "语音进程")):
            session = make_session_factory(engine)()
            try:
                session.add(Learner(name=name, grade_level="primary-3"))
                session.commit()
            finally:
                session.close()

        with make_session_factory(web_engine)() as session:
            names = sorted(session.scalars(select(Learner.name)))
        assert names == ["网页进程", "语音进程"]
    finally:
        web_engine.dispose()
        mcp_engine.dispose()


def test_connection_pragmas(tmp_path):
    engine = make_engine(_url(tmp_path))
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
            assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        engine.dispose()
