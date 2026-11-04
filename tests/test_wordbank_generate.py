"""卡：英语听写闭环（一次出一整个单元）—— AC2–AC7。

数据源是仓库里人工核对过的真实英语词表 ``data/wordbank/英语-沪教版2022修订-五上.json``
（8 单元 124 条，Unit 1 17 条），断言全部对着真实文件：题面是中文提示、答案是英文词。
AC1/AC8 的变异自证在 ``docs/EVIDENCE.md``。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn import book_service, entry_service
from cairn.entry_service import EntryPayload
from cairn.models import (
    REVIEW_STATUS_NEW,
    ErrorItem,
    Learner,
    Question,
    Unit,
    Word,
    make_engine,
    make_session_factory,
)
from cairn.web import create_app

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "wordbank"
ENGLISH_JSON = DATA_DIR / "英语-沪教版2022修订-五上.json"

pytestmark = pytest.mark.skipif(
    not ENGLISH_JSON.is_file(), reason="真实英语词表不在 data/wordbank/"
)


# --------------------------------------------------------------------------- 装置


def _english() -> dict:
    return json.loads(ENGLISH_JSON.read_text(encoding="utf-8"))


@pytest.fixture()
def seeded(session, learner):
    """真实英语词表入库，返回 (book_id, unit1_id)。"""
    book_id = book_service.import_book(session, _english()).book_id
    unit_id = session.scalars(
        select(Unit.id).where(Unit.book_id == book_id, Unit.unit_no == 1)
    ).one()
    return book_id, unit_id


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "generate.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as c:
        yield c, str(db)


def _seed_real_book(db_path: str) -> tuple[int, int]:
    """在服务的库里导入同一份真实词表，返回 (book_id, unit1_id)。"""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            book_id = book_service.import_book(session, _english()).book_id
            unit_id = session.scalars(
                select(Unit.id).where(Unit.book_id == book_id, Unit.unit_no == 1)
            ).one()
            return book_id, unit_id
    finally:
        engine.dispose()


def _count(db_path: str, sql: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return int(con.execute(sql).fetchone()[0])
    finally:
        con.close()


def _error_item_id(db_path: str, answer: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT ei.id FROM error_item ei JOIN question q ON q.id = ei.question_id "
            "WHERE q.answer = ?",
            (answer,),
        ).fetchone()
    finally:
        con.close()
    assert row is not None, f"题库里没有 {answer}"
    return int(row[0])


# --------------------------------------------------------------------------- AC2


def test_unit1_becomes_17_dictation_questions(session, learner, seeded):
    """Unit 1 的 17 个词 → 17 条题：中文提示 + 英文答案 + 听写 + en + 朗读留空。"""
    _, unit_id = seeded
    created, skipped = book_service.generate_dictation(session, learner.id, unit_id)
    assert (created, skipped) == (17, 0)

    questions = session.scalars(
        select(Question).where(Question.learner_id == learner.id).order_by(Question.id.asc())
    ).all()
    assert len(questions) == 17
    first = questions[0]
    assert (first.content, first.answer) == ("去慢跑", "go jogging")
    assert first.question_type == "听写"
    assert first.subject == "英语"
    assert first.lang == "en"
    assert first.audio_text is None  # 英文词默认念单词本身，不另存文本

    # 顺序跟词表的 position 走
    words = session.scalars(
        select(Word).where(Word.unit_id == unit_id).order_by(Word.position.asc())
    ).all()
    assert [q.content for q in questions] == [w.cue for w in words]

    items = session.scalars(
        select(ErrorItem).where(ErrorItem.learner_id == learner.id)
    ).all()
    assert len(items) == 17
    assert {item.review_status for item in items} == {REVIEW_STATUS_NEW}


# --------------------------------------------------------------------------- AC3


def test_second_click_creates_nothing(session, learner, seeded):
    """连点两次：第二次新增 0、跳过 17，题库与错题条数都不变。"""
    _, unit_id = seeded
    assert book_service.generate_dictation(session, learner.id, unit_id) == (17, 0)
    questions_before = len(session.scalars(select(Question)).all())
    items_before = len(session.scalars(select(ErrorItem)).all())

    assert book_service.generate_dictation(session, learner.id, unit_id) == (0, 17)
    assert len(session.scalars(select(Question)).all()) == questions_before
    assert len(session.scalars(select(ErrorItem)).all()) == items_before


# --------------------------------------------------------------------------- AC4


def test_manually_entered_word_is_skipped(session, learner, seeded):
    """家长之前手录过「去慢跑 / go jogging」→ 生成时跳过那一条，不重复。"""
    _, unit_id = seeded
    entry_service.create_error_item(
        session,
        learner.id,
        EntryPayload(
            subject="英语",
            question_type="听写",
            content="去慢跑",
            answer="go jogging",
        ),
    )

    created, skipped = book_service.generate_dictation(session, learner.id, unit_id)
    assert (created, skipped) == (16, 1)

    pairs = session.execute(
        select(Question.content, Question.answer).where(
            Question.learner_id == learner.id
        )
    ).all()
    assert [pair for pair in pairs if pair == ("去慢跑", "go jogging")] == [
        ("去慢跑", "go jogging")
    ]
    assert len(pairs) == 17


# --------------------------------------------------------------------------- AC5


def test_only_the_current_learner_is_touched(session, learner, seeded):
    """另一个孩子的题库一条不动；反过来给另一个孩子生成也不碰这个孩子的。"""
    _, unit_id = seeded
    other = Learner(name="另一个孩子", grade_level="primary-3")
    session.add(other)
    session.commit()

    book_service.generate_dictation(session, learner.id, unit_id)
    assert session.scalars(
        select(Question).where(Question.learner_id == other.id)
    ).all() == []
    assert session.scalars(
        select(ErrorItem).where(ErrorItem.learner_id == other.id)
    ).all() == []

    before = len(
        session.scalars(select(Question).where(Question.learner_id == learner.id)).all()
    )
    assert book_service.generate_dictation(session, other.id, unit_id) == (17, 0)
    assert (
        len(session.scalars(select(Question).where(Question.learner_id == learner.id)).all())
        == before
    )


# --------------------------------------------------------------------------- AC7


def test_books_page_has_button_and_shows_result(client):
    """按钮 + 摘要：本单元 17 个词，新增 17 条，跳过 0 条；第二次全跳过。"""
    c, db = client
    book_id, unit_id = _seed_real_book(db)

    page = c.get(f"/books?book_id={book_id}")
    assert page.status_code == 200
    assert "出这一单元的听写题" in page.text
    assert f'formaction="/books/{book_id}/units/{unit_id}/generate"' in page.text
    assert 'class="btn"' in page.text  # 用 /static 的组件类，不另写样式
    assert '/static/v3.css?' in page.text
    assert '/static/base.css' not in page.text
    assert 'style="' not in page.text  # 新增 UI 不带任何内联样式
    assert "已生成" not in page.text

    first = c.post(f"/books/{book_id}/units/{unit_id}/generate", follow_redirects=True)
    assert first.status_code == 200
    assert "本单元 17 个词，新增 17 条，跳过 0 条" in first.text
    assert "已生成 17 条" in first.text

    second = c.post(f"/books/{book_id}/units/{unit_id}/generate", follow_redirects=True)
    assert second.status_code == 200
    assert "本单元 17 个词，新增 0 条，跳过 17 条" in second.text
    assert _count(db, "SELECT COUNT(*) FROM question") == 17
    assert _count(db, "SELECT COUNT(*) FROM error_item") == 17


def test_generate_route_rejects_unit_from_another_book(client):
    """路径里的书与单元对不上 → 404，不悄悄出题。"""
    c, db = client
    book_id, unit_id = _seed_real_book(db)
    assert c.post(f"/books/{book_id + 1}/units/{unit_id}/generate").status_code == 404
    assert _count(db, "SELECT COUNT(*) FROM question") == 0


# --------------------------------------------------------------------------- AC6


def test_generated_questions_reach_practice_and_grade(client):
    """生成的题进 /practice；go jogging 判对，go joging 判错并给提示。"""
    c, db = client
    book_id, unit_id = _seed_real_book(db)
    c.post(f"/books/{book_id}/units/{unit_id}/generate")

    page = c.get("/practice")
    assert page.status_code == 200
    assert "今日无题" not in page.text  # 排期不是空的
    assert 'action="/practice/reveal"' in page.text

    item_id = _error_item_id(db, "go jogging")
    right = c.post(
        "/practice/reveal",
        data={"item_id": str(item_id), "learner_answer": "go jogging"},
    )
    assert right.status_code == 200
    # G2-5 文案：「自动判定：正确」→「机器判的 / 对」（判定逻辑不变）
    assert "机器判的" in right.text
    assert re.search(r"机器判的</span>\s*<span>对", right.text)

    wrong = c.post(
        "/practice/reveal",
        data={"item_id": str(item_id), "learner_answer": "go joging"},
    )
    assert wrong.status_code == 200
    assert re.search(r"机器判的</span>\s*<span>不对", wrong.text)
    assert "你是不是想写 go jogging？" in wrong.text


# --------------------------------------------------------------------------- 边界


def test_generate_rejects_unknown_unit(session, learner):
    """单元不存在 → 中文报错，不建任何题。"""
    with pytest.raises(book_service.BookDataError):
        book_service.generate_dictation(session, learner.id, 999999)
    assert session.scalars(select(Question)).all() == []


def test_unit_state_counts_match_generation(session, learner, seeded):
    """页面上的「已生成 N 条」与生成判据同一套：点前 0，点后 17。"""
    book_id, unit_id = seeded
    before = book_service.unit_dictation_state(session, learner.id, book_id)
    assert [(s.unit_no, s.generated) for s in before][0] == (1, 0)
    assert [s.unit_id for s in before if s.unit_no == 1] == [unit_id]

    book_service.generate_dictation(session, learner.id, unit_id)
    after = book_service.unit_dictation_state(session, learner.id, book_id)
    assert dict((s.unit_no, s.generated) for s in after)[1] == 17
    assert [s.generated for s in after if s.unit_no != 1] == [0] * (len(after) - 1)
