"""Persistent execution state for a scan's pinned workflow version."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow


class WorkflowRun(Base):
    __tablename__ = "workflow_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan.id"), nullable=False, unique=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("workflow_version.version_id"), nullable=False)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class WorkflowRunStep(Base):
    __tablename__ = "workflow_run_step"
    __table_args__ = (UniqueConstraint("run_id", "step_index", name="uq_workflow_run_step"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_run.id"), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowReviewItem(Base):
    __tablename__ = "workflow_review_item"
    __table_args__ = (UniqueConstraint("run_id", "question_id", name="uq_workflow_review_question"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_run.id"), nullable=False, index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("question.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
