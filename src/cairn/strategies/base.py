"""Strategy 协议（唯一契约，docs/REVIEW-STRATEGIES.md 第二节）。

评分统一为四档 again/hard/good/easy（与现有 FSRS 评分一致），策略自己映射
到内部等级。换策略不改账本（REVIEW 全量记录），只换实现。

state 是策略自己的字典（FSRS 的 DSR 只是其中一种实现）：

    stability / difficulty / due / last_review   FSRS 恢复卡片所需的四元组
    subject / learner（可选）                    调用方补充的上下文，
                                                FsrsStrategy 用它选目标记忆率

``initial_state`` 从一个复习对象读出初始 state；调用方再把 subject/learner
拼进去（对象本身不带这两样），``on_grade`` 返回的新 state 会原样保留它们，
所以可以直接链式喂回下一步。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class UnknownStrategyError(LookupError):
    """registry.get_strategy 收到未注册的策略 id 时抛出。"""

    def __init__(self, strategy_id: str, known: tuple[str, ...] = ()) -> None:
        self.strategy_id = strategy_id
        self.known = known
        message = f"未知复习策略: {strategy_id!r}"
        if known:
            message += f"（已注册: {', '.join(known)}）"
        super().__init__(message)


@runtime_checkable
class Strategy(Protocol):
    """一套复习方法的契约：状态怎么初始化、评分后怎么变、下次何时练。"""

    id: str

    def initial_state(self, item: Any) -> dict:
        """从一个复习对象读出该策略的初始状态（新对象 → 各键为 None）。"""
        ...

    def next_due(self, state: dict, record: Any, now: datetime) -> datetime:
        """按 *record*（含 rating 的复习记录或评分字符串）算下次到期时刻。"""
        ...

    def on_grade(self, state: dict, grade: str, now: datetime) -> dict:
        """评分后状态怎么变：返回新 state（含 stability/difficulty/due/last_review）。"""
        ...

    def describe(self) -> str:
        """给家长看的一句话说明。"""
        ...
