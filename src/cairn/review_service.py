"""Review integration glue: rating an ERROR_ITEM end-to-end.

One function applies a review:

    rating -> strategy schedule (default FSRS-6) -> REVIEW row + ERROR_ITEM
    write-back (S/D/next_due_at) -> review_status transition (AC2 rules)

「到期时间怎么算」走策略层（cairn.strategies）：learner.strategy_id 优先，
否则跟 CAIRN_DEFAULT_STRATEGY / 内置 fsrs。分科只影响「用哪个目标记忆率」
（数学 learner.desired_retention_math 默认 0.85，其余 0.90）——这层选择在
FsrsStrategy 内部沿用 scheduler_for，行为与重构前逐位一致。
这里没有第二套间隔表。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.entry_service import ValidationError
from cairn.models.error_item import (
    ErrorItem,
    REVIEW_STATUS_NEEDS_VARIANT,
    REVIEW_STATUS_NEW,
    REVIEW_STATUS_REVIEWING,
)
from cairn.models.learner import Learner, utcnow
from cairn.models.question import Question
from cairn.models.review import (
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
    Review,
)
from cairn.strategies.registry import get_strategy, strategy_id_for

_RATINGS = frozenset({RATING_AGAIN, RATING_HARD, RATING_GOOD, RATING_EASY})


def apply_review(
    session: Session,
    error_item: ErrorItem,
    rating: str,
    now: datetime | None = None,
    duration_ms: int | None = None,
    *,
    commit: bool = True,
) -> Review:
    """Apply one review to *error_item* and persist all effects."""
    from cairn.workflow.review import ready_for_practice

    if not session.scalar(select(ready_for_practice(error_item.question_id))):
        raise ValidationError("题目仍在核对或安排复习，请稍后再练")
    review_at = now if now is not None else utcnow()

    # 学科取自题目（同一错题只有一道题）；learner 取自己的分科目标与策略。
    question = session.get(Question, error_item.question_id)
    learner = session.get(Learner, error_item.learner_id)
    strategy = get_strategy(error_item.workflow_strategy_id or strategy_id_for(learner))
    state = {
        **strategy.initial_state(error_item),
        "subject": question.subject if question else None,
        "learner": learner,
    }
    result = strategy.on_grade(state, rating, review_at)

    review = Review(
        error_item_id=error_item.id,
        rating=rating,
        reviewed_at=review_at,
        review_duration_ms=duration_ms,
        stability_after=result["stability"],
        difficulty_after=result["difficulty"],
        next_due_at=result["due"],
    )
    session.add(review)

    error_item.stability = result["stability"]
    error_item.difficulty = result["difficulty"]
    error_item.next_due_at = result["due"]
    error_item.last_reviewed_at = review_at

    if rating == RATING_AGAIN:
        error_item.wrong_count += 1
        error_item.last_wrong_at = review_at

    # AC2 rule: first review moves new -> reviewing
    if error_item.review_status == REVIEW_STATUS_NEW:
        error_item.transition_to(REVIEW_STATUS_REVIEWING)

    # 0.3 卡2: a wrong answer on a needs_variant item breaks the trailing
    # streak — back to the normal reviewing pipeline until it is rebuilt
    if (
        error_item.review_status == REVIEW_STATUS_NEEDS_VARIANT
        and rating in (RATING_AGAIN, RATING_HARD)
    ):
        error_item.transition_to(REVIEW_STATUS_REVIEWING)

    if commit:
        session.commit()
    else:
        session.flush()
    return review


def grade(
    session: Session,
    error_item_id: int,
    rating: str,
    now: datetime | None = None,
    duration_ms: int | None = None,
    *,
    learner_id: int | None = None,
) -> Review:
    """One-command grading entry (B3 card 1): load the item by id, validate
    the four-way rating, then delegate all bookkeeping to apply_review.

    No scheduling logic here — apply_review owns REVIEW + ERROR_ITEM
    write-back, wrong counting and the status transition. Validation happens
    before any write, so failures leave the DB untouched.
    """
    item = session.get(ErrorItem, error_item_id)
    if item is None:
        if learner_id is not None:
            raise ValidationError("这道题不在你的题库里")
        raise ValidationError(f"error_item does not exist: error_item_id={error_item_id}")
    if learner_id is not None:
        question = session.get(Question, item.question_id)
        if item.learner_id != learner_id or question is None or question.learner_id != learner_id:
            raise ValidationError("这道题不在你的题库里")
    if rating not in _RATINGS:
        raise ValidationError(f"rating must be one of again/hard/good/easy: {rating!r}")
    return apply_review(session, item, rating, now=now, duration_ms=duration_ms)
