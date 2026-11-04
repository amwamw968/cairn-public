"""Backup export: one JSON document holding every table needed to restore a
single child's bank on another machine.

Shape follows the tricolor-card ``exportJson`` the roadmap asked us to reuse:
a schema version stamp + an export timestamp + the tables themselves.
One family has several children, each with their own bank, so the document is
built **per learner** — see ``build_export`` for exactly which rows travel.

Tables exported:
- entity tables: ``learner`` / ``question`` / ``knowledge_point`` /
  ``error_item`` / ``review``
- knowledge-point tables: ``concepts`` / ``concept_aliases`` (the 0.2 shape)
- the many-to-many link tables ``question_knowledge_link`` and
  ``question_concept`` (plain ``Table`` objects, not ORM classes — exported so a
  restore keeps both wirings)

``learner`` has no separate settings column today: its configurable attributes
are ``grade_level`` and the two per-subject retention targets, and every column
of the row is exported so settings travel with the backup.

``question`` rows carry answer/analysis on purpose — this is the parent's own
backup, not the practice page (which must never expose answers before reveal).

Import lives in a later card: this module only builds the document, so the web
layer stays a thin adapter and the JSON can be re-read as-is.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from cairn.migrations import SCHEMA_VERSION
from cairn.models import (
    Concept,
    ConceptAlias,
    Collection,
    CollectionItem,
    CollectionPracticeItem,
    CollectionVersion,
    CollectionVersionQuestion,
    ErrorItem,
    KnowledgePoint,
    Learner,
    Question,
    RecapAttempt,
    Review,
)
from cairn.models.concept import question_concept
from cairn.models.question import question_knowledge_link
from cairn.models.scan import Scan
from cairn.models.workflow_run import WorkflowReviewItem, WorkflowRun


class IncompleteWorkflowExport(ValueError):
    """A learning-only backup cannot safely restore an unfinished review."""

    def __init__(self, scan_id: int):
        self.scan_id = scan_id
        super().__init__("请先完成这张卷子的核对和处理，再导出学习数据。")


class InconsistentLearnerData(ValueError):
    """A partial or cross-owner backup must not be downloaded as complete."""


def _jsonable(value: Any) -> Any:
    """SQLite hands back datetimes; JSON wants strings."""
    return value.isoformat() if isinstance(value, datetime) else value


def _rows(session: Session, model: type, *criteria: Any) -> list[dict[str, Any]]:
    """Every column of every matching row, id ascending (deterministic output)."""
    columns = [column.name for column in model.__table__.columns]
    statement = select(model).order_by(model.id)
    if criteria:
        statement = statement.where(*criteria)
    return [
        {name: _jsonable(getattr(row, name)) for name in columns}
        for row in session.scalars(statement)
    ]


def _table_rows(session: Session, table: Any, *criteria: Any) -> list[dict[str, Any]]:
    """Every column of every matching row of a plain ``Table`` (no ORM class, so
    there is no ``.id`` to sort by — order by the primary-key columns for
    determinism)."""
    columns = [column.name for column in table.columns]
    order_by = list(table.primary_key.columns)
    statement = select(table).order_by(*order_by)
    if criteria:
        statement = statement.where(*criteria)
    return [
        {name: _jsonable(row._mapping[name]) for name in columns}
        for row in session.execute(statement)
    ]


def build_export(
    session: Session, learner_id: int, now: datetime | None = None
) -> dict[str, Any]:
    """One learner's backup document; *now* is injectable so tests stay stable.

    Every child has their own question bank, so the document only carries the
    rows that belong to *learner_id*: their questions, their error items, the
    reviews on those items, and the link rows for their questions. Concepts /
    aliases / knowledge points are the family-wide taxonomy — shared read-only
    content, exported whole so tags survive a restore.
    """
    # A savepoint gives all reads one SQLite snapshot, so a concurrent
    # selection cannot slip into the export after the incomplete-run check.
    with session.begin_nested():
        unfinished = session.scalar(
            select(Scan.id).join(WorkflowRun, WorkflowRun.scan_id == Scan.id)
            .join(WorkflowReviewItem, WorkflowReviewItem.run_id == WorkflowRun.id)
            .where(Scan.learner_id == learner_id, WorkflowRun.status != "completed")
            .order_by(Scan.id).limit(1)
        )
        if unfinished is not None:
            raise IncompleteWorkflowExport(unfinished)
        document = _build_export(session, learner_id, now)
        from cairn import import_service
        try:
            import_service._validate(document)
        except import_service.BackupError as exc:
            raise InconsistentLearnerData("学习数据存在归属或关联异常，暂时不能导出，请先检查数据。") from exc
        return document


def _build_export(session: Session, learner_id: int, now: datetime | None) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    question_ids = list(
        session.scalars(select(Question.id).where(Question.learner_id == learner_id))
    )
    error_item_ids = list(
        session.scalars(select(ErrorItem.id).where(ErrorItem.learner_id == learner_id))
    )
    collection_ids = list(session.scalars(select(Collection.id).where(Collection.learner_id == learner_id)))
    version_ids = list(session.scalars(select(CollectionVersion.id).where(
        CollectionVersion.collection_id.in_(collection_ids))))
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": now.isoformat(timespec="seconds"),
        "learner": _rows(session, Learner, Learner.id == learner_id),
        "question": _rows(session, Question, Question.learner_id == learner_id),
        "knowledge_point": _rows(session, KnowledgePoint),
        "question_knowledge_link": _table_rows(
            session, question_knowledge_link, question_knowledge_link.c.question_id.in_(question_ids)
        ),
        "concepts": _rows(session, Concept),
        "concept_aliases": _rows(session, ConceptAlias),
        "question_concept": _table_rows(
            session, question_concept, question_concept.c.question_id.in_(question_ids)
        ),
        "error_item": _rows(session, ErrorItem, ErrorItem.learner_id == learner_id),
        "review": _rows(session, Review, Review.error_item_id.in_(error_item_ids)),
        "recap_attempt": _rows(session, RecapAttempt, or_(RecapAttempt.learner_id == learner_id,
                               RecapAttempt.error_item_id.in_(error_item_ids))),
        "learning_collection": _rows(session, Collection, Collection.learner_id == learner_id),
        "learning_collection_item": _rows(session, CollectionItem,
                                          CollectionItem.collection_id.in_(collection_ids)),
        "learning_collection_version": _rows(session, CollectionVersion,
                                             CollectionVersion.collection_id.in_(collection_ids)),
        "learning_collection_version_question": _table_rows(
            session, CollectionVersionQuestion.__table__,
            CollectionVersionQuestion.version_id.in_(version_ids)),
        "learning_collection_practice_item": _rows(
            session, CollectionPracticeItem, CollectionPracticeItem.version_id.in_(version_ids)),
    }


def export_filename(now: datetime) -> str:
    """cairn-export-YYYYMMDD-HHMM.json (local time — the parent's clock)."""
    return f"cairn-export-{now:%Y%m%d-%H%M}.json"


def dumps(document: dict[str, Any]) -> str:
    """UTF-8 JSON with Chinese written as-is (ensure_ascii=False)."""
    return json.dumps(document, ensure_ascii=False, indent=2)
