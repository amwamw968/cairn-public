"""Due-queue queries: what should be reviewed right now."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.models.error_item import ErrorItem


def due_items(session: Session, now: datetime, learner_id: int | None = None, limit: int | None = None) -> list[ErrorItem]:
    """Return ERROR_ITEMs due for review at *now*.

    Filter : next_due_at <= now (and optional learner_id)
    Order  : next_due_at ascending (most overdue first)
    limit  : optional cap on returned rows
    """
    from cairn.models.error_item import REVIEW_STATUS_ARCHIVED, REVIEW_STATUS_NEW
    from cairn.workflow.review import ready_for_practice

    stmt = select(ErrorItem).where(
        ErrorItem.next_due_at.is_not(None),
        ErrorItem.next_due_at <= now,
        ErrorItem.review_status != REVIEW_STATUS_ARCHIVED,
        ErrorItem.review_status != REVIEW_STATUS_NEW,
        ready_for_practice(ErrorItem.question_id),
    )
    if learner_id is not None:
        stmt = stmt.where(ErrorItem.learner_id == learner_id)
    stmt = stmt.order_by(ErrorItem.next_due_at.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())
