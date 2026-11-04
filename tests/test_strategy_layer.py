"""策略层等价重构的证明（复习方法策略层卡）。

旧路径 = ``cairn.scheduler.Scheduler.schedule``（重构前 apply_review 的调用
方式）；新路径 = ``registry.get_strategy("fsrs")`` 的 ``on_grade`` /
``next_due``。同一输入必须给出逐位相等的结果（datetime、float 全等），
到期时间相等则由 next_due_at 派生的出题顺序必然相等（顺序只由 due 与
难度分决定，难度分逻辑本卡未动）。

五组输入：新题、答错、答对、连续答对（三连）、长间隔（60 天前复习过）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cairn.models.review import RATING_AGAIN, RATING_EASY, RATING_GOOD
from cairn.scheduler import Scheduler
from cairn.strategies.base import UnknownStrategyError
from cairn.strategies.fsrs import FsrsStrategy
from cairn.strategies.registry import (
    DEFAULT_STRATEGY_ID,
    ENV_DEFAULT_STRATEGY,
    default_strategy_id,
    get_strategy,
    strategy_id_for,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
LONG_AGO = NOW - timedelta(days=60)

SCENARIOS = ("new_good", "wrong_again", "good_again", "streak_good3", "long_interval_easy")


def _learner(retention: float = 0.9) -> SimpleNamespace:
    """apply_review 传给 scheduler_for 的 learner 形状（分科保留率 + 策略）。"""
    return SimpleNamespace(
        desired_retention_math=retention,
        desired_retention_other=retention,
        strategy_id=None,
    )


def _prior_state() -> dict:
    """一次 good 评分后的真实 FSRS 状态（老路径产出，作为共享输入）。"""
    r = Scheduler().schedule(RATING_GOOD, now=NOW)
    return {
        "stability": r.stability,
        "difficulty": r.difficulty,
        "due": r.due_at,
        "last_review": NOW,
    }


def _old_path(rating: str, state: dict, now: datetime, retention: float = 0.9):
    """重构前 apply_review 的调用方式：直接 schedule。"""
    return Scheduler(desired_retention=retention).schedule(
        rating,
        stability=state.get("stability"),
        difficulty=state.get("difficulty"),
        due=state.get("due"),
        last_review=state.get("last_review"),
        now=now,
    )


def _new_path(rating: str, state: dict, now: datetime, retention: float = 0.9):
    """重构后 apply_review 的调用方式：registry → on_grade。"""
    strategy = get_strategy("fsrs")
    full_state = {**state, "subject": "数学", "learner": _learner(retention)}
    return strategy.on_grade(full_state, rating, now)


# ---- AC2：五组等价对比 ----


def test_equiv_new_item():
    """组1 新题：无历史状态，good。"""
    empty = {}
    old = _old_path(RATING_GOOD, empty, NOW)
    new = _new_path(RATING_GOOD, empty, NOW)
    assert new["due"] == old.due_at
    assert new["stability"] == old.stability
    assert new["difficulty"] == old.difficulty


def test_equiv_wrong_answer():
    """组2 答错：有历史状态，again。"""
    prior = _prior_state()
    old = _old_path(RATING_AGAIN, prior, NOW)
    new = _new_path(RATING_AGAIN, prior, NOW)
    assert new["due"] == old.due_at
    assert new["stability"] == old.stability
    assert new["difficulty"] == old.difficulty


def test_equiv_correct_answer():
    """组3 答对：有历史状态，good。"""
    prior = _prior_state()
    old = _old_path(RATING_GOOD, prior, NOW)
    new = _new_path(RATING_GOOD, prior, NOW)
    assert new["due"] == old.due_at
    assert new["stability"] == old.stability
    assert new["difficulty"] == old.difficulty


def test_equiv_consecutive_correct():
    """组4 连续答对：good×3 链式排期，逐步逐位一致。"""
    old_state: dict | None = None
    new_state: dict | None = None
    for step, at in enumerate((NOW, NOW + timedelta(hours=1), NOW + timedelta(days=1)), 1):
        old = _old_path(RATING_GOOD, old_state or {}, at)
        new = _new_path(RATING_GOOD, new_state or {}, at)
        assert new["due"] == old.due_at, f"step {step} due"
        assert new["stability"] == old.stability, f"step {step} stability"
        assert new["difficulty"] == old.difficulty, f"step {step} difficulty"
        old_state = {
            "stability": old.stability,
            "difficulty": old.difficulty,
            "due": old.due_at,
            "last_review": at,
        }
        new_state = new


def test_equiv_long_interval():
    """组5 长间隔：60 天前复习过，easy。"""
    prior = {**_prior_state(), "due": LONG_AGO, "last_review": LONG_AGO}
    old = _old_path(RATING_EASY, prior, NOW)
    new = _new_path(RATING_EASY, prior, NOW)
    assert new["due"] == old.due_at
    assert new["stability"] == old.stability
    assert new["difficulty"] == old.difficulty


def test_equiv_order_unchanged():
    """出题顺序等价：五组的 due 逐条相等 → 按 due 排序的出题顺序完全一致。"""
    prior = _prior_state()
    long_prior = {**prior, "due": LONG_AGO, "last_review": LONG_AGO}
    cases = [
        ("new_good", RATING_GOOD, {}, NOW),
        ("wrong_again", RATING_AGAIN, prior, NOW),
        ("good_again", RATING_GOOD, prior, NOW),
        ("long_interval_easy", RATING_EASY, long_prior, NOW),
    ]
    old_dues, new_dues = [], []
    for name, rating, state, at in cases:
        old_dues.append((name, _old_path(rating, state, at).due_at))
        new_dues.append((name, _new_path(rating, state, at)["due"]))
    assert new_dues == old_dues
    old_order = [n for n, _ in sorted(old_dues, key=lambda x: x[1])]
    new_order = [n for n, _ in sorted(new_dues, key=lambda x: x[1])]
    assert old_order == new_order


def test_equiv_next_due_matches_on_grade():
    """next_due 协议方法与 on_grade 的 due 一致，且接受 Review 形状的对象。"""
    strategy = get_strategy("fsrs")
    prior = _prior_state()
    record = SimpleNamespace(rating=RATING_GOOD)
    assert strategy.next_due(prior, record, NOW) == _new_path(RATING_GOOD, prior, NOW)["due"]


# ---- AC3：registry ----


def test_get_strategy_fsrs():
    strategy = get_strategy("fsrs")
    assert isinstance(strategy, FsrsStrategy)
    assert strategy.id == "fsrs"
    assert strategy.describe()


def test_get_strategy_unknown_raises():
    with pytest.raises(UnknownStrategyError) as excinfo:
        get_strategy("nope")
    assert "nope" in str(excinfo.value)
    assert "fsrs" in str(excinfo.value)  # 消息列出已注册 id


def test_strategy_id_for_prefers_learner_choice():
    # 本卡起：已存储但未注册的 id 回落默认（带 WARNING），所以这里的自选值
    # 必须是已注册 id；断言意图不变 —— 自己选的优先于默认。
    assert strategy_id_for(SimpleNamespace(strategy_id="ebbinghaus")) == "ebbinghaus"


def test_strategy_id_for_default(monkeypatch):
    monkeypatch.delenv(ENV_DEFAULT_STRATEGY, raising=False)
    assert strategy_id_for(None) == DEFAULT_STRATEGY_ID == "fsrs"


def test_default_strategy_env_override(monkeypatch):
    monkeypatch.setenv(ENV_DEFAULT_STRATEGY, "fsrs")
    assert default_strategy_id() == "fsrs"


def test_default_strategy_env_unknown_falls_back(monkeypatch):
    monkeypatch.setenv(ENV_DEFAULT_STRATEGY, "bogus")
    assert default_strategy_id() == "fsrs"
