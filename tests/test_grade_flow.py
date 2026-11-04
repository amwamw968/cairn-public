"""B3 card 1 — grading loop: one-command grade() end to end."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from cairn.entry_service import ValidationError
from cairn.models import ErrorItem
from cairn.models.review import RATING_AGAIN, RATING_GOOD, Review
from cairn.review_service import grade

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _make_item(session, learner, question) -> ErrorItem:
    item = ErrorItem(learner_id=learner.id, question_id=question.id)
    session.add(item)
    session.commit()
    return item


def test_grade_persists_review_and_scheduling(session, learner, question):
    item = _make_item(session, learner, question)

    review = grade(session, item.id, RATING_GOOD, now=NOW)

    # REVIEW ledger row: rating + timestamp + FSRS snapshot
    rows = list(session.scalars(select(Review)).all())
    assert len(rows) == 1
    assert rows[0].error_item_id == item.id
    assert rows[0].rating == RATING_GOOD
    assert rows[0].reviewed_at == NOW
    assert rows[0].stability_after is not None
    assert rows[0].difficulty_after is not None
    assert rows[0].next_due_at is not None

    # ERROR_ITEM scheduling state written back
    assert item.stability is not None and item.stability > 0
    assert item.difficulty is not None and 1.0 <= item.difficulty <= 10.0
    assert item.next_due_at is not None and item.next_due_at > NOW
    assert item.last_reviewed_at == NOW

    # status transition: new -> reviewing
    assert item.review_status == "reviewing"


def test_grade_again_counts_wrong(session, learner, question):
    item = _make_item(session, learner, question)
    assert item.wrong_count == 1

    later = NOW.replace(hour=14)
    grade(session, item.id, RATING_AGAIN, now=later)

    assert item.wrong_count == 2
    assert item.last_wrong_at == later


def test_grade_unknown_id_zero_write(session, learner, question):
    with pytest.raises(ValidationError):
        grade(session, 99999, RATING_GOOD, now=NOW)

    assert list(session.scalars(select(Review)).all()) == []


def test_grade_rejects_invalid_rating(session, learner, question):
    item = _make_item(session, learner, question)

    with pytest.raises(ValidationError):
        grade(session, item.id, "ok", now=NOW)

    assert list(session.scalars(select(Review)).all()) == []
    assert item.last_reviewed_at is None
    assert item.next_due_at is None
