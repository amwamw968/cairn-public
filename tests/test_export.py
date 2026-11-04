"""0.2 卡1 AC3–AC5 — GET /export：JSON 下载、三表全量、中文不乱码。

数据一律中文：ASCII-only 的用例抓不到 ensure_ascii 漏配或 utf-8 解码错。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from cairn.web import create_app

SUBJECT = "数学"
QTYPE = "计算"
CONTENT = "三加五等于多少？"
ANSWER = "八"
ANALYSIS = "把 3 和 5 合起来数"

FILENAME_RE = re.compile(r'attachment; filename="cairn-export-\d{8}-\d{4}\.json"')


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/export.db")
    with TestClient(app) as c:
        yield c, str(tmp_path / "export.db")


def _entry_form(**overrides) -> dict[str, str]:
    data = {
        "subject": SUBJECT,
        "question_type": QTYPE,
        "content": CONTENT,
        "answer": ANSWER,
        "analysis": ANALYSIS,
    }
    data.update(overrides)
    return data


def _count(db_path: str, table: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        con.close()


def _seed_one_round(c) -> None:
    """录入 → 揭示 → 判分：一题、一条 error_item、一条 review。"""
    assert c.post("/entry", data=_entry_form()).status_code == 200
    assert c.post(
        "/practice/reveal", data={"item_id": "1", "learner_answer": ANSWER}
    ).status_code == 200
    assert c.post(
        "/practice/grade", data={"item_id": "1", "rating": "good"}
    ).status_code == 200


def test_export_is_attachment_with_dated_filename(client):
    c, _ = client
    _seed_one_round(c)

    resp = c.get("/export")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    disposition = resp.headers["content-disposition"]
    assert FILENAME_RE.fullmatch(disposition), disposition
    assert datetime.now().strftime("%Y%m%d") in disposition


def test_export_counts_match_db(client):
    c, db = client
    _seed_one_round(c)

    document = json.loads(c.get("/export").content.decode("utf-8"))

    assert isinstance(document["schema_version"], int)
    datetime.fromisoformat(document["exported_at"])  # 可解析的 ISO 时间
    # 判题确定性层：reveal 自动判分写一条 good，家长手工 good 再写一条
    for table, expected in (("question", 1), ("error_item", 1), ("review", 2)):
        assert len(document[table]) == _count(db, table), table
        assert len(document[table]) == expected, table

    question = document["question"][0]
    assert (question["subject"], question["question_type"]) == (SUBJECT, QTYPE)
    assert (question["content"], question["answer"], question["analysis"]) == (
        CONTENT,
        ANSWER,
        ANALYSIS,
    )
    # 三表导出的是整行，不是挑出来的列
    assert "audio_text" in question and "lang" in question
    assert document["error_item"][0]["review_status"] == "reviewing"
    assert document["review"][0]["rating"] == "good"


def test_export_chinese_not_escaped(client):
    c, _ = client
    _seed_one_round(c)

    body = c.get("/export").content.decode("utf-8")

    assert CONTENT in body
    assert ANALYSIS in body
    assert "\\u4e09" not in body  # 中文没被 json.dumps 默认转义


ENTITY_TABLES = ("learner", "question", "knowledge_point", "error_item", "review")
ASSOCIATION_TABLE = "question_knowledge_link"


def _seed_knowledge_link(db_path: str) -> None:
    """连上一条知识点并把题目挂上去（入口流程不建 KP，这里直接落库）。"""
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "insert into knowledge_point (id, name, subject, parent_id) "
            "values (1, '进位加法', '数学', NULL)"
        )
        con.execute(
            "insert into question_knowledge_link (question_id, knowledge_point_id) "
            "values (1, 1)"
        )
        con.commit()
    finally:
        con.close()


def test_export_has_all_entity_tables_and_learner_settings(client):
    """换机不丢数据：全部实体表 + 知识点关联表都在，learner 的配置字段可见。"""
    c, db = client
    _seed_one_round(c)
    _seed_knowledge_link(db)

    document = json.loads(c.get("/export").content.decode("utf-8"))

    for table in ENTITY_TABLES:
        assert table in document, f"缺实体表: {table}"
        assert len(document[table]) == _count(db, table), table
    assert ASSOCIATION_TABLE in document, f"缺关联表: {ASSOCIATION_TABLE}"
    assert len(document[ASSOCIATION_TABLE]) == _count(db, ASSOCIATION_TABLE), ASSOCIATION_TABLE

    # learner 整行导出；其唯一配置字段 grade_level 必须能看到
    learner = document["learner"][0]
    assert {"id", "name", "grade_level", "created_at"} <= set(learner)
    assert learner["grade_level"] == "primary"
    # 关联表内容确已导出，可据此还原 wiring
    assert document["question_knowledge_link"] == [
        {"question_id": 1, "knowledge_point_id": 1}
    ]
    assert document["knowledge_point"][0]["name"] == "进位加法"


CONCEPT_TABLES = ("concepts", "concept_aliases", "question_concept")


def test_export_includes_concept_tables_and_subject_retention(client):
    """0.2 卡4a：知识点三表进备份（含别名/挂题）；learner 分科目标随行。"""
    c, db = client
    _seed_one_round(c)

    con = sqlite3.connect(db)
    try:
        con.execute(
            "insert into concepts (id, name, subject, parent_id, status) "
            "values (1, '进位加法', '数学', NULL, 'active')"
        )
        con.execute(
            "insert into concept_aliases (id, concept_id, alias) values (1, 1, '进位加')"
        )
        con.execute("insert into question_concept (question_id, concept_id) values (1, 1)")
        con.commit()
    finally:
        con.close()

    document = json.loads(c.get("/export").content.decode("utf-8"))

    for table in CONCEPT_TABLES:
        assert table in document, f"缺知识点表: {table}"
        assert len(document[table]) == _count(db, table), table
    assert document["concepts"][0]["name"] == "进位加法"
    assert document["concepts"][0]["status"] == "active"
    assert document["concept_aliases"] == [{"id": 1, "concept_id": 1, "alias": "进位加"}]
    assert document["question_concept"] == [{"question_id": 1, "concept_id": 1}]

    learner = document["learner"][0]
    assert learner["desired_retention_math"] == 0.85
    assert learner["desired_retention_other"] == 0.9
