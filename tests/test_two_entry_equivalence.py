"""两入口等价：网页 submit_and_judge 与 MCP submit_answer 必须同一判定。

评审阻塞 1 的回归防线：MCP（语音/ESP32 走的链路）不得再挂第二套判题。
同一道题、同一个答案，两侧的 correct 与 needs_review 必须完全一致，
review 簿记（写 good/again vs 进 pending 复核队列）也必须一致。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from cairn import mcp_server
from cairn.graduation_service import pending_review_queue
from cairn.models import REVIEW_STATUS_NEW, ErrorItem, Learner, Question
from cairn.models.review import Review
from cairn.practice_service import submit_and_judge


@pytest.fixture()
def store(tmp_path):
    """文件库 + 一个 learner：与 test_mcp_server 同形（MCP 每次调用单开 Session）。"""
    from cairn.models import init_db, make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{tmp_path / 'cairn.db'}")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        learner = Learner(name="小明")
        session.add(learner)
        session.commit()
        learner_id = learner.id
    try:
        from types import SimpleNamespace

        yield SimpleNamespace(factory=factory, learner_id=learner_id)
    finally:
        engine.dispose()


def _add_item(store, *, answer: str, subject: str = "数学",
              question_type: str = "计算") -> int:
    with store.factory() as session:
        question = Question(
            learner_id=store.learner_id,
            subject=subject,
            grade_level="primary-3",
            question_type=question_type,
            content="12 + 15 = ?",
            answer=answer,
            analysis="先算再验算。",
        )
        session.add(question)
        session.commit()
        item = ErrorItem(
            learner_id=store.learner_id,
            question_id=question.id,
            review_status=REVIEW_STATUS_NEW,
        )
        session.add(item)
        session.commit()
        return item.id


def _web_judge(store, item_id: int, answer: str):
    with store.factory() as session:
        _, grade = submit_and_judge(session, store.learner_id, item_id, answer)
    return grade


def _mcp_judge(store, item_id: int, answer: str) -> mcp_server.Reply:
    with store.factory() as session:
        return mcp_server._submit_answer_reply(
            session, store.learner_id, item_id, answer
        )


def _reviews(store, item_id: int) -> list[Review]:
    with store.factory() as session:
        return list(session.scalars(
            select(Review).where(Review.error_item_id == item_id)
        ))


def _pending_ids(store) -> set[int]:
    with store.factory() as session:
        return {i.id for i in pending_review_queue(session, store.learner_id)}


EQUIV_CASES = [
    ("比号", "3:4", "6:8"),
    ("全角减号", "-3", "−3"),
    ("集合", "{2,3}", "{3,2}"),
    ("中文单位", "100万", "1000000"),
]


@pytest.mark.parametrize("name,answer,learner_answer", EQUIV_CASES, ids=[c[0] for c in EQUIV_CASES])
def test_two_entries_agree_on_equivalence_cases(store, name, answer, learner_answer):
    web_id = _add_item(store, answer=answer)
    mcp_id = _add_item(store, answer=answer)

    grade = _web_judge(store, web_id, learner_answer)
    reply = _mcp_judge(store, mcp_id, learner_answer)

    # 对错一致、复核判定一致
    assert grade.correct is (reply.kind == "correct"), name
    assert grade.needs_review is (reply.kind == "needs_review"), name
    # review 簿记一致：要么都没写，要么写了同一档
    web_reviews, mcp_reviews = _reviews(store, web_id), _reviews(store, mcp_id)
    assert bool(web_reviews) == bool(mcp_reviews)
    if web_reviews:
        assert web_reviews[0].rating == mcp_reviews[0].rating


def test_two_entries_agree_on_low_confidence_pending(store):
    """含糊答案：两侧都不硬判，都落 pending 复核队列。"""
    web_id = _add_item(store, answer="27")
    mcp_id = _add_item(store, answer="27")

    grade = _web_judge(store, web_id, "((")
    reply = _mcp_judge(store, mcp_id, "((")

    assert grade.needs_review is (reply.kind == "needs_review")
    assert _reviews(store, web_id) == [] and _reviews(store, mcp_id) == []
    assert {web_id, mcp_id} <= _pending_ids(store)
