"""0.2 卡1 AC6–AC8 — 最小迁移机制：版本表、顺序执行、老库升级不丢数据。

测试里的「老库」是手写的 0.1 期 DDL（没有 audio_text / lang），不是从当前模型
反射出来的——否则永远测不出「加列等于没加」。
"""

from __future__ import annotations

import sqlite3

from cairn.migrations import LAST_RUN_REPORT, MIGRATIONS, SCHEMA_VERSION
from cairn.models import init_db, make_engine

OLD_SCHEMA = """
CREATE TABLE learner (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(64) NOT NULL,
    grade_level VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE question (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    subject VARCHAR(32) NOT NULL,
    grade_level VARCHAR(16) NOT NULL,
    question_type VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    answer TEXT,
    analysis TEXT,
    source VARCHAR(64) NOT NULL,
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
CREATE TABLE review (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    error_item_id INTEGER NOT NULL REFERENCES error_item(id),
    rating VARCHAR(8) NOT NULL,
    reviewed_at DATETIME NOT NULL,
    review_duration_ms INTEGER,
    stability_after FLOAT,
    difficulty_after FLOAT,
    next_due_at DATETIME
);
"""


def _connect(db_path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path))


def _make_old_db(db_path) -> None:
    """0.1 期的库：缺 audio_text / lang，但是有真实数据（中文）。"""
    con = _connect(db_path)
    try:
        con.executescript(OLD_SCHEMA)
        con.execute(
            "insert into learner (id, name, grade_level, created_at) "
            "values (1, '小明', 'primary-3', '2026-10-01 08:00:00')"
        )
        con.execute(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, analysis, source, created_at) values "
            "(1, '数学', 'primary-3', '计算', '三加五等于多少？', '八', "
            "'把 3 和 5 合起来数', 'manual', '2026-10-01 08:01:00')"
        )
        con.execute(
            "insert into error_item (id, learner_id, question_id, wrong_count, "
            "last_wrong_at, review_status, confidence, created_at) values "
            "(1, 1, 1, 2, '2026-10-01 08:02:00', 'reviewing', 1.0, '2026-10-01 08:02:00')"
        )
        con.execute(
            "insert into review (id, error_item_id, rating, reviewed_at, next_due_at) "
            "values (1, 1, 'good', '2026-10-02 09:00:00', '2026-10-04 09:00:00')"
        )
        con.commit()
    finally:
        con.close()


