"""AC2 — review_status state machine."""

from __future__ import annotations

import pytest

from cairn.models import (
    ErrorItem,
    InvalidTransition,
    REVIEW_STATUS_ARCHIVED,
    REVIEW_STATUS_MASTERED,
    REVIEW_STATUS_NEW,
    REVIEW_STATUS_REVIEWING,
)


def test_review_status_transitions(session, learner, question):
    item = ErrorItem(learner_id=learner.id, question_id=question.id)
    session.add(item)
    session.commit()
    assert item.review_status == REVIEW_STATUS_NEW

    # legal happy path: new -> reviewing -> mastered -> archived
    item.transition_to(REVIEW_STATUS_REVIEWING)
    session.commit()
    assert item.review_status == REVIEW_STATUS_REVIEWING

    item.transition_to(REVIEW_STATUS_MASTERED)
    session.commit()
    assert item.review_status == REVIEW_STATUS_MASTERED

    item.transition_to(REVIEW_STATUS_ARCHIVED)
    session.commit()
    assert item.review_status == REVIEW_STATUS_ARCHIVED


def test_mastered_rollback_is_legal(session, learner, question):
    item = ErrorItem(learner_id=learner.id, question_id=question.id)
    session.add(item)
    session.commit()
    item.transition_to(REVIEW_STATUS_REVIEWING)
    item.transition_to(REVIEW_STATUS_MASTERED)
    session.commit()

    # legal rollback: mastered -> reviewing
    item.transition_to(REVIEW_STATUS_REVIEWING)
    session.commit()
    assert item.review_status == REVIEW_STATUS_REVIEWING


def test_skip_transition_rejected_and_db_unchanged(session, learner, question):
    item = ErrorItem(learner_id=learner.id, question_id=question.id)
    session.add(item)
    session.commit()
    assert item.review_status == REVIEW_STATUS_NEW
    before = item.review_status

    # illegal skip: new -> mastered
    with pytest.raises(InvalidTransition):
        item.transition_to(REVIEW_STATUS_MASTERED)
    session.rollback()
    assert item.review_status == before

    # archived is terminal
    item.transition_to(REVIEW_STATUS_REVIEWING)
    item.transition_to(REVIEW_STATUS_MASTERED)
    item.transition_to(REVIEW_STATUS_ARCHIVED)
    session.commit()
    with pytest.raises(InvalidTransition):
        item.transition_to(REVIEW_STATUS_REVIEWING)
    session.rollback()
    assert item.review_status == REVIEW_STATUS_ARCHIVED
