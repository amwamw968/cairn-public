"""0.2 卡8 数学「同类题再练」：服务层检索 + /practice/similar 页面。

数据全用中文（学科「数学」、知识点「进位 / 两位数乘一位数」、题干是中文应用题），
与家里真实录入的形状一致。口径按 docs/CONCEPTS-RESEARCH.md 第四节（B 线）：
共享至少一个 active 知识点、排除原题、共享数降序 → 从未练过 → 到期升序。

覆盖：一题共享、两题共享、无共享、排除原题 + limit、练过优先、到期优先、
老数据补标（新建 / 选已有）、揭示后的入口、列表与练过次数、单题进入练习、
learner 隔离、出库题不捞、页面只读、缺参不 500、零依赖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn import similar_service
from cairn.concept_service import concepts_for_question, tag_question
from cairn.entry_service import ValidationError
from cairn.models import (
    ConceptAlias,
    ErrorItem,
    Learner,
    Question,
    Review,
    REVIEW_STATUS_ARCHIVED,
    REVIEW_STATUS_NEW,
    REVIEW_STATUS_REVIEWING,
    make_engine,
    make_session_factory,
)
from cairn.web import create_app

NOW = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)  # 本地 2026-10-31 12:00
SUBJECT = "数学"


# ---- helpers -------------------------------------------------------------


def _question(
    session,
    content: str,
    answer: str = "92",
    subject: str = SUBJECT,
    qtype: str = "计算",
) -> Question:
    """题目必须有主人：用当前库里最早的那个孩子（fixture learner 已经先建好）。"""
    owner = session.scalars(select(Learner).order_by(Learner.id)).first()
    if owner is None:
        owner = Learner(name="test-child")
        session.add(owner)
        session.commit()
    question = Question(
        learner_id=owner.id,
        subject=subject,
        grade_level="primary-3",
        question_type=qtype,
        content=content,
        answer=answer,
    )
    session.add(question)
    session.commit()
    return question


def _item(
    session,
    learner: Learner,
    question: Question,
    *,
    status: str = REVIEW_STATUS_REVIEWING,
    due: datetime | None = None,
) -> ErrorItem:
    item = ErrorItem(
        learner_id=learner.id,
        question_id=question.id,
        wrong_count=1,
        review_status=status,
        next_due_at=due,
    )
    session.add(item)
    session.commit()
    return item


def _review(session, item: ErrorItem, rating: str = "good") -> Review:
    review = Review(error_item_id=item.id, rating=rating, reviewed_at=NOW)
    session.add(review)
    session.commit()
    return review


def _similar(session, learner: Learner, item: ErrorItem, limit: int = 5):
    return similar_service.find_similar_questions(session, learner.id, item.id, limit=limit)


# ---- AC1 / AC2 共享知识点 -------------------------------------------------


def test_shared_one_concept_finds_the_other_question(session, learner):
    """AC1：两张题共享「进位」→ 原题能捞出另一题（不是重做原题）。"""
    source = _question(session, "23 × 4 = ?", "92")
    other = _question(session, "18 × 5 = ?", "90")
    tag_question(session, source, ["进位"])
    tag_question(session, other, ["进位"])
    session.commit()

    source_item = _item(session, learner, source)
    other_item = _item(session, learner, other)

    rows = _similar(session, learner, source_item)

    assert [row.question_id for row in rows] == [other.id]
    row = rows[0]
    assert row.error_item_id == other_item.id
    assert row.content == "18 × 5 = ?"
    assert row.concept_names == ("进位",)
    assert row.shared_count == 1
    assert row.practice_count == 0
    assert row.practiced is False


def test_shared_two_ranks_first(session, learner):
    """AC2：共享 2 个知识点的题排在只共享 1 个的前面。"""
    source = _question(session, "23 × 4 = ?", "92")
    one_shared = _question(session, "34 × 2 = ?", "68")
    two_shared = _question(session, "27 × 3 = ?", "81")
    tag_question(session, source, ["进位", "两位数乘一位数"])
    tag_question(session, one_shared, ["进位"])
    tag_question(session, two_shared, ["进位", "两位数乘一位数"])
    session.commit()

    source_item = _item(session, learner, source)
    _item(session, learner, one_shared)
    _item(session, learner, two_shared)

    rows = _similar(session, learner, source_item)

    assert [row.question_id for row in rows] == [two_shared.id, one_shared.id]
    assert [row.shared_count for row in rows] == [2, 1]
    assert rows[0].concept_names == ("进位", "两位数乘一位数")


# ---- AC3 没有同类 --------------------------------------------------------


def test_no_similar_returns_empty_list(session, learner):
    """AC3：只有题型相同、知识点不同的题 → 不算是同类。"""
    source = _question(session, "23 × 4 = ?", "92")
    unrelated = _question(session, "x + 3 = 8，x = ?", "5")
    tag_question(session, source, ["进位"])
    tag_question(session, unrelated, ["解方程"])
    session.commit()

    source_item = _item(session, learner, source)
    _item(session, learner, unrelated)

    assert _similar(session, learner, source_item) == []


# ---- AC4 排除原题 + limit ------------------------------------------------


def test_excludes_source_question(session, learner):
    """AC4：原题自身不入结果；limit 生效且不留原题的位置。"""
    source = _question(session, "23 × 4 = ?", "92")
    tag_question(session, source, ["进位"])
    session.commit()
    source_item = _item(session, learner, source)

    others = []
    for text in ("11 × 9 = ?", "12 × 8 = ?", "13 × 7 = ?"):
        question = _question(session, text, "99")
        tag_question(session, question, ["进位"])
        session.commit()
        _item(session, learner, question)
        others.append(question.id)

    rows = _similar(session, learner, source_item)
    assert len(rows) == 3
    assert source.id not in [row.question_id for row in rows]
    assert source_item.id not in [row.error_item_id for row in rows]

    limited = _similar(session, learner, source_item, limit=2)
    assert [row.question_id for row in limited] == others[:2]


# ---- AC5 排序后两级 ------------------------------------------------------


def test_never_practiced_first(session, learner):
    """AC5：共享数相同 → 没练过的排在练过的前面。"""
    source = _question(session, "23 × 4 = ?", "92")
    practiced = _question(session, "34 × 2 = ?", "68")
    fresh = _question(session, "35 × 2 = ?", "70")
    for question in (source, practiced, fresh):
        tag_question(session, question, ["进位"])
    session.commit()

    source_item = _item(session, learner, source)
    practiced_item = _item(session, learner, practiced)
    _review(session, practiced_item)
    _item(session, learner, fresh)

    rows = _similar(session, learner, source_item)

    assert [row.question_id for row in rows] == [fresh.id, practiced.id]
    assert rows[0].practice_count == 0
    assert rows[1].practice_count == 1


def test_due_first_when_both_practiced(session, learner):
    """AC5：都练过 → 到期早的优先；没有到期时间的排最后。"""
    source = _question(session, "23 × 4 = ?", "92")
    later = _question(session, "34 × 2 = ?", "68")
    sooner = _question(session, "35 × 2 = ?", "70")
    no_due = _question(session, "36 × 2 = ?", "72")
    for question in (source, later, sooner, no_due):
        tag_question(session, question, ["进位"])
    session.commit()

    source_item = _item(session, learner, source)
    later_item = _item(session, learner, later, due=NOW + timedelta(days=5))
    _review(session, later_item)
    sooner_item = _item(session, learner, sooner, due=NOW + timedelta(days=1))
    _review(session, sooner_item)
    no_due_item = _item(session, learner, no_due)
    _review(session, no_due_item)

    rows = _similar(session, learner, source_item)

    assert [row.question_id for row in rows] == [sooner.id, later.id, no_due.id]
    assert rows[-1].next_due_at is None


# ---- AC6 老数据补标 ------------------------------------------------------


def test_untagged_question_asks_for_a_tag(session, learner):
    """AC6：没标知识点的题 → 走补标分支（view.untagged + 下拉选项）。"""
    source = _question(session, "23 × 4 = ?", "92")
    existing = _question(session, "34 - 18 = ?", "16")
    tag_question(session, existing, ["退位"])
    session.commit()
    source_item = _item(session, learner, source)

    view = similar_service.build_similar_view(session, learner.id, source_item.id)

    assert view.untagged is True
    assert view.concept_names == ()
    assert view.items == ()
    assert [option.name for option in view.concept_options] == ["退位"]
    assert _similar(session, learner, source_item) == []


def test_tag_form_creates_and_attaches_new_concept(session, learner):
    """AC6：补标（新建「进位」）后原题立刻能捞出同类题。"""
    source = _question(session, "23 × 4 = ?", "92")
    other = _question(session, "18 × 5 = ?", "90")
    tag_question(session, other, ["进位"])
    session.commit()
    source_item = _item(session, learner, source)
    _item(session, learner, other)

    concept = similar_service.tag_source(
        session, learner.id, source_item.id, new_name="进位"
    )

    assert concept.name == "进位"
    rows = _similar(session, learner, source_item)
    assert [row.question_id for row in rows] == [other.id]
    # 幂等：再挂一次不会崩，也不会多一个知识点
    similar_service.tag_source(session, learner.id, source_item.id, new_name="进位")
    assert len(similar_service.build_similar_view(session, learner.id, source_item.id).concept_names) == 1


def test_tag_form_with_existing_concept(session, learner):
    """AC6：也可以从下拉里选已有知识点；跨学科的知识点会被拒。"""
    source = _question(session, "23 × 4 = ?", "92")
    carry = tag_question(session, _question(session, "18 × 5 = ?", "90"), ["进位"])[0]
    english = tag_question(
        session, _question(session, "apple", "苹果", subject="英语", qtype="单词"), ["固定搭配:keep cool"]
    )[0]
    session.commit()
    source_item = _item(session, learner, source)

    similar_service.tag_source(session, learner.id, source_item.id, concept_id=carry.id)
    assert similar_service.build_similar_view(session, learner.id, source_item.id).concept_names == (
        "进位",
    )

    other_source = _item(session, learner, _question(session, "36 ÷ 4 = ?", "9"))
    with pytest.raises(ValueError):
        similar_service.tag_source(
            session, learner.id, other_source.id, concept_id=english.id
        )


def test_tag_new_name_stays_in_subject_despite_global_alias(session, learner):
    """AC6：全局别名把「计算」指到英语知识点时，数学补标「计算」不得串科。

    别名表全局唯一（``concept_aliases.alias``），迁移/历史数据可能留下一条把
    「计算」指向英语知识点的别名。数学题补标「计算」时必须在本学科拿到自己的
    「计算」，不能借到英语那个上——否则同类题检索会跨科。
    """
    english = tag_question(
        session,
        _question(session, "calculate 3 + 4", "7", subject="英语", qtype="单词"),
        ["计算"],
    )[0]
    # 直接落一行全局别名：'计算' → 英语知识点（add_alias 会因与规范名同名而拒绝，
    # 这里模拟迁移留下的跨科别名）。
    session.add(ConceptAlias(concept_id=english.id, alias="计算"))
    session.commit()

    source = _question(session, "23 × 4 = ?", "92")
    session.commit()
    source_item = _item(session, learner, source)

    concept = similar_service.tag_source(
        session, learner.id, source_item.id, new_name="计算"
    )

    assert concept.subject == "数学"
    assert concept.id != english.id

    attached = concepts_for_question(session, source.id)
    assert english.id not in [c.id for c in attached]
    assert [c.name for c in attached] == ["计算"]
    assert attached[0].subject == "数学"


# ---- web：揭示入口 / 列表 / 单题进入 / 只读 -------------------------------


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/similar.db")
    with TestClient(app) as test_client:
        yield test_client, str(tmp_path / "similar.db")


def _seed(db_path: str) -> dict[str, int]:
    """两张共享「进位」的题：原题练过 0 次，同类题练过 1 次。"""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    with factory() as session:
        learner = session.scalars(select(Learner).order_by(Learner.id)).first()
        source = _question(session, "23 × 4 = ?", "92")
        other = _question(session, "18 × 5 = ?", "90")
        tag_question(session, source, ["进位"])
        tag_question(session, other, ["进位"])
        session.commit()
        source_item = _item(session, learner, source)
        other_item = _item(session, learner, other)
        _review(session, other_item)
        ids = {
            "source_question": source.id,
            "source_item": source_item.id,
            "other_question": other.id,
            "other_item": other_item.id,
        }
    engine.dispose()
    return ids


def test_reveal_button_links_to_similar_page(client):
    """AC7：揭示答案后出现「再来一道同类的」；答题态页面没有这个入口。"""
    c, db = client
    ids = _seed(db)

    ask = c.get("/practice")
    assert "再来一道同类的" not in ask.text

    revealed = c.post(
        "/practice/reveal",
        data={"item_id": ids["source_item"], "learner_answer": "92"},
    )
    assert "再来一道同类的" in revealed.text
    assert f"/practice/similar?item_id={ids['source_item']}" in revealed.text


def test_list_page_shows_tags_and_practice_counts(client):
    """AC8：列表显示知识点标签与练过次数，并排除原题。"""
    c, db = client
    ids = _seed(db)

    page = c.get(f"/practice/similar?item_id={ids['source_item']}")

    assert page.status_code == 200
    assert "18 × 5 = ?" in page.text
    assert "23 × 4 = ?" in page.text  # 原题作为「原题」抬头出现
    assert "进位" in page.text
    assert "练过 1 次" in page.text
    assert f"/practice?item_id={ids['other_item']}" in page.text
    assert "这个知识点目前只有这一道题" not in page.text


def test_no_similar_page_says_so_with_two_ways_out(client):
    """AC3：没有同类 → 明说「只有这一道题」+ 去录入 / 记下这个知识点两个入口。"""
    c, db = client
    engine = make_engine(f"sqlite:///{db}")
    factory = make_session_factory(engine)
    with factory() as session:
        learner = session.scalars(select(Learner).order_by(Learner.id)).first()
        question = _question(session, "23 × 4 = ?", "92")
        tag_question(session, question, ["进位"])
        session.commit()
        item = _item(session, learner, question)
        item_id = item.id
    engine.dispose()

    page = c.get(f"/practice/similar?item_id={item_id}")

    assert page.status_code == 200
    assert "这个知识点目前只有这一道题" in page.text
    assert 'href="/entry"' in page.text and "去手动输入一道同类的" in page.text
    assert "记下这个知识点" in page.text
    # 不假装有数据：没有列表项、没有「练这道」
    assert "练这道" not in page.text


def test_untagged_page_shows_tag_form_then_finds_similar(client):
    """AC6：老数据 → 页面要求补标；补标后同类题出现。"""
    c, db = client
    engine = make_engine(f"sqlite:///{db}")
    factory = make_session_factory(engine)
    with factory() as session:
        learner = session.scalars(select(Learner).order_by(Learner.id)).first()
        source = _question(session, "23 × 4 = ?", "92")
        other = _question(session, "18 × 5 = ?", "90")
        tag_question(session, other, ["进位"])
        session.commit()
        source_item = _item(session, learner, source)
        _item(session, learner, other)
        ids = {"item": source_item.id}
    engine.dispose()

    page = c.get(f"/practice/similar?item_id={ids['item']}")
    assert "这道题还没标知识点，先去补一个" in page.text
    assert 'action="/practice/similar/tag"' in page.text
    assert '<select name="concept_id">' in page.text
    assert "18 × 5 = ?" not in page.text  # 还没标签，不硬凑结果

    tagged = c.post(
        "/practice/similar/tag",
        data={"item_id": ids["item"], "concept_id": "", "new_concept": "进位"},
        follow_redirects=True,
    )
    assert tagged.status_code == 200
    assert "18 × 5 = ?" in tagged.text
    assert "这道题还没标知识点" not in tagged.text


def test_practice_by_item_hides_answer(client):
    """AC8：点进去走现有练习流程，答题态不泄漏标准答案。"""
    c, db = client
    ids = _seed(db)

    page = c.get(f"/practice?item_id={ids['other_item']}")

    assert page.status_code == 200
    assert "18 × 5 = ?" in page.text
    # 按文本节点边界查泄漏（V3.1 内联图标的路径数据里含 "90" 子串，裸子串会误报）
    assert ">90<" not in page.text
    assert 'action="/practice/reveal"' in page.text


def test_practice_by_item_rejects_unknown_item(client):
    """AC10：给了不存在的 item_id 也只是提示，不 500。"""
    c, _ = client
    page = c.get("/practice?item_id=9999")
    assert page.status_code == 200
    assert "这道题不在你的题库里" in page.text


def test_similar_is_read_only(client):
    """AC10：GET 同类页不动 review_status / next_due_at / last_reviewed_at。"""
    c, db = client
    ids = _seed(db)

    def snapshot() -> list[tuple]:
        engine = make_engine(f"sqlite:///{db}")
        factory = make_session_factory(engine)
        with factory() as session:
            rows = [
                (item.id, item.review_status, item.next_due_at, item.last_reviewed_at)
                for item in session.scalars(select(ErrorItem).order_by(ErrorItem.id))
            ]
        engine.dispose()
        return rows

    before = snapshot()
    c.get(f"/practice/similar?item_id={ids['source_item']}")
    c.get(f"/practice?item_id={ids['other_item']}")
    assert snapshot() == before


def test_missing_item_id(client):
    """AC10：不带 item_id 也 200，明确说缺参数。"""
    c, _ = client
    page = c.get("/practice/similar")
    assert page.status_code == 200
    assert "缺少 item_id" in page.text


# ---- AC9 隔离 ------------------------------------------------------------


def test_learner_isolation(session, learner):
    """AC9：别人的题不能出现在我的同类列表里。"""
    other_learner = Learner(name="另一个孩子")
    session.add(other_learner)
    session.commit()

    source = _question(session, "23 × 4 = ?", "92")
    mine = _question(session, "18 × 5 = ?", "90")
    theirs = _question(session, "19 × 5 = ?", "95")
    for question in (source, mine, theirs):
        tag_question(session, question, ["进位"])
    session.commit()

    source_item = _item(session, learner, source)
    _item(session, learner, mine)
    theirs_item = _item(session, other_learner, theirs)

    rows = _similar(session, learner, source_item)
    assert [row.question_id for row in rows] == [mine.id]

    with pytest.raises(ValidationError):
        similar_service.find_similar_questions(session, learner.id, theirs_item.id)


def test_archived_excluded(session, learner):
    """AC9：全部出库的题不再往回捞。"""
    source = _question(session, "23 × 4 = ?", "92")
    archived = _question(session, "18 × 5 = ?", "90")
    fresh = _question(session, "19 × 5 = ?", "95")
    for question in (source, archived, fresh):
        tag_question(session, question, ["进位"])
    session.commit()

    source_item = _item(session, learner, source, status=REVIEW_STATUS_NEW)
    _item(session, learner, archived, status=REVIEW_STATUS_ARCHIVED)
    _item(session, learner, fresh)

    assert [row.question_id for row in _similar(session, learner, source_item)] == [fresh.id]


# ---- AC11 零依赖 / 只查库 -------------------------------------------------


def test_service_is_query_only():
    """AC11：不 import 调度器/网络/模型客户端，避免同类检索偷偷改排期。"""
    source = Path(similar_service.__file__).read_text(encoding="utf-8")
    for forbidden in ("scheduler", "fsrs", "requests", "httpx", "urllib", "openai"):
        assert forbidden not in source