def _columns(db_path, table: str) -> set[str]:
    con = _connect(db_path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _version(db_path) -> int:
    con = _connect(db_path)
    try:
        return con.execute("select version from schema_version where id = 1").fetchone()[0]
    finally:
        con.close()


def _scalar(db_path, sql: str):
    con = _connect(db_path)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def test_fresh_db_stamped_latest(tmp_path):
    """全新库：create_all 已按当前模型建表，直接打上最新版本，不跑 ALTER。"""
    db = tmp_path / "fresh.db"
    engine = make_engine(f"sqlite:///{db}")
    try:
        init_db(engine)
    finally:
        engine.dispose()

    assert _version(db) == SCHEMA_VERSION
    assert {"audio_text", "lang"} <= _columns(db, "question")


def test_old_db_upgrades_and_keeps_data(tmp_path):
    """0.1 老库（缺两列、有数据）→ init_db → 新列在、版本对、数据一字不差。"""
    db = tmp_path / "old.db"
    _make_old_db(db)
    assert "audio_text" not in _columns(db, "question")  # 前提：确实缺列

    engine = make_engine(f"sqlite:///{db}")
    try:
        init_db(engine)
    finally:
        engine.dispose()

    assert {"audio_text", "lang"} <= _columns(db, "question")
    assert _version(db) == SCHEMA_VERSION

    con = _connect(db)
    try:
        content, answer, analysis = con.execute(
            "select content, answer, analysis from question where id = 1"
        ).fetchone()
        name, = con.execute("select name from learner where id = 1").fetchone()
        status, = con.execute("select review_status from error_item where id = 1").fetchone()
        ratings, = con.execute("select count(*) from review").fetchone()
    finally:
        con.close()
    assert (content, answer, analysis) == ("三加五等于多少？", "八", "把 3 和 5 合起来数")
    assert name == "小明"
    assert status == "reviewing"
    assert ratings == 1


def test_partial_db_only_applies_missing(tmp_path):
    """落后一版：库里已有 audio_text + 版本记录 1，只该补 lang。"""
    db = tmp_path / "partial.db"
    _make_old_db(db)
    con = _connect(db)
    try:
        con.executescript(
            "CREATE TABLE schema_version (id INTEGER NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL, applied_at DATETIME);"
        )
        con.execute("insert into schema_version (id, version) values (1, 1)")
        con.execute("ALTER TABLE question ADD COLUMN audio_text TEXT")
        con.commit()
    finally:
        con.close()

    engine = make_engine(f"sqlite:///{db}")
    try:
        init_db(engine)
    finally:
        engine.dispose()

    assert "lang" in _columns(db, "question")
    assert _version(db) == SCHEMA_VERSION
    assert _scalar(db, "select count(*) from question") == 1


def test_partial_tables_still_migrated(tmp_path):
    """只缺一部分表的库也算老库：不能被当成全新库直接打版本（否则列永不加）。"""
    db = tmp_path / "partial_tables.db"
    con = _connect(db)
    try:
        con.executescript(
            "CREATE TABLE question (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "subject VARCHAR(32) NOT NULL, grade_level VARCHAR(16) NOT NULL, "
            "question_type VARCHAR(32) NOT NULL, content TEXT NOT NULL, answer TEXT, "
            "analysis TEXT, source VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL);"
        )
        con.execute(
            "insert into question (subject, grade_level, question_type, content, answer, "
            "source, created_at) values ('语文', 'primary-3', '填空', '一( )当先', '马', "
            "'manual', '2026-10-01 08:00:00')"
        )
        con.commit()
    finally:
        con.close()

    engine = make_engine(f"sqlite:///{db}")
    try:
        init_db(engine)
    finally:
        engine.dispose()

    assert {"audio_text", "lang"} <= _columns(db, "question")
    assert _version(db) == SCHEMA_VERSION
    assert _scalar(db, "select content from question where id = 1") == "一( )当先"


def test_init_db_is_idempotent(tmp_path):
    """重复 init_db 不重复执行迁移、不报 duplicate column、数据不动。"""
    db = tmp_path / "twice.db"
    _make_old_db(db)
    for _ in range(2):
        engine = make_engine(f"sqlite:///{db}")
        try:
            init_db(engine)
        finally:
            engine.dispose()

    assert _version(db) == SCHEMA_VERSION
    assert _scalar(db, "select count(*) from schema_version") == 1
    assert _scalar(db, "select count(*) from question") == 1
    assert [m.version for m in MIGRATIONS] == list(range(1, SCHEMA_VERSION + 1))


def test_backfill_sets_lang_for_null_rows(tmp_path):
    """老库 lang 为 NULL：迁移链跑完后不再有 NULL，且最终按 answer 定语种。"""
    db = tmp_path / "backfill.db"
    _make_old_db(db)  # 老 schema（无 lang 列），id=1 是中文题
    con = _connect(db)
    try:
        con.execute(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, analysis, source, created_at) values "
            "(2, '英语', 'primary-3', '单词', '苹果', 'apple', NULL, 'manual', "
            "'2026-10-01 08:05:00')"
        )
        con.commit()
    finally:
        con.close()

    engine = make_engine(f"sqlite:///{db}")
    try:
        init_db(engine)
    finally:
        engine.dispose()

    assert _scalar(db, "select count(*) from question where lang is null") == 0
    assert _scalar(db, "select lang from question where id = 1") == "zh"
    assert _scalar(db, "select lang from question where id = 2") == "en"


def test_v4_rejudges_lang_by_answer(tmp_path):
    """v4 回填：拼音提示 + 汉字答案，旧 lang=en（按 content 误判）→ 按 answer 重判 zh。"""
    db = tmp_path / "v4.db"
    _make_old_db(db)
    con = _connect(db)
    try:
        con.execute("ALTER TABLE question ADD COLUMN audio_text TEXT")
        con.execute("ALTER TABLE question ADD COLUMN lang VARCHAR(8)")
        con.execute(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, analysis, source, created_at, lang) values "
            "(2, '语文', 'primary-3', '词语', 'chūn tiān', '春天', NULL, 'manual', "
            "'2026-10-01 08:05:00', 'en')"
        )
        # 模拟已到 v3 的库：列已在，版本=3，v4 才会执行
        con.executescript(
            "CREATE TABLE schema_version (id INTEGER NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL, applied_at DATETIME);"
        )
        con.execute("insert into schema_version (id, version) values (1, 3)")
        con.commit()
    finally:
        con.close()

    engine = make_engine(f"sqlite:///{db}")
    try:
        init_db(engine)
    finally:
        engine.dispose()

    assert _version(db) == SCHEMA_VERSION
    assert _scalar(db, "select lang from question where id = 2") == "zh"


def _make_old_questions(db_path, rows) -> None:
    """老 schema（无 audio_text / lang）里塞若干 question 行，供 v4 使用。"""
    con = _connect(db_path)
    try:
        con.executescript(OLD_SCHEMA)
        con.executemany(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, analysis, source, created_at) values "
            "(?, ?, 'primary-3', '单词', ?, ?, NULL, 'manual', '2026-10-01 08:00:00')",
            rows,
        )
        con.commit()
    finally:
        con.close()


def _upgrade(db_path) -> None:
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        init_db(engine)
    finally:
        engine.dispose()


def _table_names(db_path) -> set[str]:
    con = _connect(db_path)
    try:
        return {
            row[0]
            for row in con.execute("select name from sqlite_master where type='table'")
        }
    finally:
        con.close()


