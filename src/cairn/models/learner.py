"""LEARNER — one row per student using the system."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base

# 分科目标保留率（docs/CONCEPTS-RESEARCH.md 第六节）：数学重「重做 + 变式」，
# 间隔可以更疏 → 0.85；语言类要更勤地见面 → 0.90。FSRS 仍是唯一排期权，
# 这两个值只喂 desired_retention，不引入固定间隔。
DEFAULT_RETENTION_MATH = 0.85
DEFAULT_RETENTION_OTHER = 0.90


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Learner(Base):
    __tablename__ = "learner"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    grade_level: Mapped[str] = mapped_column(String(16), nullable=False, default="primary")
    # per-subject retention: 数学 0.85（间隔更疏）、其他 0.90。server_default 让
    # 「老库 ADD COLUMN」也能给存量行补上同一个值（迁移 v5）。
    desired_retention_math: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_RETENTION_MATH, server_default="0.85"
    )
    desired_retention_other: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_RETENTION_OTHER, server_default="0.9"
    )
    # 该孩子的默认复习策略（可空；空 = 跟 CAIRN_DEFAULT_STRATEGY / fsrs，迁移 v12）。
    strategy_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("strategy.id"), nullable=True
    )
    # 每天练几题（设置页，蓝图 E.12，默认 5；迁移 v22）。
    daily_target: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
