"""EXAM — one scheduled test the learner is preparing for (0.2 卡10).

家长填「哪天考什么」；调度侧只读这一张表，不改 FSRS 状态。

- ``exam_date`` 是日历日（``Date``），不是时刻：家长说的是「下周三考试」。
- ``subject`` 为空（NULL）= 全部学科；填了就只对那一科的题做考前提前。
- 不做唯一约束：同一天可以有两场考试（数学 + 英语），谁生效由
  ``exam_service.active_exam`` 取最近一场。
- 过期不删：``exam_date`` 过去后只是「不生效」，记录留在 /exam 页面当历史。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow


class Exam(Base):
    __tablename__ = "exam"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learner.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)  # 单元考 / 期中考试
    exam_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 中文名（数学/英语/语文），与 question.subject 口径一致；NULL = 全部学科
    subject: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)  # 考试范围，家长自己看
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