def _question_row(db_path, row_id):
    con = _connect(db_path)
    try:
        return con.execute(
            "select subject, content, answer, lang from question where id = ?",
            (row_id,),
        ).fetchone()
    finally:
        con.close()


def test_v4_normalizes_old_batch_rows(tmp_path):
    """v4：旧批量行（subject=english/chinese）对调 content↔answer 并改中文名，
    lang 按对调后的 answer 判；单条录入行（subject 中文名）一字不动。"""
    db = tmp_path / "v4_old_batch.db"
    _make_old_questions(
        db,
        [
            (1, "english", "apple", "苹果"),      # 旧批量英文：content=词 answer=提示
            (2, "chinese", "春天", "chūn tiān"),  # 旧批量中文：content=字 answer=拼音
            (3, "语文", "chūn tiān", "春天"),     # 单条录入：新口径，别动
        ],
    )

    _upgrade(db)

    assert _version(db) == SCHEMA_VERSION
    # 1：对调 + subject 改名 + 按单词 apple 判 en
    assert _question_row(db, 1) == ("英语", "苹果", "apple", "en")
    # 2：对调 + subject 改名 + 按汉字 春天 判 zh
    assert _question_row(db, 2) == ("语文", "chūn tiān", "春天", "zh")
    # 3：content/answer/subject 原样，lang 按 answer（春天）判 zh（v3 曾按 content 误判 en）
    assert _question_row(db, 3) == ("语文", "chūn tiān", "春天", "zh")


def test_v4_pinyin_answer_on_manual_row_stays_zh(tmp_path):
    """v4 其余行：answer 是拼音、content 是汉字 → content/answer 不动，lang 判 zh。"""
    db = tmp_path / "v4_pinyin_answer.db"
    _make_old_questions(db, [(1, "语文", "春天", "chūn tiān")])

    _upgrade(db)

    assert _version(db) == SCHEMA_VERSION
    assert _question_row(db, 1) == ("语文", "春天", "chūn tiān", "zh")


# --- v5：知识点三表 + 分科保留率 -------------------------------------------------

V4_KNOWLEDGE_SCHEMA = """
CREATE TABLE knowledge_point (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(128) NOT NULL UNIQUE,
    subject VARCHAR(32) NOT NULL,
    parent_id INTEGER REFERENCES knowledge_point(id)
);
CREATE TABLE question_knowledge_link (
    question_id INTEGER NOT NULL REFERENCES question(id),
    knowledge_point_id INTEGER NOT NULL REFERENCES knowledge_point(id),
    PRIMARY KEY (question_id, knowledge_point_id)
);
"""


def _make_v4_db(db_path, *, with_question_field: bool = False) -> None:
    """0.2 卡3 之后的库：有 audio_text/lang，有知识点两表，版本=4。

    知识点刻意排成「子在前、父在后」，再用一条指向空名字的关联制造无法迁移的
    存量，用来验证两遍建父子 + 跳过条数进报告。
    """
    con = _connect(db_path)
    try:
        con.executescript(OLD_SCHEMA)
        con.executescript(V4_KNOWLEDGE_SCHEMA)
        con.execute("ALTER TABLE question ADD COLUMN audio_text TEXT")
        con.execute("ALTER TABLE question ADD COLUMN lang VARCHAR(8)")
        if with_question_field:
            con.execute("ALTER TABLE question ADD COLUMN knowledge_point TEXT")
        con.execute(
            "insert into learner (id, name, grade_level, created_at) "
            "values (1, '小明', 'primary-3', '2026-10-01 08:00:00')"
        )
        con.execute(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, analysis, source, created_at, lang) values "
            "(1, '数学', 'primary-3', '计算', '23 × 4 = ?', '92', NULL, 'manual', "
            "'2026-10-01 08:01:00', 'zh')"
        )
        con.execute(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, analysis, source, created_at, lang) values "
            "(2, '语文', 'primary-3', '填空', '春天，万物复苏', '春', NULL, 'manual', "
            "'2026-10-01 08:02:00', 'zh')"
        )
        if with_question_field:
            con.execute(
                "update question set knowledge_point = '解方程、一元一次方程解法' where id = 1"
            )
            con.execute("update question set knowledge_point = '   ' where id = 2")
        con.execute(
            "insert into knowledge_point (id, name, subject, parent_id) values "
            "(1, '两位数乘一位数', '数学', 2), (2, '进位', '数学', NULL), "
            "(3, '', '数学', NULL)"
        )
        con.execute(
            "insert into question_knowledge_link (question_id, knowledge_point_id) values "
            "(1, 1), (1, 2), (2, 3)"
        )
        con.execute(
            "insert into error_item (id, learner_id, question_id, wrong_count, "
            "last_wrong_at, review_status, confidence, created_at) values "
            "(1, 1, 1, 1, '2026-10-01 08:03:00', 'new', 1.0, '2026-10-01 08:03:00')"
        )
        con.execute(
            "insert into review (id, error_item_id, rating, reviewed_at) "
            "values (1, 1, 'good', '2026-10-02 09:00:00')"
        )
        con.executescript(
            "CREATE TABLE schema_version (id INTEGER NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL, applied_at DATETIME);"
        )
        con.execute("insert into schema_version (id, version) values (1, 4)")
        con.commit()
    finally:
        con.close()


