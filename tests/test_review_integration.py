"""AC4 — scheduler integrates with ERROR_ITEM / REVIEW."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from cairn.models import ErrorItem, REVIEW_STATUS_REVIEWING
from cairn.models.review import RATING_AGAIN, RATING_GOOD, Review
from cairn.review_service import apply_review

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def test_review_updates_error_item(session, learner, question):
    item = ErrorItem(learner_id=learner.id, question_id=question.id)
    session.add(item)
    session.commit()

    review = apply_review(session, item, RATING_GOOD, now=NOW)

    # REVIEW ledger row with rating + timestamp
    rows = list(session.scalars(select(Review)).all())
    assert len(rows) == 1
    assert rows[0].id == review.id
    assert rows[0].error_item_id == item.id
    assert rows[0].rating == RATING_GOOD
    assert rows[0].reviewed_at == NOW

    # ERROR_ITEM FSRS state written back
    assert item.stability is not None and item.stability > 0
    assert item.difficulty is not None and 1.0 <= item.difficulty <= 10.0
    assert item.next_due_at is not None and item.next_due_at > NOW
    assert item.last_reviewed_at == NOW

    # snapshot in the ledger matches the item state
    assert review.stability_after == item.stability
    assert review.next_due_at == item.next_due_at

    # status transition per AC2: new -> reviewing
    assert item.review_status == REVIEW_STATUS_REVIEWING

    # matches direct scheduler call for the same input
    # （0.2 卡4a 起按学科选 retention；conftest 的题 subject='math' → 0.85）
    from cairn.scheduler import Scheduler

    direct = Scheduler(desired_retention=0.85).schedule(RATING_GOOD, now=NOW)
    assert item.next_due_at == direct.due_at
    assert item.stability == direct.stability
    assert item.difficulty == direct.difficulty


def test_wrong_count_increments(session, learner, question):
    item = ErrorItem(learner_id=learner.id, question_id=question.id, wrong_count=1)
    session.add(item)
    session.commit()
    before = item.last_wrong_at

    later = NOW.replace(hour=14)
    apply_review(session, item, RATING_AGAIN, now=later)

    assert item.wrong_count == 2
    assert item.last_wrong_at == later
    assert item.last_wrong_at != before or before is None
