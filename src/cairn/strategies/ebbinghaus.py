"""艾宾浩斯固定曲线策略（docs/REVIEW-STRATEGIES.md 第二节）。

7 档固定间隔：1h / 1d / 2d / 4d / 7d / 15d / 30d（第 1 档是 1 小时）。

    good / easy → 进一档（第 7 档封顶，之后保持）
    again       → 回第 1 档
    hard        → 留在当前档，下次间隔仍按当前档算

档位不新增任何表/列，完全由账本（review 全量记录）推断：无历史 → 第 1 档；
有历史 → 末尾连续 good/easy 的次数 N = 第 N+1 档（上限 7）。hard 不进档也
不清零连续次数（对应「留在当前档」），again 清零。换策略时账本保留、状态
按历史重建，与 REVIEW-STRATEGIES.md 的「策略可换」契约一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from cairn.models.review import RATING_AGAIN, RATING_EASY, RATING_GOOD, Review
from cairn.strategies.base import Strategy

STRATEGY_ID = "ebbinghaus"

# 第 1..7 档的复习间隔（索引 = 档位 - 1）；第 1 档是 1 小时。
TIER_INTERVALS: tuple[timedelta, ...] = (
    timedelta(hours=1),
    timedelta(days=1),
    timedelta(days=2),
    timedelta(days=4),
    timedelta(days=7),
    timedelta(days=15),
    timedelta(days=30),
)
MAX_TIER = len(TIER_INTERVALS)


def tier_from_history(ratings: Iterable[str]) -> int:
    """末尾连续 good/easy 的次数 N → 第 N+1 档（上限 MAX_TIER）。

    again 清零（对应「答错回第 1 档」）；hard 不进档也不清零。
    """
    streak = 0
    for rating in reversed(list(ratings)):
        if rating in (RATING_GOOD, RATING_EASY):
            streak += 1
        elif rating == RATING_AGAIN:
            break
    return min(streak + 1, MAX_TIER)


def _rating_of(record: Any) -> str:
    if isinstance(record, str):
        return record
    if isinstance(record, dict):
        return record["rating"]
    return record.rating


class EbbinghausStrategy:
    """艾宾浩斯固定曲线：7 档，答错回第 1 档。"""

    id: str = STRATEGY_ID

    def describe(self) -> str:
        return (
            "艾宾浩斯固定曲线：固定间隔 1小时/1天/2天/4天/7天/15天/30天，"
            "答对进一档，答错回第 1 档"
        )

    def initial_state(self, item: Any) -> dict:
        """从账本历史推断档位；新对象（无历史）即第 1 档。

        FSRS 的 stability/difficulty 对本策略无意义，恒为 None。
        """
        return {
            "tier": tier_from_history(_history_ratings(item)),
            "stability": None,
            "difficulty": None,
            "due": getattr(item, "next_due_at", None),
            "last_review": getattr(item, "last_reviewed_at", None),
        }

    def next_due(self, state: dict, record: Any, now: datetime) -> datetime:
        """下次到期时刻；*record* 是含 rating 的记录、评分字符串或映射。"""
        return self.on_grade(state, _rating_of(record), now)["due"]

    def on_grade(self, state: dict, grade: str, now: datetime) -> dict:
        tier = int(state.get("tier") or 1)
        if grade == RATING_AGAIN:
            tier = 1
        elif grade in (RATING_GOOD, RATING_EASY):
            tier = min(tier + 1, MAX_TIER)
        # hard：留在当前档
        return {
            **state,
            "tier": tier,
            "stability": None,
            "difficulty": None,
            "due": now + TIER_INTERVALS[tier - 1],
            "last_review": now,
        }


def _history_ratings(item: Any) -> list[str]:
    """账本里该对象的评分序列（旧→新）；非 ORM 对象（纯单元测试）视为无历史。"""
    state = sa_inspect(item, None)
    session = state.session if state is not None else None
    item_id = getattr(item, "id", None)
    if session is None or item_id is None:
        return []
    return list(
        session.scalars(
            select(Review.rating)
            .where(Review.error_item_id == item_id)
            .order_by(Review.reviewed_at.asc(), Review.id.asc())
        ).all()
    )