def _concept_parent(db_path, name: str):
    con = _connect(db_path)
    try:
        return con.execute(
            "select c.id, c.parent_id, p.name from concepts c "
            "left join concepts p on p.id = c.parent_id where c.name = ?",
            (name,),
        ).fetchone()
    finally:
        con.close()


def test_v5_creates_tables_and_backfills_old_knowledge_points(tmp_path):
    """AC7：老库升级到 5 —— 旧表不动、concepts 回填、父子/关联搬过来。"""
    db = tmp_path / "v5.db"
    _make_v4_db(db)

    _upgrade(db)

    assert _version(db) == SCHEMA_VERSION  # 加表后全量升到最新（v6 只加 book/unit/word）
    assert {"concepts", "concept_aliases", "question_concept"} <= _columns_all(db)
    # 旧表一行不少、一列不少（软迁移：不删）
    assert _scalar(db, "select count(*) from knowledge_point") == 3
    assert _scalar(db, "select count(*) from question_knowledge_link") == 3
    # 可解析的两行 → 两个 concept；空名字那行留着并进报告
    assert _scalar(db, "select count(*) from concepts") == 2
    concept_id, parent_id, parent_name = _concept_parent(db, "两位数乘一位数")
    assert parent_id == _scalar(db, "select id from concepts where name = '进位'")
    assert parent_name == "进位"
    # 题目关联搬过来了：一题两个知识点
    assert _scalar(db, "select count(*) from question_concept") == 2
    assert _scalar(db, "select count(*) from question_concept where question_id = 1") == 2

    # learner 分科默认值（AC5：老库升级后也成立）
    assert _scalar(db, "select desired_retention_math from learner where id = 1") == 0.85
    assert _scalar(db, "select desired_retention_other from learner where id = 1") == 0.9

    # 数据不丢
    assert _scalar(db, "select content from question where id = 1") == "23 × 4 = ?"
    assert _scalar(db, "select count(*) from error_item") == 1
    assert _scalar(db, "select count(*) from review") == 1

    # 迁移报告：建了 2 个 concept，跳过 2 条（空知识点 + 指向它的关联）
    report = LAST_RUN_REPORT["v5"]
    assert report["concepts_created"] == 2
    assert report["knowledge_point_rows"] == 3
    assert report["skipped_count"] == 2
    assert any("knowledge_point#3" in item for item in report["skipped"])
    assert any("question_knowledge_link(2,3)" in item for item in report["skipped"])


def test_v5_parses_legacy_question_knowledge_point_column(tmp_path):
    """AC7：老库若有 question.knowledge_point 文本列 —— 原值不删，可解析的灌进新表。"""
    db = tmp_path / "v5_field.db"
    _make_v4_db(db, with_question_field=True)

    _upgrade(db)

    assert _version(db) == SCHEMA_VERSION  # 加表后全量升到最新（v6 只加 book/unit/word）
    # 原字段一字未改（可追溯）
    assert _scalar(db, "select knowledge_point from question where id = 1") == (
        "解方程、一元一次方程解法"
    )
    assert _scalar(db, "select knowledge_point from question where id = 2") == "   "

    con = _connect(db)
    try:
        names = [
            row[0]
            for row in con.execute(
                "select c.name from question_concept qc "
                "join concepts c on c.id = qc.concept_id "
                "where qc.question_id = 1 and c.subject = '数学' order by c.id"
            )
        ]
    finally:
        con.close()
    # 旧表那两条（两位数乘一位数/进位）+ 文本列解析出的两条，同题共挂 4 个
    assert names == ["两位数乘一位数", "进位", "解方程", "一元一次方程解法"]

    report = LAST_RUN_REPORT["v5"]
    assert report["question_knowledge_point_rows"] == 2
    assert any("question#2.knowledge_point" in item for item in report["skipped"])


def test_v5_is_idempotent(tmp_path):
    """AC7：重复 init_db 不会把知识点翻倍、不会再跑一遍报告。"""
    db = tmp_path / "v5_twice.db"
    _make_v4_db(db)
    _upgrade(db)
    _upgrade(db)

    assert _version(db) == SCHEMA_VERSION  # 加表后全量升到最新（v6 只加 book/unit/word）
    assert _scalar(db, "select count(*) from concepts") == 2
    assert _scalar(db, "select count(*) from question_concept") == 2
    assert _scalar(db, "select count(*) from schema_version") == 1
    assert LAST_RUN_REPORT == {}


def _columns_all(db_path) -> set[str]:
    con = _connect(db_path)
    try:
        return {
            row[0] for row in con.execute("select name from sqlite_master where type='table'")
        }
    finally:
        con.close()


# ---------------------------------------------------------------- v9 归属


