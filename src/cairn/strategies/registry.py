"""策略注册表：按 id 取策略 + 默认策略解析。

默认策略来源（优先级从高到低）：
    1. learner.strategy_id（DB 里孩子自己选的；空 = 跟默认）
    2. 环境变量 CAIRN_DEFAULT_STRATEGY（部署级默认；未注册的值告警后回退 fsrs）
    3. 内置 "fsrs"

健壮性：learner.strategy_id 存了未注册的 id（旧数据/已下线策略）时不抛异常
——回落默认策略并记 WARNING，保证这个孩子的每次评分照常走。
"""

from __future__ import annotations

import logging
import os

from cairn.strategies.base import Strategy, UnknownStrategyError
from cairn.strategies.ebbinghaus import EbbinghausStrategy
from cairn.strategies.fsrs import FsrsStrategy
from cairn.strategies.leitner import LeitnerStrategy

logger = logging.getLogger(__name__)

ENV_DEFAULT_STRATEGY = "CAIRN_DEFAULT_STRATEGY"
DEFAULT_STRATEGY_ID = "fsrs"

_REGISTRY: dict[str, Strategy] = {}


def _register(strategy: Strategy) -> None:
    _REGISTRY[strategy.id] = strategy


_register(FsrsStrategy())
_register(EbbinghausStrategy())
_register(LeitnerStrategy())


def get_strategy(strategy_id: str) -> Strategy:
    """按 id 取策略；未知 id 抛 UnknownStrategyError（消息列出已注册 id）。"""
    strategy = _REGISTRY.get(strategy_id)
    if strategy is None:
        raise UnknownStrategyError(strategy_id, tuple(sorted(_REGISTRY)))
    return strategy


def list_strategies() -> list[dict]:
    """已注册策略目录（注册顺序）：id、展示名、给家长看的一句话说明。"""
    return [
        {"id": s.id, "name": _display_name(s), "describe": s.describe()}
        for s in _REGISTRY.values()
    ]


def _display_name(strategy: Strategy) -> str:
    """展示名 = describe 冒号前的短句（fsrs 与 v12 种子行的 name 同源）。"""
    return strategy.describe().split("：", 1)[0]


def default_strategy_id() -> str:
    """环境变量解析后的默认策略 id；未设/未注册时回退内置 fsrs。"""
    raw = (os.environ.get(ENV_DEFAULT_STRATEGY) or "").strip()
    if not raw:
        return DEFAULT_STRATEGY_ID
    if raw not in _REGISTRY:
        logger.warning(
            "%s=%r 未注册，回退默认策略 %r（已注册: %s）",
            ENV_DEFAULT_STRATEGY,
            raw,
            DEFAULT_STRATEGY_ID,
            ", ".join(sorted(_REGISTRY)),
        )
        return DEFAULT_STRATEGY_ID
    return raw


def strategy_id_for(learner: object) -> str:
    """一个孩子的生效策略 id：自己选的优先，否则走默认。

    已存储但未注册的 id（旧数据写了 nope 之类）不抛异常：记 WARNING 后
    回落默认策略，不能让这个孩子的每次评分都崩。
    """
    stored = getattr(learner, "strategy_id", None)
    if stored:
        stored = str(stored)
        if stored not in _REGISTRY:
            fallback = default_strategy_id()
            logger.warning(
                "learner.strategy_id=%r 未注册，回落默认策略 %r（已注册: %s）",
                stored,
                fallback,
                ", ".join(_REGISTRY),
            )
            return fallback
        return stored
    return default_strategy_id()
