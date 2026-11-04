"""STRATEGY — 复习策略定义表（可插拔策略，docs/REVIEW-STRATEGIES.md）。

代码层 registry（cairn.strategies.registry）决定「怎么排期」；这张表存
「有哪些策略可选」的目录（id、展示名、参数快照），供后续界面选择与
策略参数落地用。迁移 v12 建表并种入默认行 fsrs。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow

# 与 registry.DEFAULT_STRATEGY_ID 同值：DB 目录里的默认行 id。
DEFAULT_STRATEGY_ROW_ID = "fsrs"


class Strategy(Base):
    __tablename__ = "strategy"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 策略参数快照（JSON 文本）；fsrs 参数 pin 在代码里，故可空。
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