# v8 的形状：question / scan 都还没有 learner_id（本卡之前的样子）。
OLD_SCHEMA_V8 = """
CREATE TABLE learner (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(64) NOT NULL,
    grade_level VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE question (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    subject VARCHAR(32) NOT NULL,
    grade_level VARCHAR(16) NOT NULL,
    question_type VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    answer TEXT,
    analysis TEXT,
    audio_text TEXT,
    lang VARCHAR(8),
    source VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE scan (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    device_id VARCHAR(128) NOT NULL,
    device_name VARCHAR(128) NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    captured_at DATETIME NOT NULL,
    path VARCHAR(255) NOT NULL UNIQUE,
    note TEXT
);
"""


def _make_v8_db(db_path) -> None:
    """v8 老库：两个 learner、三道题、两张照片，全都没有归属列。"""
    con = _connect(db_path)
    try:
        con.executescript(OLD_SCHEMA_V8)
        con.executemany(
            "insert into learner (id, name, grade_level, created_at) values (?, ?, ?, ?)",
            [
                (1, "老大", "primary-3", "2026-10-01 08:00:00"),
                (2, "老二", "primary-1", "2026-10-02 08:00:00"),
            ],
        )
        con.executemany(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, analysis, source, created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "数学", "primary-3", "计算", "三加五等于多少？", "八", "数一数", "manual", "2026-10-01 08:01:00"),
                (2, "英语", "primary-3", "单词", "未来", "future", None, "manual", "2026-10-01 08:02:00"),
                (3, "语文", "primary-3", "词语", "苹果", "苹果", None, "manual", "2026-10-01 08:03:00"),
            ],
        )
        con.executemany(
            "insert into scan (id, device_id, device_name, width, height, captured_at, path) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "fake:uvc-pro", "高拍仪", 800, 600, "2026-10-01 09:00:00", "scans/2026/10/a.jpg"),
                (2, "fake:builtin", "内置摄像头", 640, 480, "2026-10-01 09:01:00", "scans/2026/10/b.jpg"),
            ],
        )
        con.commit()
    finally:
        con.close()


def test_v9_owns_existing_questions_and_scans(tmp_path):
    """AC2：v8 老库升级后，题目与照片全部归最小 id 的 learner，条数不变。"""
    db = tmp_path / "v9.db"
    _make_v8_db(db)
    assert "learner_id" not in _columns(db, "question")
    assert "learner_id" not in _columns(db, "scan")

    _upgrade(db)

    assert _version(db) == SCHEMA_VERSION
    assert "learner_id" in _columns(db, "question")
    assert "learner_id" in _columns(db, "scan")

    assert _scalar(db, "select count(*) from question") == 3
    assert _scalar(db, "select count(*) from scan") == 2
    assert _scalar(db, "select count(*) from question where learner_id = 1") == 3
    assert _scalar(db, "select count(*) from scan where learner_id = 1") == 2
    assert _scalar(db, "select count(*) from question where learner_id is null") == 0

    report = LAST_RUN_REPORT["v9"]
    assert report["owner_id"] == 1
    assert report["questions_owned"] == 3
    assert report["scans_owned"] == 2


def test_v9_is_idempotent(tmp_path):
    """AC2：再升一次不重跑、不改数据、报告为空。"""
    db = tmp_path / "v9_twice.db"
    _make_v8_db(db)
    _upgrade(db)
    _upgrade(db)

    assert _version(db) == SCHEMA_VERSION
    assert _scalar(db, "select count(*) from question where learner_id = 1") == 3
    assert _scalar(db, "select count(*) from schema_version") == 1
    assert LAST_RUN_REPORT == {}


def test_v9_creates_owner_when_old_db_has_none(tmp_path):
    """没有 learner 但已有题目的手工库：先补一条占位 learner，再归属，别丢题。"""
    db = tmp_path / "v9_orphan.db"
    con = _connect(db)
    try:
        con.executescript(OLD_SCHEMA_V8)
        con.execute(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, source, created_at) values "
            "(1, '数学', 'primary-3', '计算', '一加一', '二', 'manual', '2026-10-01 08:00:00')"
        )
        con.commit()
    finally:
        con.close()

    _upgrade(db)

    assert _scalar(db, "select count(*) from learner") == 1
    owner = _scalar(db, "select id from learner")
    assert _scalar(db, f"select count(*) from question where learner_id = {owner}") == 1
    assert LAST_RUN_REPORT["v9"]["owner_id"] == owner


# ---------------------------------------------------------------- v11 变式指针


