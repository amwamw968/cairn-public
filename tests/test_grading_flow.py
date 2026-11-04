"""判题层接练习流的端到端用例（AC5）：

submit_and_judge → 确定性对错写 review（对→good、错→again）；
低置信度不写 review、错题落入现有 pending 复核队列。
"""

from __future__ import annotations

from sqlalchemy import select

from cairn.entry_service import EntryPayload, create_error_item
from cairn.graduation_service import pending_review_queue
from cairn.models.review import Review
from cairn.practice_service import submit_and_judge


def _make_item(session, learner, subject="数学", question_type="计算",
               content="12 + 15 = ?", answer="27", analysis=None):
    return create_error_item(
        session,
        learner.id,
        EntryPayload(subject=subject, question_type=question_type,
                     content=content, answer=answer, analysis=analysis),
    )


def _reviews(session, error_item_id):
    return list(session.scalars(
        select(Review).where(Review.error_item_id == error_item_id)
    ))


def test_correct_answer_writes_good_review(session, learner):
    item = _make_item(session, learner, answer="二十七")

    revealed, grade = submit_and_judge(session, learner.id, item.id, "27")

    assert grade.correct and grade.confidence >= 0.8 and not grade.needs_review
    reviews = _reviews(session, item.id)
    assert len(reviews) == 1 and reviews[0].rating == "good"
    assert revealed.answer == "二十七"


def test_wrong_answer_writes_again_review_and_counts_wrong(session, learner):
    item = _make_item(session, learner, answer="27")

    _, grade = submit_and_judge(session, learner.id, item.id, "28")

    assert not grade.correct and not grade.needs_review
    reviews = _reviews(session, item.id)
    assert len(reviews) == 1 and reviews[0].rating == "again"
    assert item.wrong_count == 2  # 录入初始 1 + again 判分 +1


def test_ratio_judged_on_reveal(session, learner):
    item = _make_item(session, learner, content="男生女生比", answer="3:4")

    _, grade = submit_and_judge(session, learner.id, item.id, "6:8")

    assert grade.correct  # 比等价，不折算成商


def test_low_confidence_skips_review_enters_pending_queue(session, learner):
    item = _make_item(session, learner, answer="27")

    _, grade = submit_and_judge(session, learner.id, item.id, "((")

    assert grade.needs_review and grade.confidence < 0.8
    assert _reviews(session, item.id) == []  # 没写 review
    assert item.id in [i.id for i in pending_review_queue(session, learner.id)]
