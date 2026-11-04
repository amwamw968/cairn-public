"""Graduation (outboxing) and pending-review queue.

Graduation rule (0.3 卡2, dual condition — no graduating by rote):
    trailing streak of >= 2 Good/Easy reviews on the item itself
    (Again/Hard breaks the streak)
    AND at least one correct (Good/Easy) review on a variant question
    (a question whose variant_of points at this item's question)
    AND the last review is >= 3 days old (last_reviewed <= now - 3 days)
    -> review_status = archived.

    If the streak + quiet-days gates are met but no correct variant review
    exists, the item cannot graduate: reviewing -> needs_variant (0.3 卡2).
    needs_variant items stay in the practice pool and are listed by
    needs_variant_items(); a Again/Hard review on one returns it to
    reviewing (streak broken). Archived items stay out of the daily
    practice queue (fetch_practice already excludes them), remain
    queryable via archived_items, and can be restored via restore().

Pending-review queue:
    confidence < 0.8 AND never reviewed (no REVIEW record) AND not archived.
    confirm_review sets confidence = 1.0; reject_review archives the item.

Restore gate (B3 review fix):
    restore() records restored_at on the item; evaluate_graduation requires
    the most recent review to be NEWER than the last restore, so a restored
    item must be re-practiced (at least one fresh review) before it can
    graduate again — the pre-restore streak can never re-archive it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.entry_service import ValidationError
from cairn.models.error_item import (
    ErrorItem,
    REVIEW_STATUS_ARCHIVED,
    REVIEW_STATUS_MASTERED,
    REVIEW_STATUS_NEEDS_VARIANT,
    REVIEW_STATUS_REVIEWING,
)
from cairn.models.question import Question
from cairn.models.review import RATING_EASY, RATING_GOOD, Review
from cairn.models.learner import utcnow

logger = logging.getLogger(__name__)

_STREAK_RATINGS = frozenset({RATING_GOOD, RATING_EASY})
_MIN_STREAK = 2
_QUIET_DAYS = timedelta(days=3)
_PENDING_CONFIDENCE_MAX = 0.8


def _as_utc(dt: datetime) -> datetime:
    """Normalize a timestamp for comparison. SQLite round-trips DateTime
    columns without tzinfo; stored values are UTC, so naive means UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _has_correct_variant(session: Session, error_item: ErrorItem) -> bool:
    """至少一条变式题（variant_of 指向本题 question）答对（Good/Easy）的记录。

    变式记录按 learner 隔离：别的孩子在同知识点变式上的答对不算数。
    """
    correct_variants = (
        select(func.count())
        .select_from(Review)
        .join(ErrorItem, ErrorItem.id == Review.error_item_id)
        .join(Question, Question.id == ErrorItem.question_id)
        .where(
            Question.variant_of == error_item.question_id,
            ErrorItem.learner_id == error_item.learner_id,
            Review.rating.in_(frozenset(_STREAK_RATINGS)),
        )
    )
    return int(session.scalar(correct_variants) or 0) > 0


def evaluate_graduation(session: Session, error_item: ErrorItem, now: datetime) -> bool:
    """Apply the 0.3 dual-condition graduation rule to *error_item* against *now*.

    Returns True iff the item was archived by this call. False means the
    rule did not fire; when the streak + quiet-days gates are met but no
    correct variant review exists, the item is parked in needs_variant
    (from reviewing) so the "missing variant" state is visible and
    queryable — this is a state change, not an archive.

    Restore gate: if the item was ever restored (restored_at set), the most
    recent review must be newer than the last restore — otherwise the
    pre-restore streak would re-archive it on the very next sweep.
    """
    if error_item.review_status not in (REVIEW_STATUS_REVIEWING, REVIEW_STATUS_NEEDS_VARIANT):
        return False

    reviews = list(
        session.scalars(
            select(Review)
            .where(Review.error_item_id == error_item.id)
            .order_by(Review.reviewed_at.desc())
        )
    )
    streak_ok = len(reviews) >= _MIN_STREAK and all(
        r.rating in _STREAK_RATINGS for r in reviews[:_MIN_STREAK]
    )
    if not streak_ok:
        if error_item.review_status == REVIEW_STATUS_NEEDS_VARIANT:
            # the trailing streak broke while parked in needs_variant:
            # back to the normal pipeline until it is rebuilt
            error_item.transition_to(REVIEW_STATUS_REVIEWING)
            session.commit()
        return False
    if _as_utc(reviews[0].reviewed_at) > _as_utc(now) - _QUIET_DAYS:
        return False
    if (
        error_item.restored_at is not None
        and _as_utc(reviews[0].reviewed_at) <= _as_utc(error_item.restored_at)
    ):
        # no review has landed since the last restore: the trailing streak
        # is the pre-restore one, so graduation must not fire
        return False

    if not _has_correct_variant(session, error_item):
        if error_item.review_status == REVIEW_STATUS_REVIEWING:
            error_item.transition_to(REVIEW_STATUS_NEEDS_VARIANT)
            session.commit()
            logger.warning(
                "graduation blocked, missing correct variant review: "
                "error_item_id=%s question_id=%s learner_id=%s status=needs_variant",
                error_item.id,
                error_item.question_id,
                error_item.learner_id,
            )
        return False

    error_item.transition_to(REVIEW_STATUS_ARCHIVED)
    session.commit()
    return True


