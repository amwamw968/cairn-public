"""B3 card 2 — graduation (outboxing): streak + quiet-days rule, query, restore.

0.3 卡2 双条件改版：出库 = 连续 2 次 Good/Easy + 至少一条变式题答对 + 静默 ≥3 天。
原「连续 3 次、无变式即出库」的断言与双条件直接冲突，逐条改写见 PLAN.md
「既有断言冲突」；静默门、restore 门、队列排除语义的断言原样保留。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cairn.graduation_service import (
    archived_items,
    evaluate_graduation,
    needs_variant_items,
    restore,
)
from cairn.models import ErrorItem, Question
from cairn.practice_service import fetch_practice
from cairn.review_service import grade

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _make_item(session, learner, question) -> ErrorItem:
    item = ErrorItem(learner_id=learner.id, question_id=question.id)
    session.add(item)
    session.commit()
    return item


def _grade_series(session, item, ratings, start=T0, step_days=1):
    for i, rating in enumerate(ratings):
        grade(session, item.id, rating, now=start + timedelta(days=i * step_days))


def _add_variant_with_correct_review(session, learner, question, when):
    """变式题（variant_of 指向原题）+ 一条 Good 复习记录（答对）。"""
    variant_q = Question(
        learner_id=learner.id,
        subject=question.subject,
        grade_level=question.grade_level,
        question_type=question.question_type,
        content=question.content + "（变式）",
        answer=question.answer,
        variant_of=question.id,
    )
    session.add(variant_q)
    session.commit()
    v_item = ErrorItem(learner_id=learner.id, question_id=variant_q.id)
    session.add(v_item)
    session.commit()
    grade(session, v_item.id, "good", now=when)
    return variant_q


def test_two_streak_plus_variant_pass_archives(session, learner, question):
    """0.3 卡2 AC2：连续 2 次答对 + 变式答对 1 次 + 静默 ≥3 天 → archived。"""
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good", "good"])
    _add_variant_with_correct_review(session, learner, question, T0 + timedelta(days=2))

    # 静默门未到（最后一次原题复习在 T0+1d）
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=3)) is False

    # 静默 ≥3 天后出库
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=4)) is True
    assert item.review_status == "archived"


def test_streak_of_5_without_variant_blocks_as_needs_variant(
    session, learner, question, caplog
):
    """0.3 卡2 AC1：原题连续答对 5 次、无变式 → 不是 archived，是 needs_variant，
    原因可通过 needs_variant_items 与日志查到。"""
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good"] * 5)

    assert evaluate_graduation(session, item, now=T0 + timedelta(days=8)) is False
    assert item.review_status == "needs_variant"

    ids = [i.id for i in needs_variant_items(session, learner.id)]
    assert item.id in ids
    assert item.id not in [a.id for a in archived_items(session, learner.id)]

    assert any("missing correct variant review" in r.message for r in caplog.records)


def test_variant_correct_but_streak_one_not_out(session, learner, question):
    """0.3 卡2 AC3：变式答对但原题只对 1 次 → 不出库，留在 reviewing。"""
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good"])
    _add_variant_with_correct_review(session, learner, question, T0 + timedelta(days=1))

    assert evaluate_graduation(session, item, now=T0 + timedelta(days=5)) is False
    assert item.review_status == "reviewing"


def test_variant_wrong_rating_does_not_count(session, learner, question):
    """变式答错（again）不算「变式答对」：streak 够也只能进 needs_variant。"""
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good", "good"])

    variant_q = Question(
        learner_id=learner.id,
        subject=question.subject,
        grade_level=question.grade_level,
        question_type=question.question_type,
        content=question.content + "（变式）",
        answer=question.answer,
        variant_of=question.id,
    )
    session.add(variant_q)
    session.commit()
    v_item = ErrorItem(learner_id=learner.id, question_id=variant_q.id)
    session.add(v_item)
    session.commit()
    grade(session, v_item.id, "again", now=T0 + timedelta(days=1))

    assert evaluate_graduation(session, item, now=T0 + timedelta(days=5)) is False
    assert item.review_status == "needs_variant"


def test_needs_variant_returns_to_reviewing_after_wrong_answer(session, learner, question):
    """needs_variant 上 Again 打断 streak → 退回 reviewing（0.3 卡2 状态机）。"""
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good", "good"])
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=4)) is False
    assert item.review_status == "needs_variant"

    grade(session, item.id, "again", now=T0 + timedelta(days=4, hours=1))
    assert item.review_status == "reviewing"
    assert item.id not in [i.id for i in needs_variant_items(session, learner.id)]


def test_needs_variant_archives_when_variant_lands(session, learner, question):
    """先卡在 needs_variant，之后补上变式答对 → 再巡检即可出库。"""
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good", "good"])
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=4)) is False
    assert item.review_status == "needs_variant"

    _add_variant_with_correct_review(session, learner, question, T0 + timedelta(days=5))
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=5)) is True
    assert item.review_status == "archived"


def test_streak_broken_by_again_or_hard(session, learner, question):
    item = _make_item(session, learner, question)
    # trailing streak is only 1 (hard breaks it): good, hard, easy
    _grade_series(session, item, ["good", "hard", "easy"])
    _add_variant_with_correct_review(session, learner, question, T0 + timedelta(days=1))

    assert evaluate_graduation(session, item, now=T0 + timedelta(days=8)) is False
    assert item.review_status == "reviewing"

    # one more good completes a trailing streak of 2
    grade(session, item.id, "good", now=T0 + timedelta(days=9))
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=12)) is True
    assert item.review_status == "archived"


def test_three_day_gate(session, learner, question):
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good", "good"])
    _add_variant_with_correct_review(session, learner, question, T0 + timedelta(days=2))
    # last review at T0 + 1d

    # 静默 1d23h：静默门未到
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=2, hours=23)) is False
    assert item.review_status == "reviewing"

    # 静默恰好 3 天：门开，出库
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=4)) is True
    assert item.review_status == "archived"


def test_archived_queryable_and_restorable(session, learner, question):
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good", "good"])
    _add_variant_with_correct_review(session, learner, question, T0 + timedelta(days=2))
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=4)) is True

    # archived stays out of the daily queue (B2 behavior re-checked)
    due_at = item.next_due_at
    batch = fetch_practice(session, learner.id, now=due_at, limit=10)
    assert item.id not in [p.error_item_id for p in batch]

    # ... but remains queryable
    ids = [a.id for a in archived_items(session, learner.id)]
    assert item.id in ids

    # restore: archived -> reviewing, out of the archive query
    restored = restore(session, item.id)
    assert restored.review_status == "reviewing"
    assert item.id not in [a.id for a in archived_items(session, learner.id)]

    # and it re-enters the practice pool once due again
    batch_after = fetch_practice(session, learner.id, now=due_at, limit=10)
    assert item.id in [p.error_item_id for p in batch_after]


def test_restored_item_not_regraduated_before_new_review(session, learner, question):
    """Review fix (a): a sweep right after restore must NOT re-archive —
    the pre-restore streak stays blocked until a fresh review lands,
    no matter how much time passes."""
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good", "good"])
    _add_variant_with_correct_review(session, learner, question, T0 + timedelta(days=2))
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=4)) is True
    assert item.review_status == "archived"

    restored = restore(session, item.id, now=T0 + timedelta(days=6))
    assert restored.review_status == "reviewing"
    assert restored.restored_at == T0 + timedelta(days=6)

    # sweep one day after restore: old streak + quiet days would fire
    # without the restore gate
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=7)) is False
    assert item.review_status == "reviewing"

    # and still blocked much later — only a new review can re-open graduation
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=60)) is False
    assert item.review_status == "reviewing"


def test_restored_item_graduates_again_after_new_review(session, learner, question):
    """Review fix (b): after restore + a fresh Good review, graduation
    becomes possible again — and only once the quiet-days gate re-opens
    counted from the NEW review."""
    item = _make_item(session, learner, question)
    _grade_series(session, item, ["good", "good"])
    _add_variant_with_correct_review(session, learner, question, T0 + timedelta(days=2))
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=4)) is True

    restore(session, item.id, now=T0 + timedelta(days=6))

    # re-practice: one fresh good review after the restore
    grade(session, item.id, "good", now=T0 + timedelta(days=9))
    assert item.review_status == "reviewing"

    # quiet days restart from the new review: +2d after it -> still reviewing
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=11)) is False
    assert item.review_status == "reviewing"

    # +3 days after the new review -> graduates again
    assert evaluate_graduation(session, item, now=T0 + timedelta(days=12)) is True
    assert item.review_status == "archived"
