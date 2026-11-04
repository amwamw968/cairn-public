"""Authoring drafts and immutable published workflow artifacts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow


class Workflow(Base):
    __tablename__ = "workflow"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    draft_json: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_version_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class WorkflowVersion(Base):
    __tablename__ = "workflow_version"
    __table_args__ = (UniqueConstraint("workflow_id", "number", name="uq_workflow_version_number"),)

    version_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow.id"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    graph_json: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_json: Mapped[str] = mapped_column(Text, nullable=False)
    published_by: Mapped[str] = mapped_column(String(64), nullable=False, default="operator")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
