"""Batch 2 AC5–AC8 — practice orchestration (fetch / reveal / interleave)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cairn.models import ErrorItem, Question, REVIEW_STATUS_ARCHIVED, REVIEW_STATUS_NEW, REVIEW_STATUS_REVIEWING
from cairn.practice_service import fetch_practice, submit_answer

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _mk_item(
    session,
    learner,
    question_type: str,
    idx: int = 0,
    *,
    status: str = REVIEW_STATUS_NEW,
    due: datetime | None = None,
    answer: str = "42",
    analysis: str = "half of 84",
    created_at: datetime | None = None,
    audio_text: str | None = None,
    lang: str | None = None,
    content: str | None = None,
):
    q = Question(
        learner_id=learner.id,
        subject="math",
        grade_level="primary-3",
        question_type=question_type,
        content=content if content is not None else f"Q-{question_type}-{idx}",
        answer=answer,
        analysis=analysis,
        audio_text=audio_text,
        lang=lang,
    )
    session.add(q)
    session.flush()
    item = ErrorItem(
        learner_id=learner.id,
        question_id=q.id,
        review_status=status,
        next_due_at=due,
        confidence=1.0,
    )
    if created_at is not None:
        item.created_at = created_at
    session.add(item)
    session.commit()
    return item


# ---- AC5 取题池构成 ----

def test_fetch_includes_new_and_due_items(session, learner):
    new_item = _mk_item(session, learner, "fill")  # review_status=new, no due
    due_item = _mk_item(
        session, learner, "choice", status=REVIEW_STATUS_REVIEWING, due=NOW - timedelta(hours=1)
    )
    _mk_item(session, learner, "choice", 1, status=REVIEW_STATUS_REVIEWING, due=NOW + timedelta(hours=2))  # not due yet
    _mk_item(
        session, learner, "choice", 2, status=REVIEW_STATUS_ARCHIVED, due=NOW - timedelta(hours=5)
    )  # archived

    batch = fetch_practice(session, learner.id, NOW, limit=10)

    ids = {i.error_item_id for i in batch}
    assert ids == {new_item.id, due_item.id}


def test_limit_caps_batch(session, learner):
    for n in range(3):
        _mk_item(session, learner, "fill", n)

    batch = fetch_practice(session, learner.id, NOW, limit=2)

    assert len(batch) == 2


# ---- AC6 先答后揭示 ----

def test_fetch_hides_answer_and_analysis(session, learner):
    _mk_item(session, learner, "fill", answer="secret-42", analysis="secret-walkthrough")

    batch = fetch_practice(session, learner.id, NOW, limit=10)

    assert len(batch) == 1
    item = batch[0]
    assert not hasattr(item, "answer"), "PracticeItem must not carry an answer field"
    assert not hasattr(item, "analysis"), "PracticeItem must not carry an analysis field"


def test_submit_reveals_answer_and_analysis(session, learner):
    item = _mk_item(session, learner, "fill", answer="42", analysis="half of 84")

    revealed = submit_answer(session, learner.id, item.id, "41")

    assert revealed.answer == "42"
    assert revealed.analysis == "half of 84"
    assert revealed.error_item_id == item.id


# ---- 0.2 卡3 AC1：取题带 cue（audio_text/lang），仍不带 answer ----

def test_fetch_carries_audio_cue_without_answer(session, learner):
    _mk_item(session, learner, "单词", answer="secret-42", audio_text="apple", lang="en")

    item = fetch_practice(session, learner.id, NOW, limit=10)[0]

    assert item.audio_text == "apple"
    assert item.lang == "en"
    assert not hasattr(item, "answer")
    assert not hasattr(item, "analysis")


def test_fetch_leaves_empty_cue_empty(session, learner):
    """数学题没有 audio_text：字段为 None，取题不报错。"""
    _mk_item(session, learner, "计算", answer="8")

    item = fetch_practice(session, learner.id, NOW, limit=10)[0]

    assert item.audio_text is None
    assert item.is_dictation is False


# ---- 0.2 卡3 修正：听写模式标记（有 cue 且 content≠answer） ----

def test_fetch_marks_dictation_when_cue_and_content_differs(session, learner):
    _mk_item(session, learner, "词语", answer="苹果", audio_text="píngguǒ", lang="zh")

    item = fetch_practice(session, learner.id, NOW, limit=10)[0]

    assert item.is_dictation is True
    assert not hasattr(item, "answer")


def test_fetch_not_dictation_when_content_equals_answer(session, learner):
    """content==answer（老数据/数学题）即便填了 cue 也不进听写模式。"""
    _mk_item(
        session, learner, "计算", content="八", answer="八", audio_text="bā", lang="zh"
    )

    item = fetch_practice(session, learner.id, NOW, limit=10)[0]

    assert item.is_dictation is False


# ---- AC7 题型交错（可行情形） ----

def test_interleave_no_consecutive_same_type(session, learner):
    # counts: choice x2, fill x2, apply x1 — max(2) <= rest(3) + 1, fully separable
    _mk_item(session, learner, "choice", 0)
    _mk_item(session, learner, "choice", 1)
    _mk_item(session, learner, "fill", 0)
    _mk_item(session, learner, "fill", 1)
    _mk_item(session, learner, "apply", 0)

    batch = fetch_practice(session, learner.id, NOW, limit=5)

    types = [i.question_type for i in batch]
    assert len(batch) == 5
    for prev, cur in zip(types, types[1:]):
        assert prev != cur, f"adjacent same type: {types}"


# ---- AC8 题型交错（不可行情形） ----

def test_interleave_infeasible_minimal_pairs(session, learner):
    # 4 choice + 1 fill: choice cannot be fully separated; theoretical minimum
    # adjacent same-type pairs = max - rest - 1 = 4 - 1 - 1 = 2
    for n in range(4):
        _mk_item(session, learner, "choice", n)
    _mk_item(session, learner, "fill", 0)

    batch = fetch_practice(session, learner.id, NOW, limit=5)

    types = [i.question_type for i in batch]
    assert len(batch) == 5
    pairs = sum(1 for prev, cur in zip(types, types[1:]) if prev == cur)
    assert pairs == 2, f"expected minimal 2 adjacent same-type pairs, got {pairs} in {types}"


# ---- 0.2 卡1：交错必须先于 limit 截断 ----

def test_interleave_runs_before_limit_truncation(session, learner):
    """池子大于一批时交错仍要生效。

    回归：旧实现 `_interleave(candidates[:limit])` 先截断，20 道里前 10 道
    全是「计算」，交错了也等于没交错。先交错再截断，前 10 道必须两种题型都有。
    """
    base = NOW - timedelta(days=30)
    for n in range(10):  # 先录的 10 道全是「计算」
        _mk_item(session, learner, "计算", n, created_at=base + timedelta(minutes=n))
    for n in range(10):  # 后录的 10 道全是「填空」
        _mk_item(session, learner, "填空", n, created_at=base + timedelta(minutes=10 + n))

    batch = fetch_practice(session, learner.id, NOW, limit=10)

    types = [i.question_type for i in batch]
    assert len(batch) == 10
    assert set(types) == {"计算", "填空"}, f"取题池被截断成单一题型: {types}"
    for prev, cur in zip(types, types[1:]):
        assert prev != cur, f"交错后仍相邻同题型: {types}"


def test_limit_keeps_priority_within_one_type(session, learner):
    """截断仍按优先级取：同题型内 created_at 早的先出（新题优先语义不变）。"""
    base = NOW - timedelta(days=30)
    for n in range(6):
        _mk_item(session, learner, "fill", n, created_at=base + timedelta(minutes=n))

    batch = fetch_practice(session, learner.id, NOW, limit=3)

    assert [i.content for i in batch] == ["Q-fill-0", "Q-fill-1", "Q-fill-2"]


# ---- BUKA 配额 + 难度分（docs/BUKA-LESSONS.md 一.2/一.3） ----

import pytest

from cairn.models import REVIEW_STATUS_MASTERED
from cairn.models.learner import utcnow
from cairn.models.review import Review


@pytest.fixture(autouse=True)
def _clean_quota_env(monkeypatch):
    """配额参数只由测试显式设定，不受开发者 shell 环境影响。"""
    for var in ("CAIRN_BATCH_SIZE", "CAIRN_GREEN_RATIO", "CAIRN_RED_RATIO"):
        monkeypatch.delenv(var, raising=False)


DUE = NOW - timedelta(hours=1)


def _mk_pool_item(
    session,
    learner,
    idx: int,
    *,
    status: str = REVIEW_STATUS_REVIEWING,
    wrong_count: int = 1,
    due: datetime | None = DUE,
    lang: str | None = None,
    question_type: str = "fill",
    hard_reviews: int = 0,
    restored: bool = False,
):
    """配额测试专用造题：一条错题 + 一道题，可调难度三要素。"""
    q = Question(
        learner_id=learner.id,
        subject="math" if lang is None else "english",
        grade_level="primary-3",
        question_type=question_type,
        content=f"P-{question_type}-{idx}-{lang}",
        answer="42",
        analysis="-",
        lang=lang,
    )
    session.add(q)
    session.flush()
    item = ErrorItem(
        learner_id=learner.id,
        question_id=q.id,
        review_status=status,
        next_due_at=due,
        wrong_count=wrong_count,
        confidence=1.0,
        restored_at=utcnow() if restored else None,
    )
    session.add(item)
    session.flush()
    for _ in range(hard_reviews):
        session.add(Review(error_item_id=item.id, rating="hard", reviewed_at=utcnow()))
    session.commit()
    return item


def _counts_by_status(session, batch):
    """按 review_status 数一批 PracticeItem（靠 error_item_id 回查）。"""
    out = {}
    for it in batch:
        row = session.get(ErrorItem, it.error_item_id)
        out[row.review_status] = out.get(row.review_status, 0) + 1
    return out


def test_quota_green_capped_and_red_dominates(session, learner):
    """30 题混合池（10绿/10黄/10红），limit=20 → 绿≤4、红≥8。"""
    for n in range(10):
        _mk_pool_item(session, learner, n, status=REVIEW_STATUS_MASTERED)
        _mk_pool_item(session, learner, 100 + n, wrong_count=1)
        _mk_pool_item(session, learner, 200 + n, wrong_count=3)

    batch = fetch_practice(session, learner.id, NOW, limit=20)

    assert len(batch) == 20
    counts = _counts_by_status(session, batch)
    assert counts.get(REVIEW_STATUS_MASTERED, 0) <= 4, counts
    # 红 = wrong_count≥2 的未掌握题；用难度分反查数量
    red = sum(
        1
        for it in batch
        if (row := session.get(ErrorItem, it.error_item_id)).review_status != REVIEW_STATUS_MASTERED
        and row.wrong_count >= 2
    )
    assert red >= 8, red


def test_quota_single_green_still_taken(session, learner):
    """绿池只有 1 题：绿仍取 1（不因比例取整而空转）。"""
    _mk_pool_item(session, learner, 0, status=REVIEW_STATUS_MASTERED)
    for n in range(1, 26):
        _mk_pool_item(session, learner, n, wrong_count=3)

    batch = fetch_practice(session, learner.id, NOW, limit=20)

    assert len(batch) == 20
    counts = _counts_by_status(session, batch)
    assert counts.get(REVIEW_STATUS_MASTERED, 0) == 1


def test_quota_backfills_green_when_red_yellow_short(session, learner):
    """红黄不足时用绿补满，总数仍等于 limit。"""
    for n in range(14):
        _mk_pool_item(session, learner, n, status=REVIEW_STATUS_MASTERED)
    for n in range(14, 18):
        _mk_pool_item(session, learner, n, wrong_count=3)  # 红
    for n in range(18, 20):
        _mk_pool_item(session, learner, n, wrong_count=1)  # 黄

    batch = fetch_practice(session, learner.id, NOW, limit=20)

    assert len(batch) == 20
    counts = _counts_by_status(session, batch)
    # 红4 + 黄2 全进，剩余 14 个名额全给绿（cap 4 + 补满 10）
    assert counts.get(REVIEW_STATUS_MASTERED, 0) == 14
    assert counts.get(REVIEW_STATUS_REVIEWING, 0) == 6


def test_quota_balances_zh_and_en(session, learner):
    """中英混合池 → 各占一半（相差 ≤1）。"""
    for n in range(12):
        _mk_pool_item(session, learner, n, wrong_count=3, lang="zh", question_type="默写")
    for n in range(12, 20):
        _mk_pool_item(session, learner, n, wrong_count=3, lang="en", question_type="默写")

    batch = fetch_practice(session, learner.id, NOW, limit=10)

    assert len(batch) == 10
    zh = sum(1 for it in batch if it.lang == "zh")
    en = sum(1 for it in batch if it.lang == "en")
    assert abs(zh - en) <= 1, (zh, en)


def test_difficulty_orders_same_due_wrong_first(session, learner):
    """同到期：wrong=3（难度6）先于 wrong=1（难度2）。"""
    weak = _mk_pool_item(session, learner, 0, wrong_count=3, question_type="计算")
    easy = _mk_pool_item(session, learner, 1, wrong_count=1, question_type="计算")

    batch = fetch_practice(session, learner.id, NOW, limit=10)

    ids = [it.error_item_id for it in batch]
    assert ids.index(weak.id) < ids.index(easy.id), ids


def test_difficulty_lapse_beats_single_wrong(session, learner):
    """同到期：lapse=1（+3）先于 wrong=1（+2）——难度分对照。"""
    from cairn.practice_service import difficulty_score

    assert difficulty_score(0, 0, 1) > difficulty_score(1, 0, 0)
    lapsed = _mk_pool_item(
        session, learner, 0, wrong_count=0, restored=True, question_type="计算"
    )
    plain = _mk_pool_item(session, learner, 1, wrong_count=1, question_type="计算")

    batch = fetch_practice(session, learner.id, NOW, limit=10)

    ids = [it.error_item_id for it in batch]
    assert ids.index(lapsed.id) < ids.index(plain.id), ids


def test_green_ratio_zero_excludes_green(session, learner, monkeypatch):
    """CAIRN_GREEN_RATIO=0 → 一道绿题都不出。"""
    monkeypatch.setenv("CAIRN_GREEN_RATIO", "0")
    for n in range(10):
        _mk_pool_item(session, learner, n, status=REVIEW_STATUS_MASTERED)
    for n in range(10, 35):
        _mk_pool_item(session, learner, n, wrong_count=3)

    batch = fetch_practice(session, learner.id, NOW, limit=20)

    assert len(batch) == 20
    assert _counts_by_status(session, batch).get(REVIEW_STATUS_MASTERED, 0) == 0


# ---- 配额返工（评审 CHANGES）：绿硬上限 + 批次级中英平衡 ----

def test_quota_green_hard_cap_and_limit_one(session, learner):
    """limit=5 → 绿恰为 1（红够、黄稀缺时富余红吸收，绿不回填越界）；
    limit=1 → 照常出 1 题。"""
    for n in range(10):
        _mk_pool_item(session, learner, n, status=REVIEW_STATUS_MASTERED)
    for n in range(10, 15):
        _mk_pool_item(session, learner, n, wrong_count=3)  # 红 5，黄 0

    batch = fetch_practice(session, learner.id, NOW, limit=5)
    assert len(batch) == 5
    counts = _counts_by_status(session, batch)
    assert counts.get(REVIEW_STATUS_MASTERED, 0) == 1, counts

    batch1 = fetch_practice(session, learner.id, NOW, limit=1)
    assert len(batch1) == 1


def test_quota_batch_level_lang_balance_mixed_states(session, learner):
    """绿全中文 / 红全中文 / 黄全英文，limit=20 → 整批中英差 ≤1。

    回归：旧实现只在每个配额组内各平衡一遍，组间不拼 → 14 中 / 6 英。
    平衡必须在最终成批这一步做。绿硬上限（≤4）同时仍要守住。"""
    for n in range(10):
        _mk_pool_item(
            session, learner, n, status=REVIEW_STATUS_MASTERED, lang="zh", question_type="默写"
        )
    for n in range(10, 20):
        _mk_pool_item(session, learner, n, wrong_count=3, lang="zh", question_type="默写")
    for n in range(20, 30):
        _mk_pool_item(session, learner, n, wrong_count=1, lang="en", question_type="默写")

    batch = fetch_practice(session, learner.id, NOW, limit=20)

    assert len(batch) == 20
    zh = sum(1 for it in batch if it.lang == "zh")
    en = sum(1 for it in batch if it.lang == "en")
    assert abs(zh - en) <= 1, (zh, en)
    assert _counts_by_status(session, batch).get(REVIEW_STATUS_MASTERED, 0) <= 4


def test_quota_lang_balance_yields_when_pool_short(session, learner):
    """英文池总共只有 2 条 → 批内英文恰为 2：是「确实不够」，不是算法没做。"""
    for n in range(20):
        _mk_pool_item(session, learner, n, wrong_count=3, lang="zh", question_type="默写")
    for n in range(20, 22):
        _mk_pool_item(session, learner, n, wrong_count=1, lang="en", question_type="默写")

    batch = fetch_practice(session, learner.id, NOW, limit=20)

    assert len(batch) == 20
    en = sum(1 for it in batch if it.lang == "en")
    assert en == 2, en


def test_difficulty_hard_rating_beats_plain_wrong(session, learner):
    """同到期：wrong=1 + 1 次 hard（不熟 +1 → 3 分）先于无 hard 的 wrong=1（2 分）。"""
    hard_item = _mk_pool_item(
        session, learner, 0, wrong_count=1, hard_reviews=1, question_type="计算"
    )
    plain = _mk_pool_item(session, learner, 1, wrong_count=1, question_type="计算")

    batch = fetch_practice(session, learner.id, NOW, limit=10)

    ids = [it.error_item_id for it in batch]
    assert ids.index(hard_item.id) < ids.index(plain.id), ids
