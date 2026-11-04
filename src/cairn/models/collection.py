"""Owned question collections and immutable, revocable text snapshots."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow


class Collection(Base):
    __tablename__ = "learning_collection"

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learner.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    subject: Mapped[str] = mapped_column(String(32), nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    license: Mapped[str] = mapped_column(String(32), nullable=False)
    author: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(300), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CollectionItem(Base):
    __tablename__ = "learning_collection_item"
    __table_args__ = (UniqueConstraint("collection_id", "question_id", name="uq_collection_question"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("learning_collection.id"), nullable=False, index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("question.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    difficulty: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(300), nullable=False)


class CollectionVersion(Base):
    __tablename__ = "learning_collection_version"
    __table_args__ = (UniqueConstraint("collection_id", "number", name="uq_collection_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("learning_collection.id"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CollectionVersionQuestion(Base):
    __tablename__ = "learning_collection_version_question"

    version_id: Mapped[int] = mapped_column(ForeignKey("learning_collection_version.id"), primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("question.id"), primary_key=True, index=True)


class CollectionPracticeItem(Base):
    """One durable practice copy per source item in an immutable version."""

    __tablename__ = "learning_collection_practice_item"
    __table_args__ = (UniqueConstraint("version_id", "source_question_id", name="uq_collection_practice_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("learning_collection_version.id"), nullable=False, index=True)
    source_question_id: Mapped[int] = mapped_column(ForeignKey("question.id"), nullable=False, index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("question.id"), nullable=False, unique=True, index=True)
