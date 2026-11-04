"""0.2 卡13 —— 导入备份：空库回环、非空拒绝、版本/字段校验、原子回滚、页面与命令行。

数据一律中文：ASCII-only 的用例抓不到 utf-8 编解码错，也看不出中文是否被转义。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from cairn import import_service, practice_service
from cairn.export_service import build_export
from cairn.import_service import BackupError, load_backup
from cairn.migrations import SCHEMA_VERSION
from cairn.models import (
    Concept,
    ConceptAlias,
    ErrorItem,
    KnowledgePoint,
    Learner,
    Question,
    Review,
    init_db,
    make_session_factory,
    make_engine,
)
from cairn.models.concept import question_concept
from cairn.models.question import question_knowledge_link
from cairn.web import create_app

# 导出文档里的业务表（顺序即外键依赖顺序）。
EXPORTED = (
    "learner",
    "question",
    "knowledge_point",
    "question_knowledge_link",
    "concepts",
    "concept_aliases",
    "question_concept",
    "error_item",
    "review",
)

CHINESE_WORD = "苹果"
CHINESE_ANALYSIS = "水果，蔷薇科"
CHINESE_MATH = "三加五等于多少？"
CHINESE_LEARNER = "小明"


@pytest.fixture()
def open_session():
    """每调一次给一个独立的空库会话；用完统一关掉。"""
    engines, sessions = [], []

    def factory():
        engine = make_engine("sqlite://")
        init_db(engine)
        engines.append(engine)
        session = make_session_factory(engine)()
        sessions.append(session)
        return session

    yield factory
    for session in sessions:
        session.close()
    for engine in engines:
        engine.dispose()


def _count(session, table) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)


def _seed(session) -> None:
    """一份中文种子：学习者 + 两套知识点 + 两道题 + 错题 + 一条复习。"""
    session.add(Learner(id=1, name=CHINESE_LEARNER, grade_level="primary-3"))
    session.add(
        Question(
            id=1,
            learner_id=1,
            subject="语文",
            grade_level="primary-3",
            question_type="听写",
            content=CHINESE_WORD,
            answer=CHINESE_WORD,
            analysis=CHINESE_ANALYSIS,
            audio_text="píngguǒ",
            lang="zh",
            source="manual",
        )
    )
    session.add(
        Question(
            id=2,
            learner_id=1,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content=CHINESE_MATH,
            answer="八",
            analysis="把 3 和 5 合起来数",
            lang="zh",
            source="manual",
        )
    )
    session.add(KnowledgePoint(id=1, name="进位加法", subject="数学"))
    session.add(Concept(id=1, name="进位加法", subject="数学", status="active"))
    session.add(ConceptAlias(id=1, concept_id=1, alias="进位加"))
    session.flush()  # error_item 与 learner/question 没有 relationship，手工保证落库顺序
    session.add(
        ErrorItem(
            id=1,
            learner_id=1,
            question_id=1,
            review_status="new",
            wrong_count=2,
        )
    )
    session.flush()
    session.add(
        Review(
            id=1,
            error_item_id=1,
            rating="good",
            reviewed_at=datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc),
        )
    )
    session.flush()
    session.execute(question_knowledge_link.insert().values(question_id=1, knowledge_point_id=1))
    session.execute(question_concept.insert().values(question_id=2, concept_id=1))
    session.commit()


# ---------------------------------------------------------------- 服务层：回环


def test_roundtrip_into_empty_db(open_session):
    """AC1 + AC2：条数逐表相等、中文逐字相等、关联还在、id 原样。"""
    source = open_session()
    _seed(source)
    document = build_export(source, 1, now=datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc))

    target = open_session()
    report = load_backup(target, document)

    assert report.schema_version == SCHEMA_VERSION
    assert report.exported_at == "2026-10-31T09:00:00+00:00"
    for key in EXPORTED:
        assert report.counts[key] == len(document[key]), key
    for key in EXPORTED:
        assert _count(target, import_service._TABLE_BY_KEY[key]) == len(document[key]), key

    # 中文没乱码、没被转义成 \uXXXX
    assert target.get(Question, 1).content == CHINESE_WORD
    assert target.get(Question, 1).analysis == CHINESE_ANALYSIS
    assert target.get(Question, 2).content == CHINESE_MATH
    assert target.get(Learner, 1).name == CHINESE_LEARNER
    assert target.get(Concept, 1).name == "进位加法"

    # id 原样 + 关联表还连得上
    assert [row.id for row in target.scalars(select(Question).order_by(Question.id))] == [1, 2]
    assert [row.id for row in target.scalars(select(Review))] == [1]
    assert target.execute(select(question_knowledge_link)).all() == [(1, 1)]
    assert target.execute(select(question_concept)).all() == [(2, 1)]


def test_import_then_practice(open_session):
    """AC7：导入完就能取到题，题面是备份里的中文原文。"""
    source = open_session()
    _seed(source)
    document = build_export(source, 1)

    target = open_session()
    load_backup(target, document)

    items = practice_service.fetch_practice(
        target, 1, datetime.now(timezone.utc), limit=5
    )

    assert [item.error_item_id for item in items] == [1]
    assert items[0].content == CHINESE_WORD
    assert items[0].subject == "语文"


# ---------------------------------------------------------------- 服务层：拒绝


def test_refuses_non_empty_target(open_session):
    """AC3：目标库有 1 道题 → 拒绝，文案点名条数，一行都不写。"""
    source = open_session()
    _seed(source)
    document = build_export(source, 1)

    target = open_session()
    owner = Learner(name="占位孩子")
    target.add(owner)
    target.flush()
    target.add(
        Question(
            learner_id=owner.id,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content="一加一等于几？",
            answer="二",
        )
    )
    target.commit()

    with pytest.raises(BackupError) as exc:
        load_backup(target, document)

    assert "当前库里有 1 道题" in str(exc.value)
    assert "请先备份或换一个空库" in str(exc.value)
    assert _count(target, Question.__table__) == 1
    assert _count(target, Learner.__table__) == 1
    assert _count(target, Review.__table__) == 0


def test_refuses_non_empty_without_questions(open_session):
    """没有题但有复习/知识点，也算非空（不做合并）。"""
    source = open_session()
    _seed(source)
    document = build_export(source, 1)

    target = open_session()
    target.add(KnowledgePoint(id=9, name="分数", subject="数学"))
    target.commit()

    with pytest.raises(BackupError) as exc:
        load_backup(target, document)

    assert "当前库不是空库" in str(exc.value)
    assert "知识点 1 条" in str(exc.value)


def test_refuses_newer_schema(open_session):
    """AC4：schema_version 高于本程序 → 拒绝并说明两边版本。"""
    source = open_session()
    _seed(source)
    document = build_export(source, 1)
    document["schema_version"] = SCHEMA_VERSION + 1

    target = open_session()
    with pytest.raises(BackupError) as exc:
        load_backup(target, document)

    message = str(exc.value)
    assert f"schema_version={SCHEMA_VERSION + 1}" in message
    assert f"只支持到 {SCHEMA_VERSION}" in message
    for key in EXPORTED:
        assert _count(target, import_service._TABLE_BY_KEY[key]) == 0, key


def test_missing_required_field_imports_nothing(open_session):
    """AC5：question 缺 content → 整批拒绝，连 learner 都没写进去。"""
    source = open_session()
    _seed(source)
    document = build_export(source, 1)
    del document["question"][0]["content"]

    target = open_session()
    with pytest.raises(BackupError) as exc:
        load_backup(target, document)

    message = str(exc.value)
    assert "题目" in message and "content" in message and "第 1 条" in message
    for key in EXPORTED:
        assert _count(target, import_service._TABLE_BY_KEY[key]) == 0, key


def test_rolls_back_on_midway_failure(open_session):
    """AC6：review 指向不存在的 error_item → 外键失败 → 全部回滚（不是导一半）。"""
    source = open_session()
    _seed(source)
    document = build_export(source, 1)
    document["review"].append(
        {
            "id": 2,
            "error_item_id": 999,
            "rating": "good",
            "reviewed_at": "2026-10-31T08:00:00+00:00",
        }
    )

    target = open_session()
    with pytest.raises(BackupError) as exc:
        load_backup(target, document)

    assert "复习记录 第 2 条" in str(exc.value)
    assert "整批已回滚" in str(exc.value)
    for key in EXPORTED:
        assert _count(target, import_service._TABLE_BY_KEY[key]) == 0, key


def test_backup_without_learner_is_refused(open_session):
    source = open_session()
    _seed(source)
    document = build_export(source, 1)
    document["learner"] = []

    target = open_session()
    with pytest.raises(BackupError) as exc:
        load_backup(target, document)

    assert "没有学习者" in str(exc.value)


# ---------------------------------------------------------------- 占位 learner


def test_placeholder_learner_is_replaced(open_session):
    """AC8：web 启动建的 default learner 让位给备份里那条。"""
    source = open_session()
    _seed(source)
    document = build_export(source, 1)

    target = open_session()
    target.add(Learner(id=1, name="default"))
    target.commit()

    report = load_backup(target, document)

    assert report.counts["learner"] == 1
    assert target.get(Learner, 1).name == CHINESE_LEARNER
    assert _count(target, Learner.__table__) == 1


def test_placeholder_learner_not_replaceable(open_session):
    source = open_session()
    _seed(source)
    document = build_export(source, 1)

    target = open_session()
    target.add(Learner(id=1, name="default"))
    target.commit()

    with pytest.raises(BackupError) as exc:
        load_backup(target, document, replace_empty_ok=False)

    assert "不允许替换" in str(exc.value)
    assert target.get(Learner, 1).name == "default"
    assert _count(target, Question.__table__) == 0


def test_default_books_do_not_block_import(open_session):
    """教材表不在备份里、也不算「非空」：导入空库时那两本空书原样留着。"""
    from cairn.book_service import ensure_default_books
    from cairn.models import Book

    source = open_session()
    _seed(source)
    document = build_export(source, 1)

    target = open_session()
    ensure_default_books(target)
    books_before = _count(target, Book.__table__)
    assert books_before == 2

    load_backup(target, document)

    assert _count(target, Book.__table__) == books_before


# ---------------------------------------------------------------- 契约


def test_export_shape_includes_recall(open_session):
    """Export includes recall attempts after their review dependencies."""
    source = open_session()
    _seed(source)

    document = build_export(source, 1)

    assert list(document) == [
        "schema_version",
        "exported_at",
        "learner",
        "question",
        "knowledge_point",
        "question_knowledge_link",
        "concepts",
        "concept_aliases",
        "question_concept",
        "error_item",
        "review",
        "recap_attempt",
        "learning_collection",
        "learning_collection_item",
        "learning_collection_version",
        "learning_collection_version_question",
        "learning_collection_practice_item",
    ]


def test_variant_backup_restores_forward_reference_and_rejects_missing_original(open_session):
    source = open_session()
    source.add(Learner(id=1, name="Learner"))
    source.add_all([
        Question(id=1, learner_id=1, subject="数学", grade_level="primary", question_type="计算", content="Variant"),
        Question(id=2, learner_id=1, subject="数学", grade_level="primary", question_type="计算", content="Original"),
    ])
    source.commit()
    source.get(Question, 1).variant_of = 2
    source.commit()
    document = build_export(source, 1)
    target = open_session()
    load_backup(target, document)
    assert target.get(Question, 1).variant_of == 2

    document["question"][0]["variant_of"] = 999
    empty = open_session()
    with pytest.raises(BackupError, match="变式题"):
        load_backup(empty, document)
    assert empty.scalar(select(Question.id)) is None


# ---------------------------------------------------------------- 页面


ENTRY_FORM = {
    "subject": "语文",
    "question_type": "听写",
    "content": CHINESE_WORD,
    "answer": CHINESE_WORD,
    "analysis": CHINESE_ANALYSIS,
}


def _seed_one_round(client) -> None:
    assert client.post("/entry", data=ENTRY_FORM).status_code == 200
    assert client.post(
        "/practice/reveal", data={"item_id": "1", "learner_answer": CHINESE_WORD}
    ).status_code == 200
    assert client.post("/practice/grade", data={"item_id": "1", "rating": "good"}).status_code == 200


def _table_counts(db_path: str) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    try:
        return {
            table: con.execute(f"select count(*) from {table}").fetchone()[0]
            for table in EXPORTED
        }
    finally:
        con.close()


def test_import_page(tmp_path):
    """AC9：上传框 + 风险说明。"""
    with TestClient(create_app(f"sqlite:///{tmp_path}/page.db")) as client:
        resp = client.get("/import")

    assert resp.status_code == 200
    assert 'enctype="multipart/form-data"' in resp.text
    assert 'accept=".json,application/json"' in resp.text
    assert "目标库必须是空库" in resp.text


def test_export_then_import_via_web(tmp_path):
    """AC1 的真实链路：真 app 导出 → 另一个空库 app 上传 → 各表条数一致 → 能练习。"""
    source_db = f"{tmp_path}/source.db"
    with TestClient(create_app(f"sqlite:///{source_db}")) as client:
        _seed_one_round(client)
        exported = client.get("/export").content
    source_counts = _table_counts(source_db)

    target_db = f"{tmp_path}/target.db"
    with TestClient(create_app(f"sqlite:///{target_db}")) as client:
        resp = client.post(
            "/import",
            files={"backup": ("cairn-export-20261031-0900.json", exported, "application/json")},
        )
        assert resp.status_code == 200
        assert "导入结果" in resp.text
        assert "导入完成" in resp.text
        assert "题目" in resp.text
        practice = client.get("/practice?item_id=1")
        assert practice.status_code == 200
        assert CHINESE_WORD in practice.text

    target_counts = _table_counts(target_db)
    for table in EXPORTED:
        assert target_counts[table] == source_counts[table], table
    assert target_counts["question"] == 1


def test_import_via_web_refuses_non_empty(tmp_path):
    """AC9：非空库在页面上给人话，且库没变。"""
    with TestClient(create_app(f"sqlite:///{tmp_path}/busy.db")) as client:
        _seed_one_round(client)
        exported = client.get("/export").content

        resp = client.post(
            "/import",
            files={"backup": ("backup.json", exported, "application/json")},
        )

        assert resp.status_code == 200
        assert "当前库里有 1 道题" in resp.text
        assert "请先备份或换一个空库" in resp.text

    assert _table_counts(f"{tmp_path}/busy.db")["question"] == 1


def test_import_via_web_rejects_bad_json(tmp_path):
    with TestClient(create_app(f"sqlite:///{tmp_path}/bad.db")) as client:
        resp = client.post(
            "/import",
            files={"backup": ("backup.json", "这不是 JSON".encode("utf-8"), "application/json")},
        )
        assert resp.status_code == 200
        assert "不是有效的 JSON" in resp.text

        resp = client.post("/import", data={})
        assert resp.status_code == 200
        assert "请选择一个 .json 备份文件" in resp.text


# ---------------------------------------------------------------- 命令行


def test_cli_import(open_session, tmp_path, capsys):
    """AC10：命令行在空库上成功、同一份再导一次被拒（NAS 上恢复用）。"""
    source = open_session()
    _seed(source)
    path = tmp_path / "backup.json"
    path.write_text(
        json.dumps(build_export(source, 1), ensure_ascii=False), encoding="utf-8"
    )
    db_url = f"sqlite:///{tmp_path}/cli.db"

    assert import_service.main([str(path), "--db-url", db_url]) == 0
    out = capsys.readouterr().out
    assert "导入完成" in out
    assert "题目 2 条" in out
    assert "学习者 1 条" in out

    assert import_service.main([str(path), "--db-url", db_url]) == 1
    err = capsys.readouterr().err
    assert "当前库里有 2 道题" in err


def test_cli_reports_missing_file(tmp_path, capsys):
    assert import_service.main([str(tmp_path / "nope.json")]) == 1
    assert "找不到备份文件" in capsys.readouterr().err
