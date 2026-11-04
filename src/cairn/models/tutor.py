"""TUTOR 讲解会话两表（0.3 讲解四段式卡，迁移 v13/v14）。

TUTOR_SESSION 一次讲解一条；stage 记录**最近一次已送达的段**
（ask_thought → hint_1 → hint_2 → hint_3 → explain；recap 回讲留给下一卡）。
TUTOR_MESSAGE 存每一条消息：孩子的原话（role=child）与 tutor 的每段产出
（role=tutor）。leak_blocked=1 表示该段是 LLM 两次泄漏后被换成预置兜底文案
的那一条——泄漏本身绝不落库。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow


class TutorSession(Base):
    __tablename__ = "tutor_session"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    learner_id: Mapped[int] = mapped_column(
        ForeignKey("learner.id"), nullable=False, index=True
    )
    question_id: Mapped[int] = mapped_column(
        ForeignKey("question.id"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class TutorMessage(Base):
    __tablename__ = "tutor_message"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("tutor_session.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(8), nullable=False)  # child / tutor
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    leak_blocked: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
