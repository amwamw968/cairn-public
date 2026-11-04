"""Durable, idempotent text-recall attempts for a learning item."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow


class RecapAttempt(Base):
    __tablename__ = "recap_attempt"
    __table_args__ = (UniqueConstraint("learner_id", "request_key", name="uq_recap_request"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learner.id"), nullable=False, index=True)
    error_item_id: Mapped[int] = mapped_column(ForeignKey("error_item.id"), nullable=False, index=True)
    request_key: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    followup_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    assessment: Mapped[str] = mapped_column(String(24), nullable=False)
    focus: Mapped[str | None] = mapped_column(String(24), nullable=True)
    reference_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_id: Mapped[int | None] = mapped_column(ForeignKey("review.id"), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