def _make_v10_db(db_path) -> None:
    """v10 形状的库：question 还没有 variant_of 列，版本记录钉在 10。

    表结构用 0.1 的 OLD_SCHEMA 即可——v11 是纯加列，只关心 question 表本身，
    1–10 号迁移因版本号已到 10 而全部跳过。
    """
    con = _connect(db_path)
    try:
        con.executescript(OLD_SCHEMA)
        con.executemany(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, analysis, source, created_at) values (?, ?, 'primary-3', '计算', "
            "?, ?, NULL, 'manual', '2026-10-01 08:00:00')",
            [
                (1, "数学", "三加五等于多少？", "八"),
                (2, "数学", "四加六等于多少？", "十"),
            ],
        )
        con.executescript(
            "CREATE TABLE schema_version (id INTEGER NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL, applied_at DATETIME);"
        )
        con.execute("insert into schema_version (id, version) values (1, 10)")
        con.commit()
    finally:
        con.close()


def test_v11_adds_variant_of_and_keeps_data(tmp_path):
    """0.3 卡2 AC4：v10 老库（无 variant_of）升级 → 列在、数据条数不变、版本到 11。"""
    db = tmp_path / "v11.db"
    _make_v10_db(db)
    assert "variant_of" not in _columns(db, "question")  # 前提：确实缺列

    _upgrade(db)

    assert SCHEMA_VERSION == MIGRATIONS[-1].version  # 链顶跟着迁移链走，不再钉字面量
    assert _version(db) == SCHEMA_VERSION
    assert "variant_of" in _columns(db, "question")
    # 纯加列不动存量行
    assert _scalar(db, "select count(*) from question") == 2
    assert _scalar(db, "select count(*) from question where variant_of is null") == 2
    assert _scalar(db, "select content from question where id = 1") == "三加五等于多少？"


def test_v11_is_idempotent(tmp_path):
    """v11 重复升级不报 duplicate column、数据不动、报告为空。"""
    db = tmp_path / "v11_twice.db"
    _make_v10_db(db)
    _upgrade(db)
    _upgrade(db)

    assert _version(db) == SCHEMA_VERSION
    assert _scalar(db, "select count(*) from question") == 2
    assert _scalar(db, "select count(*) from schema_version") == 1
    assert LAST_RUN_REPORT == {}


def test_partial_v9_db_without_question_still_reaches_latest(tmp_path):
    """v9 库缺 question 表（手搭/半成品库）→ v10/v11 跳过缺表步骤，版本照常推进。

    防御钉子：ALTER 的目标表不存在时记 INFO 跳过而不是抛 NoSuchTableError——
    去掉这层防御，本用例必须变红（NoSuchTableError: question）。
    """
    from cairn.migrations import apply_migrations

    db = tmp_path / "v9_no_question.db"
    con = _connect(db)
    try:
        con.executescript(
            "CREATE TABLE book (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "subject VARCHAR(32) NOT NULL, publisher VARCHAR(64) NOT NULL, "
            "grade INTEGER NOT NULL, semester VARCHAR(8) NOT NULL, "
            "name VARCHAR(128) NOT NULL, source_note TEXT);"
            "CREATE TABLE schema_version (id INTEGER NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL, applied_at DATETIME);"
            "INSERT INTO schema_version (id, version) VALUES (1, 9);"
        )
        con.commit()
    finally:
        con.close()

    engine = make_engine(f"sqlite:///{db}")
    try:
        assert apply_migrations(engine) == SCHEMA_VERSION
    finally:
        engine.dispose()

    assert _version(db) == SCHEMA_VERSION
    assert "imported_at" in _columns(db, "book")  # 存在的表照常加列
    assert _columns(db, "question") == set()  # 缺的表跳过、凭空造不出来


# ---- v12：strategy 目录表 + learner.strategy_id（复习方法策略层卡） ----


def _make_v11_db(db_path) -> None:
    """v11 形状的库：OLD_SCHEMA + variant_of 列，版本钉在 11；strategy 表不存在。

    error_item 带真实排期状态（stability/difficulty/next_due_at/last_reviewed_at），
    用于证明升级后到期时间逐条不变。
    """
    con = _connect(db_path)
    try:
        con.executescript(OLD_SCHEMA)
        con.execute(
            "ALTER TABLE question ADD COLUMN variant_of INTEGER REFERENCES question(id)"
        )
        con.execute(
            "insert into learner (id, name, grade_level, created_at) "
            "values (1, '小明', 'primary-3', '2026-10-01 08:00:00')"
        )
        con.execute(
            "insert into question (id, subject, grade_level, question_type, content, "
            "answer, analysis, source, created_at) values "
            "(1, '数学', 'primary-3', '计算', '三加五等于多少？', '八', "
            "'把 3 和 5 合起来数', 'manual', '2026-10-01 08:01:00')"
        )
        con.execute(
            "insert into error_item (id, learner_id, question_id, wrong_count, "
            "last_wrong_at, review_status, confidence, stability, difficulty, "
            "next_due_at, last_reviewed_at, created_at) values "
            "(1, 1, 1, 2, '2026-10-02 09:00:00', 'reviewing', 1.0, "
            "2.3065, 2.118103970459016, '2026-10-15 09:00:00', "
            "'2026-10-02 09:00:00', '2026-10-01 08:02:00')"
        )
        con.execute(
            "insert into review (id, error_item_id, rating, reviewed_at, "
            "stability_after, difficulty_after, next_due_at) values "
            "(1, 1, 'good', '2026-10-02 09:00:00', 2.3065, 2.118103970459016, "
            "'2026-10-15 09:00:00')"
        )
        con.executescript(
            "CREATE TABLE schema_version (id INTEGER NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL, applied_at DATETIME);"
        )
        con.execute("insert into schema_version (id, version) values (1, 11)")
        con.commit()
    finally:
        con.close()


