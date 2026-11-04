"""FSRS 策略：把现有 FSRS 调用搬进策略层，参数一个不改。

本实现不自带第二套排期算法——它委托 ``cairn.scheduler``（pinned
fsrs==6.3.2 的薄包装：learning/relearning steps 关、fuzz 关、确定性输出），
目标记忆率也仍由 ``scheduler_for(subject, learner)`` 按学科取
（数学 learner.desired_retention_math 默认 0.85，其余 0.90）。
等价性由 tests/test_strategy_layer.py 逐位证明。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from cairn.scheduler import scheduler_for
from cairn.strategies.base import Strategy  # noqa: F401  (re-export 便于 import)

_STRATEGY_ID = "fsrs"


class FsrsStrategy:
    """FSRS-6 自适应间隔（默认策略）。

    state 约定见 strategies.base 模块注释；``on_grade`` 返回的新 state
    保留 subject/learner 上下文，可直接链式喂回。
    """

    id: str = _STRATEGY_ID

    def describe(self) -> str:
        return (
            "FSRS-6 自适应间隔：按每次评分动态算稳定性/难度，自动拉长间隔"
            "（目标记忆率：默认 0.90，数学 0.85）"
        )

    def initial_state(self, item: Any) -> dict:
        """从复习对象快照 FSRS 四元组；新对象（各字段为 None）即新卡。"""
        return {
            "stability": getattr(item, "stability", None),
            "difficulty": getattr(item, "difficulty", None),
            "due": getattr(item, "next_due_at", None),
            "last_review": getattr(item, "last_reviewed_at", None),
        }

    def next_due(self, state: dict, record: Any, now: datetime) -> datetime:
        """下次到期时刻。*record* 是含 ``rating`` 的复习记录（如 Review 行）、
        评分字符串或 ``{"rating": ...}`` 映射。"""
        return self.on_grade(state, _rating_of(record), now)["due"]

    def on_grade(self, state: dict, grade: str, now: datetime) -> dict:
        """评分 → FSRS 排期 → 新 state。调用路径与重构前 apply_review 完全
        相同：scheduler_for(subject, learner).schedule(rating, S/D/due/
        last_review, now)，参数一个不改。"""
        result = scheduler_for(state.get("subject"), state.get("learner")).schedule(
            grade,
            stability=state.get("stability"),
            difficulty=state.get("difficulty"),
            due=state.get("due"),
            last_review=state.get("last_review"),
            now=now,
        )
        return {
            **state,
            "stability": result.stability,
            "difficulty": result.difficulty,
            "due": result.due_at,
            "last_review": now,
        }


def _rating_of(record: Any) -> str:
    if isinstance(record, str):
        return record
    if isinstance(record, dict):
        return record["rating"]
    return record.rating
