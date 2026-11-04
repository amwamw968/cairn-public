"""QUESTION + KNOWLEDGE_POINT tables.

QUESTION is the canonical question (from paper-cutting later; manual entry
now). KNOWLEDGE_POINT supports self-referential prerequisite links —
prerequisite wiring is data, applied in P1+.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Table, Column, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cairn.models.base import Base
from cairn.models.learner import utcnow


class Question(Base):
    __tablename__ = "question"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 题目归属：一个家庭里每个孩子一套独立题库（查重、同类题、导出、统计都按它过滤）。
    # 教材（book/unit/word）与知识点不隔离，仍是全局只读内容。
    learner_id: Mapped[int] = mapped_column(
        ForeignKey("learner.id"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(32), nullable=False)  # 中文名：数学/英语/语文
    grade_level: Mapped[str] = mapped_column(String(16), nullable=False, default="primary")
    question_type: Mapped[str] = mapped_column(String(32), nullable=False)  # choice/fill/apply...
    content: Mapped[str] = mapped_column(Text, nullable=False)  # stem text
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)  # canonical answer
    analysis: Mapped[str | None] = mapped_column(Text, nullable=True)  # solution walk-through
    # dictation cue (0.2 卡3): what gets read aloud — the English word itself or
    # the pinyin of a Chinese word. Never the answer.
    audio_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    lang: Mapped[str | None] = mapped_column(String(8), nullable=True)  # en / zh
    # 变式题指针（0.3 卡2 出库双条件）：指向同知识点的原题 id；原题本身为 NULL。
    # 出库判定要求原题至少有一条 variant_of 指向它的变式题被答对。
    variant_of: Mapped[int | None] = mapped_column(ForeignKey("question.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # 人工编辑痕迹（T3-a）：最近一次改题干/答案/解析的时间与来源。
    # 存 ISO8601 字符串（含 +08:00，页面上直接显示，不做二次换算）；
    # 没编辑过 = NULL。本卡只写 'human'，'model' 留给接模型之后的批量改。
    edited_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    knowledge_points: Mapped[list["KnowledgePoint"]] = relationship(
        secondary="question_knowledge_link", back_populates="questions"
    )


class KnowledgePoint(Base):
    __tablename__ = "knowledge_point"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    subject: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("knowledge_point.id"), nullable=True)

    children: Mapped[list["KnowledgePoint"]] = relationship(back_populates="parent")
    parent: Mapped["KnowledgePoint | None"] = relationship(back_populates="children", remote_side=[id])
    questions: Mapped[list[Question]] = relationship(
        secondary="question_knowledge_link", back_populates="knowledge_points"
    )


# many-to-many link between questions and knowledge points
question_knowledge_link = Table(
    "question_knowledge_link",
    Base.metadata,
    Column("question_id", ForeignKey("question.id"), primary_key=True),
    Column("knowledge_point_id", ForeignKey("knowledge_point.id"), primary_key=True),
)