def test_v12_adds_strategy_tables_and_keeps_schedule(tmp_path):
    """策略层卡 AC4：v11 库升 v12 → 列在且为空、strategy 表有默认行、
    到期时间与数据条数逐条不变、版本到 12；重复升级幂等。"""
    db = tmp_path / "v12.db"
    _make_v11_db(db)
    assert "strategy" not in _table_names(db)  # 前提：strategy 表确实不存在
    assert "strategy_id" not in _columns(db, "learner")

    _upgrade(db)
    _upgrade(db)  # 幂等：第二次不报 duplicate column

    assert SCHEMA_VERSION == MIGRATIONS[-1].version  # 链顶跟着迁移链走，不再钉字面量
    assert _version(db) == SCHEMA_VERSION
    assert "strategy" in _table_names(db)
    assert "strategy_id" in _columns(db, "learner")
    # learner.strategy_id 存在且为空（空 = 默认 fsrs）
    assert _scalar(db, "select count(*) from learner where strategy_id is null") == 1
    # 默认策略种子行
    assert _scalar(db, "select count(*) from strategy") == 1
    assert _scalar(db, "select id from strategy") == "fsrs"
    # 存量数据与到期时间逐条不变
    assert _scalar(db, "select count(*) from learner") == 1
    assert _scalar(db, "select count(*) from question") == 1
    assert _scalar(db, "select count(*) from error_item") == 1
    assert _scalar(db, "select count(*) from review") == 1
    assert (
        _scalar(db, "select next_due_at from error_item where id = 1")
        == "2026-10-15 09:00:00"
    )
    assert (
        _scalar(db, "select next_due_at from review where id = 1") == "2026-10-15 09:00:00"
    )
    assert _scalar(db, "select stability from error_item where id = 1") == 2.3065


def test_v12_tolerates_half_broken_db_without_learner(tmp_path):
    """防御钉子：库只剩 book + schema_version（v11，连 learner 表都没有）→
    v12 照常走完：strategy 表建出来、learner.strategy_id 跳过记 INFO、版本到 12。
    去掉 _add_column_if_missing 的缺表防御，本用例必须变红。"""
    from cairn.migrations import apply_migrations

    db = tmp_path / "v12_half_broken.db"
    con = _connect(db)
    try:
        con.executescript(
            "CREATE TABLE book (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "subject VARCHAR(32) NOT NULL, publisher VARCHAR(64) NOT NULL, "
            "grade INTEGER NOT NULL, semester VARCHAR(8) NOT NULL, "
            "name VARCHAR(128) NOT NULL, source_note TEXT);"
            "CREATE TABLE schema_version (id INTEGER NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL, applied_at DATETIME);"
            "INSERT INTO schema_version (id, version) VALUES (1, 11);"
        )
        con.commit()
    finally:
        con.close()

    engine = make_engine(f"sqlite:///{db}")
    try:
        assert apply_migrations(engine) == SCHEMA_VERSION
    finally:
        engine.dispose()

    assert _version(db) == SCHEMA_VERSION
    assert "strategy" in _table_names(db)
    assert _scalar(db, "select id from strategy") == "fsrs"
    assert _columns(db, "learner") == set()  # 缺的表跳过、凭空造不出来


# ---- v20：cut_feedback 圈选修正数据集 -------------------------------------------


CUT_FEEDBACK_COLUMNS = {
    "id",
    "scan_id",
    "frame_key",
    "question_no",
    "action",
    "source",
    "auto_bbox",
    "human_bbox",
    "iou",
    "selected",
    "image_sha256",
    "image_w",
    "image_h",
    "recipe",
    "params_hash",
    "created_at",
}


def _make_v19_db(db_path) -> None:
    """A v19-shaped DB: current schema minus cut_feedback, version pinned to 19.

    Built by dropping the table a fresh init created, so everything else is the
    real current shape and only v20 has work to do.
    """
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        init_db(engine)
    finally:
        engine.dispose()
    con = _connect(db_path)
    try:
        con.executescript(
            "DROP TABLE cut_feedback;"
            "UPDATE schema_version SET version = 19 WHERE id = 1;"
        )
        con.commit()
    finally:
        con.close()


def _schema_snapshot(db_path) -> list[tuple[str, str, str]]:
    """DDL of the table, its indexes and any autoindex, ordered for comparison."""
    con = _connect(db_path)
    try:
        return con.execute(
            "select type, name, sql from sqlite_master "
            "where tbl_name = 'cut_feedback' order by type, name"
        ).fetchall()
    finally:
        con.close()


