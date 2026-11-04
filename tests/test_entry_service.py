"""Batch 2 AC1–AC4, AC9–AC11 — manual entry service."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cairn.entry_service import EntryPayload, ValidationError, create_error_item
from cairn.models import ErrorItem, Question

PAYLOAD = EntryPayload(
    subject="math",
    question_type="fill",
    content="12 + 15 = ?",
    answer="27",
    analysis="12+15=27",
)


class RecordingVerifier:
    """Stub verifier: records the payload it saw, returns a fixed score."""

    def __init__(self, score: float = 0.4):
        self.score = score
        self.calls: list[EntryPayload] = []

    def verify(self, payload: EntryPayload) -> float:
        self.calls.append(payload)
        return self.score


def _counts(session) -> tuple[int, int]:
    return (
        len(session.query(Question).all()),
        len(session.query(ErrorItem).all()),
    )


# ---- AC1 落库与初值 ----

def test_create_error_item_persists_question_and_error_item(session, learner):
    item = create_error_item(session, learner.id, PAYLOAD)

    questions = session.query(Question).all()
    assert len(questions) == 1
    q = questions[0]
    assert (q.subject, q.question_type, q.content, q.answer) == ("math", "fill", "12 + 15 = ?", "27")
    assert q.source == "manual"

    items = session.query(ErrorItem).all()
    assert len(items) == 1
    assert item is items[0]
    assert item.question_id == q.id
    assert item.wrong_count == 1
    assert item.review_status == "new"
    assert item.confidence == 1.0
    assert item.last_wrong_at is not None


# ---- AC2 必填校验 ----

@pytest.mark.parametrize("field", ["subject", "question_type", "content", "answer"])
def test_missing_required_field_rejected(session, learner, field):
    payload = replace(PAYLOAD, **{field: ""})
    with pytest.raises(ValidationError, match=field):
        create_error_item(session, learner.id, payload)
    assert _counts(session) == (0, 0)

    with pytest.raises(ValidationError, match="learner_id"):
        create_error_item(session, None, PAYLOAD)
    assert _counts(session) == (0, 0)


# ---- AC3 外键防护 ----

def test_nonexistent_learner_rejected(session):
    with pytest.raises(ValidationError, match="learner"):
        create_error_item(session, 999, PAYLOAD)
    assert _counts(session) == (0, 0)


# ---- AC4 录入渠道初值 ----

def test_manual_confidence_is_one(session, learner):
    item = create_error_item(session, learner.id, PAYLOAD)
    assert item.confidence == 1.0


# ---- AC9 开关默认关闭、行为不变 ----

def test_verify_entry_closed_by_default(session, learner):
    sentinel = RecordingVerifier(score=0.4)

    # default: switch not passed at all
    a = create_error_item(session, learner.id, PAYLOAD, entry_verifier=sentinel)
    # explicit off
    b = create_error_item(session, learner.id, PAYLOAD, verify_entry=False, entry_verifier=sentinel)

    assert a.confidence == 1.0
    assert b.confidence == 1.0
    assert sentinel.calls == [], "verifier must never be called while the switch is off"


# ---- AC10 开启走校验路径 ----

def test_verify_entry_open_stores_verifier_confidence(session, learner):
    verifier = RecordingVerifier(score=0.4)

    item = create_error_item(
        session, learner.id, PAYLOAD, verify_entry=True, entry_verifier=verifier
    )

    assert len(verifier.calls) == 1
    seen = verifier.calls[0]
    assert (seen.subject, seen.question_type, seen.content, seen.answer, seen.analysis) == (
        "math", "fill", "12 + 15 = ?", "27", "12+15=27",
    )
    assert item.confidence == 0.4
    # doubtful verdict still persists (gating is P1)
    assert item.review_status == "new"
    assert item.wrong_count == 1


# ---- AC11 开启必须有校验器 ----

def test_verify_entry_open_requires_verifier(session, learner):
    with pytest.raises(ValidationError, match="entry_verifier"):
        create_error_item(session, learner.id, PAYLOAD, verify_entry=True)
    assert _counts(session) == (0, 0)