def needs_variant_items(session: Session, learner_id: int) -> list[ErrorItem]:
    """Items parked in needs_variant: streak met but no correct variant
    review yet (0.3 卡2). Queryable evidence of "缺变式卡住"."""
    return list(
        session.scalars(
            select(ErrorItem).join(Question, Question.id == ErrorItem.question_id)
            .where(
                ErrorItem.learner_id == learner_id,
                Question.learner_id == learner_id,
                ErrorItem.review_status == REVIEW_STATUS_NEEDS_VARIANT,
            )
            .order_by(ErrorItem.id.asc())
        )
    )


def archived_items(session: Session, learner_id: int) -> list[ErrorItem]:
    """All archived items of *learner_id* (queryable, oldest first)."""
    return list(
        session.scalars(
            select(ErrorItem).join(Question, Question.id == ErrorItem.question_id)
            .where(
                ErrorItem.learner_id == learner_id,
                Question.learner_id == learner_id,
                ErrorItem.review_status == REVIEW_STATUS_ARCHIVED,
            )
            .order_by(ErrorItem.id.asc())
        )
    )


def restore(session: Session, error_item_id: int, now: datetime | None = None) -> ErrorItem:
    """Manually restore an archived item to the reviewing pipeline.

    Path: archived -> mastered -> reviewing (the mastered -> reviewing
    rollback is a pre-existing legal transition). Only archived items can
    be restored; scheduling state (next_due_at) is untouched, so the item
    re-enters the practice pool whenever it is next due.

    The restore is stamped into restored_at: evaluate_graduation ignores
    the pre-restore streak until a review lands after this timestamp, so
    the next sweep cannot immediately re-archive the restored item.
    """
    item = session.get(ErrorItem, error_item_id)
    if item is None:
        raise ValidationError(f"error_item does not exist: error_item_id={error_item_id}")
    if item.workflow_rejected:
        raise ValidationError("这道题已在卷子核对时排除，如需学习请重新录入")
    if item.review_status != REVIEW_STATUS_ARCHIVED:
        raise ValidationError(
            f"only archived items can be restored: status={item.review_status!r}"
        )
    item.transition_to(REVIEW_STATUS_MASTERED)
    item.transition_to(REVIEW_STATUS_REVIEWING)
    item.restored_at = now if now is not None else utcnow()
    session.commit()
    return item


def pending_review_queue(session: Session, learner_id: int) -> list[ErrorItem]:
    """Doubtful, never-reviewed, non-archived items awaiting parent confirm."""
    from cairn.workflow.review import ready_for_practice

    return list(
        session.scalars(
            select(ErrorItem).join(Question, Question.id == ErrorItem.question_id)
            .where(
                ErrorItem.learner_id == learner_id,
                Question.learner_id == learner_id,
                ErrorItem.confidence < _PENDING_CONFIDENCE_MAX,
                ErrorItem.last_reviewed_at.is_(None),
                ErrorItem.review_status != REVIEW_STATUS_ARCHIVED,
                ready_for_practice(ErrorItem.question_id),
            )
            .order_by(ErrorItem.created_at.asc())
        )
    )


def confirm_review(session: Session, error_item_id: int, *, learner_id: int | None = None) -> ErrorItem:
    """Parent confirms the doubtful entry: confidence becomes 1.0."""
    item = session.get(ErrorItem, error_item_id)
    if item is None:
        if learner_id is not None:
            raise ValidationError("这道题不在你的题库里")
        raise ValidationError(f"error_item does not exist: error_item_id={error_item_id}")
    if learner_id is not None:
        question = session.get(Question, item.question_id)
        if item.learner_id != learner_id or question is None or question.learner_id != learner_id:
            raise ValidationError("这道题不在你的题库里")
    from cairn.workflow.review import ready_for_practice
    if not session.scalar(select(ready_for_practice(item.question_id))):
        raise ValidationError("请在卷子的工作流中核对这道题")
    item.confidence = 1.0
    session.commit()
    return item


def reject_review(session: Session, error_item_id: int, *, learner_id: int | None = None) -> ErrorItem:
    """Parent rejects the doubtful entry: the item is archived."""
    item = session.get(ErrorItem, error_item_id)
    if item is None:
        if learner_id is not None:
            raise ValidationError("这道题不在你的题库里")
        raise ValidationError(f"error_item does not exist: error_item_id={error_item_id}")
    if learner_id is not None:
        question = session.get(Question, item.question_id)
        if item.learner_id != learner_id or question is None or question.learner_id != learner_id:
            raise ValidationError("这道题不在你的题库里")
    from cairn.workflow.review import ready_for_practice
    if not session.scalar(select(ready_for_practice(item.question_id))):
        raise ValidationError("请在卷子的工作流中核对这道题")
    item.transition_to(REVIEW_STATUS_ARCHIVED)
    session.commit()
    return item