def _indexes(db_path, table: str) -> set[str]:
    con = _connect(db_path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA index_list({table})")}
    finally:
        con.close()


def _unique_index_columns(db_path, table: str) -> list[list[str]]:
    con = _connect(db_path)
    try:
        columns: list[list[str]] = []
        for row in con.execute(f"PRAGMA index_list({table})"):
            name, is_unique = row[1], row[2]
            if not is_unique:
                continue
            columns.append(
                [info[2] for info in con.execute(f"PRAGMA index_info({name})")]
            )
        return columns
    finally:
        con.close()


def test_v20_creates_cut_feedback_with_unique_and_indexes(tmp_path):
    """v20 建表：字段照 spec 逐字、`(scan_id, frame_key)` 唯一、三个索引都在。"""
    db = tmp_path / "v20.db"
    _make_v19_db(db)
    assert "cut_feedback" not in _table_names(db)  # 前提：确实没有这张表

    _upgrade(db)

    assert SCHEMA_VERSION == MIGRATIONS[-1].version
    # 这条只问「v19 的库能一路走到链顶」；v20 之后还有 v21，版本号不在这里钉死
    assert _version(db) == SCHEMA_VERSION
    assert "cut_feedback" in _table_names(db)
    assert CUT_FEEDBACK_COLUMNS <= _columns(db, "cut_feedback")
    # 重复提交是覆盖、不是追加：这一条由唯一索引兜住
    assert ["scan_id", "frame_key"] in _unique_index_columns(db, "cut_feedback")
    assert {
        "ix_cut_feedback_scan_id",
        "ix_cut_feedback_created_at",
        "ix_cut_feedback_recipe_params_hash",
    } <= _indexes(db, "cut_feedback")


def test_v20_rerun_leaves_schema_and_rows_unchanged(tmp_path):
    """v20 幂等：把版本退回去再跑一次迁移，表结构一字节不变、已有行不动。"""
    db = tmp_path / "v20_twice.db"
    _make_v19_db(db)
    _upgrade(db)
    first = _schema_snapshot(db)

    con = _connect(db)
    try:
        con.execute(
            "insert into cut_feedback (scan_id, frame_key, action, source, "
            "image_sha256, image_w, image_h, recipe, params_hash, selected, "
            "created_at) values (1, 'a1', 'accept', 'manual', 'sha', 1275, 952, "
            "'scan-entry', 'params-v1', 1, '2026-11-01T00:00:00+00:00')"
        )
        con.execute("update schema_version set version = 19 where id = 1")
        con.commit()
    finally:
        con.close()

    _upgrade(db)

    assert _schema_snapshot(db) == first
    assert _version(db) == SCHEMA_VERSION
    assert _scalar(db, "select count(*) from cut_feedback") == 1
    assert _scalar(db, "select action from cut_feedback") == "accept"


# ---- v21：question.edited_at / edited_by 人工编辑痕迹 ---------------------------


def _question_schema(db_path) -> list[tuple[str, str, str]]:
    """question 表（含索引）的 sqlite_master 行，按固定顺序——两次迁移后逐字比对。"""
    con = _connect(db_path)
    try:
        return con.execute(
            "select type, name, sql from sqlite_master "
            "where tbl_name = 'question' order by type, name"
        ).fetchall()
    finally:
        con.close()


def _question_edit_rows(db_path) -> list[tuple]:
    con = _connect(db_path)
    try:
        return con.execute(
            "select content, answer, analysis, edited_at, edited_by "
            "from question order by id"
        ).fetchall()
    finally:
        con.close()


def test_v21_adds_edit_trace_columns_and_keeps_rows(tmp_path):
    """v21：老库补两列；存量题的 content/answer/analysis 一字未变、两列留空。"""
    db = tmp_path / "v21.db"
    _make_old_db(db)
    assert "edited_at" not in _columns(db, "question")  # 前提：确实没有这两列

    _upgrade(db)

    assert SCHEMA_VERSION == MIGRATIONS[-1].version >= 22
    assert _version(db) == SCHEMA_VERSION
    assert {"edited_at", "edited_by"} <= _columns(db, "question")
    # 纯加列：一行都不许动，新列对存量行是 NULL（= 没被人工改过）
    assert _question_edit_rows(db) == [
        ("三加五等于多少？", "八", "把 3 和 5 合起来数", None, None)
    ]


def test_v21_rerun_leaves_schema_and_rows_unchanged(tmp_path):
    """v21 幂等：版本退回 20 再跑一次，sqlite_master 与存量行完全一致。"""
    db = tmp_path / "v21_twice.db"
    _make_old_db(db)
    _upgrade(db)
    schema, rows = _question_schema(db), _question_edit_rows(db)

    con = _connect(db)
    try:
        con.execute("update schema_version set version = 20 where id = 1")
        con.commit()
    finally:
        con.close()

    _upgrade(db)

    assert _question_schema(db) == schema
    assert _question_edit_rows(db) == rows
    assert _version(db) == SCHEMA_VERSION
