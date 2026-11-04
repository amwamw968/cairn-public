"""B3 card 2 — pending-review queue: doubtful unreviewed items, confirm/reject."""

from __future__ import annotations

from datetime import datetime, timezone

from cairn.entry_service import EntryPayload, create_error_item
from cairn.graduation_service import (
    confirm_review,
    pending_review_queue,
    reject_review,
)
from cairn.practice_service import fetch_practice
from cairn.review_service import grade

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

PAYLOAD = EntryPayload(
    subject="math",
    question_type="fill",
    content="12 + 15 = ?",
    answer="27",
    analysis="12+15=27",
)


class _StubVerifier:
    """Entry verifier stub returning a fixed confidence."""

    def __init__(self, confidence: float) -> None:
        self.confidence = confidence

    def verify(self, payload: EntryPayload) -> float:
        return self.confidence


def _doubtful_item(session, learner):
    return create_error_item(
        session,
        learner.id,
        PAYLOAD,
        verify_entry=True,
        entry_verifier=_StubVerifier(0.5),
    )


def _queue_ids(session, learner):
    return [i.id for i in pending_review_queue(session, learner.id)]


def test_low_confidence_enters_pending_queue(session, learner):
    doubtful = _doubtful_item(session, learner)
    assert doubtful.confidence == 0.5

    manual = create_error_item(session, learner.id, PAYLOAD)
    assert manual.confidence == 1.0

    ids = _queue_ids(session, learner)
    assert doubtful.id in ids
    assert manual.id not in ids


def test_confirm_sets_confidence_one(session, learner):
    item = _doubtful_item(session, learner)
    assert item.id in _queue_ids(session, learner)

    confirmed = confirm_review(session, item.id)

    assert confirmed.confidence == 1.0
    assert item.id not in _queue_ids(session, learner)


def test_reject_archives_item(session, learner):
    item = _doubtful_item(session, learner)

    rejected = reject_review(session, item.id)

    assert rejected.review_status == "archived"
    assert item.id not in _queue_ids(session, learner)

    # archived stays out of the daily queue too
    batch = fetch_practice(session, learner.id, now=NOW, limit=10)
    assert item.id not in [p.error_item_id for p in batch]


def test_reviewed_item_not_in_queue(session, learner):
    item = _doubtful_item(session, learner)
    assert item.id in _queue_ids(session, learner)

    # one review means it is no longer "not yet reviewed"
    grade(session, item.id, "good", now=NOW)

    assert item.id not in _queue_ids(session, learner)
