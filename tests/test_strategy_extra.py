"""艾宾浩斯 + 莱特纳 + 策略选择（本卡验收用例）。

到期时间全部用假时钟手算断言（不依赖真实时间）；账本推断路径用真实 DB
验证（apply_review 每次都从 Review 行重建 state，档位/盒号从不落库）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn.models import ErrorItem, Learner, Question, Strategy
from cairn.models.review import RATING_AGAIN, RATING_EASY, RATING_GOOD, RATING_HARD
from cairn.review_service import apply_review
from cairn.strategies.ebbinghaus import TIER_INTERVALS
from cairn.strategies.leitner import BOX_INTERVALS
from cairn.strategies.registry import (
    DEFAULT_STRATEGY_ID,
    get_strategy,
    list_strategies,
    strategy_id_for,
)
from cairn.web import create_app

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
D = timedelta(days=1)


def _seed_catalog(session) -> None:
    """给 conftest 的 session fixture 补注册策略的目录行（init_db 只种 fsrs）。

    末尾必须 flush：Learner↔Strategy 没有 relationship，UOW 不保证先插
    strategy 行再更新 learner.strategy_id，同一事务里 FK 会按落库顺序查。
    """
    for entry in list_strategies():
        if session.get(Strategy, entry["id"]) is None:
            session.add(Strategy(id=entry["id"], name=entry["name"]))
    session.flush()


# ---- 艾宾浩斯：7 档，间隔手算 ----


def test_ebbinghaus_new_item_first_tier_one_hour():
    """新题无历史 → 第 1 档；第 1 档间隔 1 小时（hard 留档即按 1 小时排）。"""
    s = get_strategy("ebbinghaus")
    state = s.initial_state(SimpleNamespace())
    assert state["tier"] == 1
    assert TIER_INTERVALS[0] == H
    assert s.next_due(state, RATING_HARD, NOW) == NOW + H


def test_ebbinghaus_good_once_one_day():
    s = get_strategy("ebbinghaus")
    state = s.on_grade({"tier": 1}, RATING_GOOD, NOW)
    assert state["tier"] == 2
    assert state["due"] == NOW + D


def test_ebbinghaus_easy_advances_like_good():
    s = get_strategy("ebbinghaus")
    state = s.on_grade({"tier": 1}, RATING_EASY, NOW)
    assert state["tier"] == 2
    assert state["due"] == NOW + D


def test_ebbinghaus_hard_stays_current_tier():
    """hard → 留在当前档，下次间隔按当前档算（第 3 档 = 2 天）。"""
    s = get_strategy("ebbinghaus")
    state = s.on_grade({"tier": 3}, RATING_HARD, NOW)
    assert state["tier"] == 3
    assert state["due"] == NOW + timedelta(days=2)


def test_ebbinghaus_three_goods_four_days_then_again_one_hour():
    """连续 3 次答对 → 第 4 档（间隔 4 天）；答错 → 回第 1 档（1 小时）。"""
    s = get_strategy("ebbinghaus")
    state = {"tier": 1}
    # 三次评分用固定假时钟：NOW、NOW+1d、NOW+2d
    for i in range(3):
        at = NOW + timedelta(days=i)
        state = s.on_grade(state, RATING_GOOD, at)
        assert state["tier"] == i + 2
    assert state["due"] == NOW + timedelta(days=2) + timedelta(days=4)  # 第 4 档 = 4 天
    at = state["due"]
    state = s.on_grade(state, RATING_AGAIN, at)
    assert state["tier"] == 1
    assert state["due"] == at + H


def test_ebbinghaus_eighth_good_caps_thirty_days():
    """连续 7 次答对到第 7 档；第 8 次答对封顶，仍是 30 天。"""
    s = get_strategy("ebbinghaus")
    state = {"tier": 1}
    for i in range(7):
        state = s.on_grade(state, RATING_GOOD, NOW + timedelta(days=i))
    assert state["tier"] == 7
    state = s.on_grade(state, RATING_GOOD, NOW + timedelta(days=7))
    assert state["tier"] == 7
    assert state["due"] == NOW + timedelta(days=7) + timedelta(days=30)


def test_ebbinghaus_infers_tier_from_ledger(session, learner, question):
    """有历史 → 连续答对次数推档；档位从不落库，每次评分都从账本重建。"""
    item = ErrorItem(learner_id=learner.id, question_id=question.id)
    session.add(item)
    _seed_catalog(session)
    learner.strategy_id = "ebbinghaus"
    session.commit()

    apply_review(session, item, RATING_GOOD, now=NOW)
    assert item.next_due_at == NOW + D  # 无历史 → 第 1 档，good → 第 2 档
    apply_review(session, item, RATING_GOOD, now=NOW + D)
    assert item.next_due_at == NOW + D + timedelta(days=2)  # 连续 2 次 → 第 3 档
    apply_review(session, item, RATING_GOOD, now=NOW + 3 * D)
    assert item.next_due_at == NOW + 3 * D + timedelta(days=4)  # 连续 3 次 → 第 4 档
    apply_review(session, item, RATING_AGAIN, now=NOW + 7 * D)
    assert item.next_due_at == NOW + 7 * D + H  # 答错 → 回第 1 档


# ---- 莱特纳：3 盒，间隔手算 ----


def test_leitner_new_item_box_one():
    """新题 → 盒 1 / 1 天。"""
    s = get_strategy("leitner")
    state = s.initial_state(SimpleNamespace())
    assert state["box"] == 1
    assert BOX_INTERVALS[0] == D
    assert s.next_due(state, RATING_HARD, NOW) == NOW + D


def test_leitner_progression_and_reset():
    """答对 → 盒 2/3 天 → 盒 3/7 天；盒 3 再答对仍 7 天；答错 → 盒 1/1 天。"""
    s = get_strategy("leitner")
    state = s.on_grade({"box": 1}, RATING_GOOD, NOW)
    assert state["box"] == 2
    assert state["due"] == NOW + timedelta(days=3)
    state = s.on_grade(state, RATING_GOOD, NOW)
    assert state["box"] == 3
    assert state["due"] == NOW + timedelta(days=7)
    state = s.on_grade(state, RATING_GOOD, NOW)
    assert state["box"] == 3
    assert state["due"] == NOW + timedelta(days=7)
    state = s.on_grade(state, RATING_AGAIN, NOW)
    assert state["box"] == 1
    assert state["due"] == NOW + D


def test_leitner_hard_stays_current_box():
    s = get_strategy("leitner")
    state = s.on_grade({"box": 2}, RATING_HARD, NOW)
    assert state["box"] == 2
    assert state["due"] == NOW + timedelta(days=3)


def test_leitner_infers_box_from_ledger(session, learner, question):
    item = ErrorItem(learner_id=learner.id, question_id=question.id)
    session.add(item)
    _seed_catalog(session)
    learner.strategy_id = "leitner"
    session.commit()

    apply_review(session, item, RATING_GOOD, now=NOW)
    assert item.next_due_at == NOW + timedelta(days=3)  # 无历史 → 盒 1，good → 盒 2
    apply_review(session, item, RATING_GOOD, now=NOW + D)
    assert item.next_due_at == NOW + D + timedelta(days=7)  # 盒 3
    apply_review(session, item, RATING_AGAIN, now=NOW + 8 * D)
    assert item.next_due_at == NOW + 8 * D + D  # 答错 → 回盒 1


# ---- registry：目录 + 未知 id 回落 ----


def test_list_strategies_contains_three():
    entries = {e["id"]: e for e in list_strategies()}
    assert set(entries) == {"fsrs", "ebbinghaus", "leitner"}
    for e in entries.values():
        assert e["name"] and e["describe"]


def test_stored_unknown_strategy_falls_back_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="cairn.strategies.registry"):
        assert strategy_id_for(SimpleNamespace(strategy_id="nope")) == DEFAULT_STRATEGY_ID
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "nope" in warnings[0].getMessage()


def test_apply_review_survives_stored_unknown_strategy(session, learner, question, caplog):
    """旧数据写了代码层没有的 strategy_id：评分不崩，按默认 fsrs 排期。"""
    # DB 目录里有这一行（FK 才过得去），但代码 registry 没注册 —— 模拟已下线策略。
    # 先单独落库：UOW 不保证同一事务里先插 strategy 行再更新 learner（见 _seed_catalog）。
    session.add(Strategy(id="legacy", name="已下线策略"))
    session.commit()
    learner.strategy_id = "legacy"
    item = ErrorItem(learner_id=learner.id, question_id=question.id)
    session.add(item)
    session.commit()

    with caplog.at_level(logging.WARNING, logger="cairn.strategies.registry"):
        apply_review(session, item, RATING_GOOD, now=NOW)
    assert any(r.levelno == logging.WARNING for r in caplog.records)

    from cairn.scheduler import Scheduler

    direct = Scheduler(desired_retention=0.85).schedule(RATING_GOOD, now=NOW)
    assert item.next_due_at == direct.due_at  # 回落 fsrs，与直接排期逐位一致


# ---- web：GET /strategies、POST /strategies/select、练习页选择控件 ----


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/web.db")
    with TestClient(app) as c:
        yield c, str(tmp_path / "web.db")


class _Ctx:
    def __init__(self, db_path):
        from cairn.models import make_engine, make_session_factory

        self.engine = make_engine(f"sqlite:///{db_path}")
        from cairn.models import init_db

        init_db(self.engine)
        self.session = make_session_factory(self.engine)()

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        self.session.close()
        self.engine.dispose()
        return False


def _make_item(db_path: str) -> int:
    with _Ctx(db_path) as s:
        learner = s.scalars(select(Learner).limit(1)).first()
        q = Question(
            learner_id=learner.id,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content="三加五等于多少？",
            answer="八",
        )
        s.add(q)
        s.flush()
        item = ErrorItem(learner_id=learner.id, question_id=q.id)
        s.add(item)
        s.commit()
        return item.id


def test_get_strategies_lists_id_and_describe(client):
    c, _ = client
    resp = c.get("/strategies")
    assert resp.status_code == 200
    body = resp.json()
    ids = {e["id"] for e in body}
    assert {"fsrs", "ebbinghaus", "leitner"} <= ids
    assert all(e["describe"] for e in body)


def test_practice_page_shows_strategy_picker(client):
    c, _ = client
    page = c.get("/practice").text
    assert 'action="/strategies/select"' in page
    assert 'value="ebbinghaus"' in page
    assert 'value="leitner"' in page


def test_select_strategy_switches_scheduling(client):
    """切到艾宾浩斯后，下一次 apply_review 用新策略（同评分同时刻，到期日不同）。"""
    from cairn.review_service import grade as review_grade

    c, db = client
    item1 = _make_item(db)
    with _Ctx(db) as s:
        review_grade(s, item1, RATING_GOOD, now=NOW)
        fsrs_due = s.get(ErrorItem, item1).next_due_at

    resp = c.post(
        "/strategies/select", data={"strategy_id": "ebbinghaus"}, follow_redirects=False
    )
    assert resp.status_code == 303

    with _Ctx(db) as s:
        learner = s.scalars(select(Learner).limit(1)).first()
        assert learner.strategy_id == "ebbinghaus"

    item2 = _make_item(db)
    with _Ctx(db) as s:
        review_grade(s, item2, RATING_GOOD, now=NOW)
        eb_due = s.get(ErrorItem, item2).next_due_at
        # SQLite 往返会丢 tzinfo（既有行为），比较前补回 UTC
        assert eb_due.replace(tzinfo=timezone.utc) == NOW + D  # 艾宾浩斯：答对一次 → 1 天
        assert eb_due != fsrs_due  # FSRS 同输入给 4 天，两策略确实不同

    page = c.get("/practice?switched=1").text
    assert "历史记录保留，新的复习计划从今天重新计算" in page


def test_select_unknown_strategy_returns_400(client):
    c, db = client
    resp = c.post("/strategies/select", data={"strategy_id": "nope"}, follow_redirects=False)
    assert resp.status_code == 400
    with _Ctx(db) as s:
        assert s.scalars(select(Learner).limit(1)).first().strategy_id is None


def test_switch_back_to_fsrs_clears_to_default(client):
    c, db = client
    assert c.post("/strategies/select", data={"strategy_id": "leitner"}, follow_redirects=False).status_code == 303
    assert c.post("/strategies/select", data={"strategy_id": "fsrs"}, follow_redirects=False).status_code == 303
    with _Ctx(db) as s:
        assert s.scalars(select(Learner).limit(1)).first().strategy_id == "fsrs"


def test_fk_catalog_rows_seeded_on_startup(client):
    """lifespan 给已注册策略补目录行：老库升级后 FK 也写得进去。"""
    c, db = client
    import sqlite3

    con = sqlite3.connect(db)
    try:
        rows = con.execute("select id from strategy order by id").fetchall()
    finally:
        con.close()
    assert {r[0] for r in rows} >= {"fsrs", "ebbinghaus", "leitner"}
