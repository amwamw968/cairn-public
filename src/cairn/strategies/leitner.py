"""莱特纳三色盒策略（docs/REVIEW-STRATEGIES.md 第二节）。

三个盒子，间隔：盒 1 = 1 天、盒 2 = 3 天、盒 3 = 7 天。

    good / easy → 进下一盒（盒 3 保持，间隔 7 天）
    again       → 回盒 1
    hard        → 留在当前盒

状态里带 ``box`` 字段（1/2/3），便于将来映射到红/黄/绿展示。盒子不新增
任何表/列，完全由账本（review 全量记录）推断：无历史 → 盒 1；有历史 →
末尾连续 good/easy 的次数 N = 第 N+1 盒（上限 3）。hard 不进盒也不清零
连续次数（对应「留在当前盒」），again 清零。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from cairn.models.review import RATING_AGAIN, RATING_EASY, RATING_GOOD, Review
from cairn.strategies.base import Strategy

STRATEGY_ID = "leitner"

# 盒 1..3 的复习间隔（索引 = 盒号 - 1）。
BOX_INTERVALS: tuple[timedelta, ...] = (
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
)
MAX_BOX = len(BOX_INTERVALS)


def box_from_history(ratings: Iterable[str]) -> int:
    """末尾连续 good/easy 的次数 N → 第 N+1 盒（上限 MAX_BOX）。

    again 清零（对应「答错回盒 1」）；hard 不进盒也不清零。
    """
    streak = 0
    for rating in reversed(list(ratings)):
        if rating in (RATING_GOOD, RATING_EASY):
            streak += 1
        elif rating == RATING_AGAIN:
            break
    return min(streak + 1, MAX_BOX)


def _rating_of(record: Any) -> str:
    if isinstance(record, str):
        return record
    if isinstance(record, dict):
        return record["rating"]
    return record.rating


class LeitnerStrategy:
    """莱特纳三色盒：三盒轮转（1/3/7 天），答错回盒 1。"""

    id: str = STRATEGY_ID

    def describe(self) -> str:
        return (
            "莱特纳三色盒：三个盒子按 1/3/7 天轮转，答对进下一盒，"
            "答错回第 1 盒"
        )

    def initial_state(self, item: Any) -> dict:
        """从账本历史推断盒号；新对象（无历史）即盒 1。"""
        return {
            "box": box_from_history(_history_ratings(item)),
            "stability": None,
            "difficulty": None,
            "due": getattr(item, "next_due_at", None),
            "last_review": getattr(item, "last_reviewed_at", None),
        }

    def next_due(self, state: dict, record: Any, now: datetime) -> datetime:
        """下次到期时刻；*record* 是含 rating 的记录、评分字符串或映射。"""
        return self.on_grade(state, _rating_of(record), now)["due"]

    def on_grade(self, state: dict, grade: str, now: datetime) -> dict:
        box = int(state.get("box") or 1)
        if grade == RATING_AGAIN:
            box = 1
        elif grade in (RATING_GOOD, RATING_EASY):
            box = min(box + 1, MAX_BOX)
        # hard：留在当前盒
        return {
            **state,
            "box": box,
            "stability": None,
            "difficulty": None,
            "due": now + BOX_INTERVALS[box - 1],
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
