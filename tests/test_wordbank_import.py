"""教材词库成品入库 — 真实词表导入、幂等、迁移、/books 页面。

数据源是仓库里人工核对过的成品 ``data/wordbank/*.json``（语文/数学），断言
全部对着真实文件，不用自造数据。英语文件当前不存在（任务写"若有"），故无英语用例。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn import book_service, wordbank_import
from cairn.migrations import SCHEMA_VERSION
from cairn.models import Book, init_db, make_engine, make_session_factory
from cairn.web import create_app

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "wordbank"
CHINESE_JSON = DATA_DIR / "语文-人教版-五上.json"
MATH_JSON = DATA_DIR / "数学-人教版-五上.json"

pytestmark = pytest.mark.skipif(
    not CHINESE_JSON.is_file() or not MATH_JSON.is_file(),
    reason="真实词库文件不在 data/wordbank/（本卡的数据前置缺失）",
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _counts(db_path: Path) -> dict[str, int]:
    """教材表按学科的词条数（book → unit → word）。"""
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT b.subject, COUNT(w.id) FROM book b "
            "JOIN unit u ON u.book_id = b.id JOIN word w ON w.unit_id = u.id "
            "GROUP BY b.subject"
        ).fetchall()
    finally:
        con.close()
    return {subject: int(n) for subject, n in rows}


# --------------------------------------------------------------------------- AC1


def test_import_real_wordbank_twice_row_counts_unchanged(tmp_path):
    """同一目录连跑两次：第二次每本都「新增 0、跳过 N」，教材表条数不变。"""
    db = tmp_path / "wordbank.db"
    argv = ["--dir", str(DATA_DIR), "--db", f"sqlite:///{db}"]

    assert wordbank_import.main(argv) == 0
    first = _counts(db)
    # 不写死条数：期望值直接从真实文件算，加一本新词表不该把这条测试顶红
    expected: dict[str, int] = {}
    for path in sorted(DATA_DIR.glob("*.json")):
        data = _load(path)
        expected[data["subject"]] = expected.get(data["subject"], 0) + sum(
            len(u["words"]) for u in data["units"]
        )
    assert first == expected  # 每本真实文件核对过的条数，一条不丢

    assert wordbank_import.main(argv) == 0
    assert _counts(db) == first  # 幂等：重导不产生重复行


def test_import_second_run_reports_zero_created(tmp_path, capsys):
    """第二次导入的输出必须逐本写明「新增 0 条」（幂等判据的可见证据）。"""
    db = tmp_path / "wordbank.db"
    argv = ["--dir", str(DATA_DIR), "--db", f"sqlite:///{db}"]
    wordbank_import.main(argv)
    wordbank_import.main(argv)
    out = capsys.readouterr().out
    assert "新增 0 条" in out
    assert out.count("新增 0 条") == len(list(DATA_DIR.glob("*.json")))  # 每份文件各自幂等


# --------------------------------------------------------------------------- AC2


@pytest.mark.parametrize("path", [CHINESE_JSON, MATH_JSON], ids=["语文", "数学"])
def test_real_file_units_and_words_land_intact(path, session):
    """语文/数学各 ≥6 个单元、每单元 ≥1 条；DB 条数与 JSON 条数一致（不改不丢）。"""
    data = _load(path)
    assert len(data["units"]) >= 6
    assert all(len(u["words"]) >= 1 for u in data["units"])

    result = book_service.import_book(session, data)
    book = session.get(Book, result.book_id)
    assert book.subject == data["subject"]
    units = list(book.units)
    assert len(units) == len(data["units"])
    total_in_json = sum(len(u["words"]) for u in data["units"])
    assert result.created == total_in_json
    assert result.skipped == 0


# --------------------------------------------------------------------------- AC3


OLD_SCHEMA = """
CREATE TABLE learner (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(64) NOT NULL,
    grade_level VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE question (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    learner_id INTEGER NOT NULL REFERENCES learner(id),
    subject VARCHAR(32) NOT NULL,
    grade_level VARCHAR(16) NOT NULL,
    question_type VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    answer TEXT,
    analysis TEXT,
    source VARCHAR(64) NOT NULL,
    lang VARCHAR(8),
    created_at DATETIME NOT NULL
);
CREATE TABLE error_item (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    learner_id INTEGER NOT NULL REFERENCES learner(id),
    question_id INTEGER NOT NULL REFERENCES question(id),
    wrong_reason VARCHAR(64),
    wrong_count INTEGER NOT NULL,
    last_wrong_at DATETIME NOT NULL,
    review_status VARCHAR(16) NOT NULL,
    confidence FLOAT,
    stability FLOAT,
    difficulty FLOAT,
    next_due_at DATETIME,
    last_reviewed_at DATETIME,
    restored_at DATETIME,
    created_at DATETIME NOT NULL
);
"""


def _make_old_db(db_path: Path) -> None:
    """没有教材表的老库：一条 learner、一条 question（个人题库）。"""
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(OLD_SCHEMA)
        con.execute(
            "INSERT INTO learner (id, name, grade_level, created_at) "
            "VALUES (1, '小明', 'primary-3', '2026-10-01 08:00:00')"
        )
        con.execute(
            "INSERT INTO question (id, learner_id, subject, grade_level, question_type, "
            "content, answer, source, created_at) VALUES "
            "(1, 1, '数学', 'primary-3', '计算', '三加五等于多少？', '八', 'manual', "
            "'2026-10-01 08:01:00')"
        )
        con.commit()
    finally:
        con.close()


def test_old_db_upgrade_keeps_private_library_and_adds_book_tables(tmp_path):
    """老库（无教材表）→ init_db：教材表出现，个人题库条数一字不动。"""
    db = tmp_path / "old.db"
    _make_old_db(db)

    engine = make_engine(f"sqlite:///{db}")
    try:
        init_db(engine)
    finally:
        engine.dispose()

    con = sqlite3.connect(str(db))
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"book", "unit", "word"} <= tables
        assert con.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 1
        assert con.execute(
            "SELECT content, answer FROM question WHERE id = 1"
        ).fetchone() == ("三加五等于多少？", "八")
        assert con.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0] == (
            SCHEMA_VERSION
        )
    finally:
        con.close()


def test_v9_db_gains_imported_at_without_losing_books(tmp_path):
    """v9 库（有 book/question 表、无 imported_at/variant_of）→ 升级：
    book.imported_at 与 question.variant_of 都出现，原书行、原题行不丢。"""
    db = tmp_path / "v9.db"
    con = sqlite3.connect(str(db))
    try:
        con.executescript(
            """
            CREATE TABLE book (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                subject VARCHAR(32) NOT NULL,
                publisher VARCHAR(64) NOT NULL,
                grade INTEGER NOT NULL,
                semester VARCHAR(8) NOT NULL,
                name VARCHAR(128) NOT NULL,
                source_note TEXT
            );
            INSERT INTO book (subject, publisher, grade, semester, name, source_note)
            VALUES ('语文', '人民教育出版社', 5, '上', '人教版 五年级上册 语文', '老库存量书');
            CREATE TABLE question (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                subject VARCHAR(32) NOT NULL,
                content TEXT NOT NULL,
                answer TEXT,
                created_at DATETIME NOT NULL
            );
            INSERT INTO question (subject, content, answer, created_at)
            VALUES ('数学', '三加五等于多少？', '八', '2026-10-01 08:01:00');
            CREATE TABLE schema_version (
                id INTEGER NOT NULL PRIMARY KEY,
                version INTEGER NOT NULL,
                applied_at DATETIME
            );
            INSERT INTO schema_version (id, version) VALUES (1, 9);
            """
        )
        con.commit()
    finally:
        con.close()

    from cairn.migrations import apply_migrations

    engine = make_engine(f"sqlite:///{db}")
    try:
        assert apply_migrations(engine) == SCHEMA_VERSION
    finally:
        engine.dispose()

    con = sqlite3.connect(str(db))
    try:
        book_cols = {row[1] for row in con.execute("PRAGMA table_info(book)")}
        assert "imported_at" in book_cols
        question_cols = {row[1] for row in con.execute("PRAGMA table_info(question)")}
        assert "variant_of" in question_cols
        subject, source_note = con.execute(
            "SELECT subject, source_note FROM book WHERE id = 1"
        ).fetchone()
        assert (subject, source_note) == ("语文", "老库存量书")
        content, answer = con.execute(
            "SELECT content, answer FROM question WHERE id = 1"
        ).fetchone()
        assert (content, answer) == ("三加五等于多少？", "八")
    finally:
        con.close()


# --------------------------------------------------------------------------- AC4


def test_books_page_lists_real_entries(tmp_path):
    """/books 按学科选到语文书 → 页面出现真实词条「欣」；左栏有三科书名。"""
    db = tmp_path / "page.db"
    db_url = f"sqlite:///{db}"
    assert wordbank_import.main(["--dir", str(DATA_DIR), "--db", db_url]) == 0

    engine = make_engine(db_url)
    factory = make_session_factory(engine)
    with factory() as s:
        chinese_book_id = s.query(Book.id).filter(Book.subject == "语文").scalar()
    engine.dispose()
    assert chinese_book_id is not None

    app = create_app(db_url)
    with TestClient(app) as client:
        resp = client.get(f"/books?book_id={chinese_book_id}")
        assert resp.status_code == 200
        assert "欣" in resp.text  # 语文第一单元第一个生字，真实词条
        assert "数学" in resp.text  # 左栏能选到数学书


# --------------------------------------------------------------------------- 导入时间


def test_import_records_imported_at_once(tmp_path, session):
    """导入时间只在第一次落库；重导不改写（幂等不止条数，时间戳也不动）。"""
    data = _load(CHINESE_JSON)
    first = book_service.import_book(session, data)
    book = session.get(Book, first.book_id)
    assert book.imported_at is not None
    stamp = book.imported_at

    second = book_service.import_book(session, data)
    assert second.created == 0
    session.refresh(book)
    assert book.imported_at == stamp
