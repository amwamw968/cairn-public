"""REVIEW — one row per review event (append-only ledger)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow

# ratings: Again / Hard / Good / Easy -> FSRS Rating 1..4 (report §5.1)
RATING_AGAIN = "again"
RATING_HARD = "hard"
RATING_GOOD = "good"
RATING_EASY = "easy"


class Review(Base):
    __tablename__ = "review"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    error_item_id: Mapped[int] = mapped_column(ForeignKey("error_item.id"), nullable=False)
    rating: Mapped[str] = mapped_column(String(8), nullable=False)  # again/hard/good/easy
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    review_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # FSRS state snapshot after this review (audit trail)
    stability_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    difficulty_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
