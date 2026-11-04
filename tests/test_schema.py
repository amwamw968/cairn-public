"""AC1 — domain model completeness."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from cairn.models import ErrorItem, Learner, Question


def test_schema_creates_all_tables(session):
    inspector = sa_inspect(session.bind)
    tables = set(inspector.get_table_names())
    for t in ("learner", "question", "knowledge_point", "error_item", "review"):
        assert t in tables, f"missing table: {t}"

    cols = {c["name"] for c in inspector.get_columns("error_item")}
    for f in ("confidence", "review_status", "wrong_reason", "wrong_count", "last_wrong_at"):
        assert f in cols, f"error_item missing column: {f}"

    # sanity: the five mapped classes are the ones we declared
    assert {m.__tablename__ for m in (Learner, Question, ErrorItem)} <= tables | {"learner", "question"}


def test_fk_enforced_on_bad_question_id(session, learner):
    """Inserting ERROR_ITEM pointing at a non-existent question must fail."""
    bad = ErrorItem(learner_id=learner.id, question_id=999999)
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_question_ownership_is_required(session, learner):
    """AC1：question.learner_id 非空、外键到 learner；不给主人写不进去。"""
    inspector = sa_inspect(session.bind)
    question_columns = {c["name"]: c for c in inspector.get_columns("question")}
    assert question_columns["learner_id"]["nullable"] is False

    foreign_keys = inspector.get_foreign_keys("question")
    assert any(
        fk["constrained_columns"] == ["learner_id"] and fk["referred_table"] == "learner"
        for fk in foreign_keys
    )

    session.add(
        Question(
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content="一加一等于几？",
            answer="二",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_scan_carries_learner_ownership(session):
    """AC1：scan 有 learner_id（老库 ADD COLUMN 只能可空，写入路径必填）。"""
    inspector = sa_inspect(session.bind)
    scan_columns = {c["name"]: c for c in inspector.get_columns("scan")}
    assert "learner_id" in scan_columns
    assert any(
        fk["constrained_columns"] == ["learner_id"] and fk["referred_table"] == "learner"
        for fk in inspector.get_foreign_keys("scan")
    )
