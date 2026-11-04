"""0.2 卡5 — GET /export/anki：.apkg 下载与包内结构（不依赖真 Anki）。

做法就是要求里那条链路：生成文件 → zipfile 打开 → 解出 collection.anki2 →
sqlite3 打开 → 断言五张表 / 行数 / JSON 载荷 / 中文原样 / tag。

不装 Anki、不装 `anki` 库（AGPL）：只证明「包的结构与 Anki legacy schema 一致」，
真机导入由 9 哥验收（见 HANDOFF）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn.models import Concept, Learner, Question, make_engine, make_session_factory
from cairn.models.concept import question_concept
from cairn.web import create_app

FILENAME_RE = re.compile(r'attachment; filename="cairn-anki-\d{8}\.apkg"')

MATH_CONTENT = "三加五等于多少？"
MATH_ANSWER = "八"
ENGLISH_CONTENT = "苹果"
ENGLISH_ANSWER = "apple"
CHINESE_CONTENT = "chūn tiān"
CHINESE_ANSWER = "春天"

#: seeded question content -> (subject, answer)
QUESTIONS = {
    MATH_CONTENT: ("数学", MATH_ANSWER),
    ENGLISH_CONTENT: ("英语", ENGLISH_ANSWER),
    CHINESE_CONTENT: ("语文", CHINESE_ANSWER),
}


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/anki.db")
    with TestClient(app) as c:
        yield c, str(tmp_path / "anki.db")


def _owner_id(session) -> int:
    """题目必须有主人；web 启动时已经建了 default learner，就用那一个。"""
    owner = session.scalars(select(Learner).order_by(Learner.id)).first()
    if owner is None:
        owner = Learner(name="test-child")
        session.add(owner)
        session.commit()
    return owner.id


def _seed(db_path: str) -> None:
    """三科各一题；数学题挂两个知识点，英语题的知识点名带空格与特殊字符。"""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            owner_id = _owner_id(session)
            math = Question(
                learner_id=owner_id,
                subject="数学",
                grade_level="primary-3",
                question_type="计算",
                content=MATH_CONTENT,
                answer=MATH_ANSWER,
            )
            english = Question(
                learner_id=owner_id,
                subject="英语",
                grade_level="primary-3",
                question_type="单词",
                content=ENGLISH_CONTENT,
                answer=ENGLISH_ANSWER,
            )
            chinese = Question(
                learner_id=owner_id,
                subject="语文",
                grade_level="primary-3",
                question_type="词语",
                content=CHINESE_CONTENT,
                answer=CHINESE_ANSWER,
            )
            session.add_all([math, english, chinese])
            session.flush()

            concepts = [
                Concept(name="进位加法", subject="数学"),
                Concept(name="两位数乘一位数", subject="数学"),
                Concept(name="单词 拼写（水果）", subject="英语"),
            ]
            session.add_all(concepts)
            session.flush()
            session.execute(
                question_concept.insert(),
                [
                    {"question_id": math.id, "concept_id": concepts[0].id},
                    {"question_id": math.id, "concept_id": concepts[1].id},
                    {"question_id": english.id, "concept_id": concepts[2].id},
                ],
            )
            session.commit()
    finally:
        engine.dispose()


def _export(c, tmp_path):
    """GET /export/anki → 落地 .apkg → 解出 collection.anki2，返回 (resp, apkg, db)。"""
    resp = c.get("/export/anki")
    apkg = tmp_path / "cairn.apkg"
    apkg.write_bytes(resp.content)
    with zipfile.ZipFile(apkg) as package:
        (tmp_path / "collection.anki2").write_bytes(package.read("collection.anki2"))
    return resp, apkg, tmp_path / "collection.anki2"


def _deck_names(decks_json: str) -> dict[int, str]:
    return {int(deck["id"]): deck["name"] for deck in json.loads(decks_json).values()}


# ---- AC1 路由 ----

def test_route_returns_apkg_attachment(client, tmp_path):
    c, db = client
    _seed(db)

    resp = c.get("/export/anki")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    disposition = resp.headers["content-disposition"]
    assert FILENAME_RE.fullmatch(disposition), disposition
    assert datetime.now().strftime("%Y%m%d") in disposition
    # 一个 zip 的 magic number：PK\x03\x04
    assert resp.content.startswith(b"PK\x03\x04")


def test_entry_page_has_anki_export_link(client):
    """要求 7：录入页顶部有中文说明的入口链接。"""
    c, _ = client

    page = c.get("/entry")

    assert 'href="/export/anki"' in page.text
    assert "导入 Anki 用" in page.text


# ---- AC2 包结构 ----

def test_package_contains_collection_and_empty_media(client, tmp_path):
    c, db = client
    _seed(db)

    _, apkg, _ = _export(c, tmp_path)

    with zipfile.ZipFile(apkg) as package:
        names = package.namelist()
        media = package.read("media")

    assert "collection.anki2" in names
    assert "media" in names
    assert json.loads(media) == {}  # 没有多媒体，空映射


# ---- AC3 legacy schema ----

def test_collection_has_five_legacy_tables(client, tmp_path):
    c, db = client
    _seed(db)

    _, _, collection = _export(c, tmp_path)

    con = sqlite3.connect(collection)
    try:
        tables = {
            row[0]
            for row in con.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        version = con.execute("select ver from col").fetchone()[0]
    finally:
        con.close()

    assert {"col", "notes", "cards", "revlog", "graves"} <= tables
    assert version == 11


# ---- AC4 行数与 new 卡 ----

def test_notes_and_cards_match_questions(client, tmp_path):
    c, db = client
    _seed(db)

    _, _, collection = _export(c, tmp_path)

    con = sqlite3.connect(collection)
    try:
        notes = con.execute("select count(*) from notes").fetchone()[0]
        cards = con.execute("select count(*) from cards").fetchone()[0]
        revlog_rows = con.execute("select count(*) from revlog").fetchone()[0]

        card_rows = con.execute(
            "select type, queue, ivl, factor, reps, lapses, due from cards order by due"
        ).fetchall()
    finally:
        con.close()

    source = sqlite3.connect(db)
    try:
        questions = source.execute("select count(*) from question").fetchone()[0]
    finally:
        source.close()

    assert questions == 3
    assert notes == cards == questions
    assert revlog_rows == 0  # 不导复习历史

    types = {row[0] for row in card_rows}
    queues = {row[1] for row in card_rows}
    assert types == {0}  # 全部 new
    assert queues == {0}
    assert [row[6] for row in card_rows] == [1, 2, 3]  # due 按顺序递增
    for _, _, ivl, factor, reps, lapses, _ in card_rows:
        assert (ivl, factor, reps, lapses) == (0, 0, 0, 0)


# ---- AC5 笔记类型 ----

def test_models_json_has_two_fields_and_template(client, tmp_path):
    c, db = client
    _seed(db)

    _, _, collection = _export(c, tmp_path)

    con = sqlite3.connect(collection)
    try:
        models = json.loads(con.execute("select models from col").fetchone()[0])
        mids = {row[0] for row in con.execute("select distinct mid from notes")}
    finally:
        con.close()

    assert len(models) == 1
    (model_id, model) = next(iter(models.items()))
    assert int(model_id) in mids
    assert [field["name"] for field in model["flds"]] == ["提示", "答案"]
    assert [field["ord"] for field in model["flds"]] == [0, 1]

    template = model["tmpls"][0]
    assert "{{提示}}" in template["qfmt"]
    assert "{{FrontSide}}" in template["afmt"]
    assert "{{答案}}" in template["afmt"]


# ---- AC6 分科牌组 ----

def test_decks_cover_three_subjects_and_cards_are_filed(client, tmp_path):
    c, db = client
    _seed(db)

    _, _, collection = _export(c, tmp_path)

    con = sqlite3.connect(collection)
    try:
        decks = _deck_names(con.execute("select decks from col").fetchone()[0])
        rows = con.execute(
            "select c.did, n.sfld from cards c join notes n on n.id = c.nid"
        ).fetchall()
    finally:
        con.close()

    names = list(decks.values())
    assert {"语文", "英语", "数学"} <= set(names)
    assert len(names) == len(set(names))  # 名字不重复
    assert sorted(decks) == list(range(1, len(decks) + 1))  # id 自增

    for did, sort_field in rows:
        subject = QUESTIONS[sort_field][0]
        assert decks[did] == subject, (sort_field, decks[did])


def test_legacy_subject_codes_map_to_chinese_decks(client, tmp_path):
    """0.1 存量 subject 是 math/english：牌组名仍要是中文，而不是 math。"""
    c, db = client
    engine = make_engine(f"sqlite:///{db}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            session.add(
                Question(
                    learner_id=_owner_id(session),
                    subject="math",
                    grade_level="primary-3",
                    question_type="计算",
                    content="1 + 1 = ?",
                    answer="2",
                )
            )
            session.commit()
    finally:
        engine.dispose()

    _, _, collection = _export(c, tmp_path)

    con = sqlite3.connect(collection)
    try:
        decks = _deck_names(con.execute("select decks from col").fetchone()[0])
    finally:
        con.close()

    assert "数学" in decks.values()
    assert "math" not in decks.values()


# ---- AC7 中文原样 ----

def test_chinese_fields_are_verbatim_utf8(client, tmp_path):
    c, db = client
    _seed(db)

    _, apkg, collection = _export(c, tmp_path)

    con = sqlite3.connect(collection)
    try:
        fields = [row[0] for row in con.execute("select flds from notes order by id")]
        decks_json = con.execute("select decks from col").fetchone()[0]
        models_json = con.execute("select models from col").fetchone()[0]
    finally:
        con.close()

    expected = [
        f"{MATH_CONTENT}\x1f{MATH_ANSWER}",
        f"{ENGLISH_CONTENT}\x1f{ENGLISH_ANSWER}",
        f"{CHINESE_CONTENT}\x1f{CHINESE_ANSWER}",
    ]
    assert fields == expected

    # 原始字节里没有 XML 字符引用，也没有 JSON 的 ASCII 转义
    with zipfile.ZipFile(apkg) as package:
        raw = package.read("collection.anki2")
    assert "&#x".encode() not in raw
    assert b"\\u4e09" not in raw  # 「三」没被转成 \u4e09

    assert "数学" in decks_json
    assert "提示" in models_json


# ---- AC8 知识点 tag ----

def test_tags_come_from_concepts_and_are_sanitized(client, tmp_path):
    c, db = client
    _seed(db)

    _, _, collection = _export(c, tmp_path)

    con = sqlite3.connect(collection)
    try:
        rows = dict(
            con.execute("select sfld, tags from notes").fetchall()
        )
    finally:
        con.close()

    math_tags = rows[MATH_CONTENT]
    assert math_tags.startswith(" ") and math_tags.endswith(" ")
    assert set(math_tags.split()) == {"进位加法", "两位数乘一位数"}

    # 空格与括号被去掉，仍是一个 tag
    assert rows[ENGLISH_CONTENT] == " 单词拼写水果 "
    # 没挂知识点 -> 空 tag 列
    assert rows[CHINESE_CONTENT] == ""


# ---- AC9 空库 ----

def test_empty_database_still_exports_valid_package(client, tmp_path):
    c, _ = client

    _, _, collection = _export(c, tmp_path)

    con = sqlite3.connect(collection)
    try:
        notes = con.execute("select count(*) from notes").fetchone()[0]
        cards = con.execute("select count(*) from cards").fetchone()[0]
        decks = json.loads(con.execute("select decks from col").fetchone()[0])
        models = json.loads(con.execute("select models from col").fetchone()[0])
    finally:
        con.close()

    assert (notes, cards) == (0, 0)
    assert [deck["name"] for deck in decks.values()] == ["Default"]
    assert len(models) == 1
