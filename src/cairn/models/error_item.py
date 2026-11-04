"""ERROR_ITEM + review_status state machine.

Status machine (legal transitions only):

    new -> reviewing -> mastered -> archived
                  ^         |
                  +---------+   (mastered -> reviewing, rollback)

B3 additions (graduation / reject / restore):
    new -> archived        (pending-review reject on an unreviewed item)
    reviewing -> archived  (graduation: streak of Good/Easy + quiet days)
    archived -> mastered   (restore step 1; restore completes via the
                            pre-existing mastered -> reviewing rollback)

0.3 card 2 additions (dual-condition graduation):
    reviewing -> needs_variant  (streak + quiet days met but no correct
                                 variant review yet: cannot graduate)
    needs_variant -> archived   (a correct variant review lands later)
    needs_variant -> reviewing  (the trailing streak broke: back to the
                                 normal pipeline until it is rebuilt)

Any other transition raises InvalidTransition and leaves the DB unchanged.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow


class InvalidTransition(Exception):
    """Raised when a review_status transition is not allowed."""


REVIEW_STATUS_NEW = "new"
REVIEW_STATUS_REVIEWING = "reviewing"
REVIEW_STATUS_MASTERED = "mastered"
REVIEW_STATUS_ARCHIVED = "archived"
# 0.3 卡2：原题 streak/静默已达标但没有任何变式题答对记录 —— 缺变式卡住，不许出库
REVIEW_STATUS_NEEDS_VARIANT = "needs_variant"

LEGAL_TRANSITIONS: dict[str, set[str]] = {
    REVIEW_STATUS_NEW: {REVIEW_STATUS_REVIEWING, REVIEW_STATUS_ARCHIVED},
    REVIEW_STATUS_REVIEWING: {
        REVIEW_STATUS_MASTERED,
        REVIEW_STATUS_ARCHIVED,
        REVIEW_STATUS_NEEDS_VARIANT,
    },
    REVIEW_STATUS_MASTERED: {REVIEW_STATUS_ARCHIVED, REVIEW_STATUS_REVIEWING},
    REVIEW_STATUS_ARCHIVED: {REVIEW_STATUS_MASTERED},
    REVIEW_STATUS_NEEDS_VARIANT: {REVIEW_STATUS_ARCHIVED, REVIEW_STATUS_REVIEWING},
}


def transition_status(current: str, target: str) -> str:
    """Validate a status transition; raise InvalidTransition if illegal."""
    if target not in LEGAL_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"illegal review_status transition: {current!r} -> {target!r}")
    return target


class ErrorItem(Base):
    __tablename__ = "error_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learner.id"), nullable=False)
    question_id: Mapped[int] = mapped_column(ForeignKey("question.id"), nullable=False)
    # A workflow may pin a review strategy to this item; NULL keeps learner defaults.
    workflow_strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workflow_tutor_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    workflow_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    workflow_recap_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # error classification (manual in P0; automated attribution is P1+)
    wrong_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wrong_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_wrong_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    # review pipeline state (AC2 state machine)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default=REVIEW_STATUS_NEW)

    # gating metadata: confidence of automated entry (P0 stores, P1 gates)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # FSRS-6 state (mirrored from scheduler; see cairn.scheduler)
    stability: Mapped[float | None] = mapped_column(Float, nullable=True)
    difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # graduation bookkeeping (B3 review fix): timestamp of the most recent
    # restore from archived; graduation stays blocked until a review lands
    # after it, so a restored item cannot be re-archived by the next sweep
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    def transition_to(self, target: str) -> None:
        """In-object state machine transition (AC2). Validates first; on
        success mutates review_status. Callers commit the session."""
        self.review_status = transition_status(self.review_status, target)
