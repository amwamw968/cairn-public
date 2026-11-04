"""0.2 卡7 统计：服务层聚合 + /stats 页面。

数据全用中文：学科是「数学 / 英语 / 语文」，知识点是中文名。时间一律按
UTC 写入（与 `apply_review` 落库口径一致，SQLite 丢 tzinfo），断言按 +08:00
本地日历日解释——顺带钉住「今天」是本地日、不是 UTC 日。

覆盖：一个知识点多题、一题多知识点不重复计数、30 天窗口边界、样本不足、
again/hard 算错、完成度三个数不重叠、断一天的连续打卡、掌握度累计曲线、空态。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn import stats_service
from cairn.concept_service import tag_question
from cairn.models import (
    ErrorItem,
    Learner,
    Question,
    Review,
    REVIEW_STATUS_ARCHIVED,
    REVIEW_STATUS_REVIEWING,
    make_engine,
    make_session_factory,
)
from cairn.web import create_app

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)  # 本地 2026-10-31 12:00
TODAY = NOW.astimezone(TZ).date()


def _local(days_ago: int, hour: int = 10, minute: int = 0) -> datetime:
    """A local wall-clock moment *days_ago* days back, stored as UTC."""
    day = TODAY - timedelta(days=days_ago)
    return datetime.combine(day, time(hour, minute), tzinfo=TZ).astimezone(timezone.utc)


def _question(session, subject: str, qtype: str, content: str, answer: str = "答案") -> Question:
    """题目必须有主人：用 fixture 建好的那个孩子（测试里唯一的 learner）。"""
    owner = session.scalars(select(Learner).order_by(Learner.id)).first()
    if owner is None:
        owner = Learner(name="test-child")
        session.add(owner)
        session.commit()
    question = Question(
        learner_id=owner.id,
        subject=subject,
        grade_level="primary",
        question_type=qtype,
        content=content,
        answer=answer,
    )
    session.add(question)
    session.commit()
    return question


def _item(
    session,
    learner: Learner,
    question: Question,
    *,
    status: str = REVIEW_STATUS_REVIEWING,
    due: datetime | None = None,
    last_reviewed: datetime | None = None,
) -> ErrorItem:
    item = ErrorItem(
        learner_id=learner.id,
        question_id=question.id,
        wrong_count=1,
        review_status=status,
        next_due_at=due,
        last_reviewed_at=last_reviewed,
    )
    session.add(item)
    session.commit()
    return item


def _review(session, item: ErrorItem, rating: str, at: datetime) -> Review:
    review = Review(error_item_id=item.id, rating=rating, reviewed_at=at)
    session.add(review)
    session.commit()
    return review


def _stats(session, learner: Learner):
    return stats_service.build_stats(session, learner.id, now=NOW, tz=TZ)


# ---- AC1 薄弱榜：一个知识点多题、聚合口径、排序 ----


def test_weakness_aggregates_multiple_questions_and_sorts(session, learner):
    q1 = _question(session, "数学", "计算", "23 × 4 = ?", "92")
    q2 = _question(session, "数学", "计算", "18 × 5 = ?", "90")
    carry = tag_question(session, q1, ["两位数乘一位数进位"])[0]
    tag_question(session, q2, ["两位数乘一位数进位"])
    session.commit()

    item1 = _item(session, learner, q1)
    _review(session, item1, "again", _local(0))
    _review(session, item1, "hard", _local(5))
    _review(session, item1, "good", _local(10))

    item2 = _item(session, learner, q2)
    _review(session, item2, "good", _local(3))
    _review(session, item2, "easy", _local(4))
    _review(session, item2, "good", _local(6))

    other = _question(session, "数学", "计算", "3x = 12，x = ?", "4")
    tag_question(session, other, ["移项变号"])
    session.commit()
    item3 = _item(session, learner, other)
    for days in (0, 1, 2):
        _review(session, item3, "again", _local(days))

    rows = _stats(session, learner).weakness

    assert [row.name for row in rows] == ["移项变号", "两位数乘一位数进位"]
    by_name = {row.name: row for row in rows}
    carry_row = by_name["两位数乘一位数进位"]
    assert carry_row.subject == "数学"
    assert carry_row.error_count == 2
    assert carry_row.open_count == 2
    assert carry_row.wrong_30d == 2
    assert carry_row.answered == 6
    assert carry_row.rate_percent == 67  # 4 对 / 6 次

    move_row = by_name["移项变号"]
    assert move_row.error_count == 1
    assert move_row.wrong_30d == 3
    assert move_row.rate_percent == 0
    assert carry_row.concept_id == carry.id


def test_weakness_counts_archived_as_closed_not_deleted(session, learner):
    question = _question(session, "语文", "词语", "苹果", "píngguǒ")
    tag_question(session, question, ["字词音形"])
    session.commit()
    item = _item(
        session,
        learner,
        question,
        status=REVIEW_STATUS_ARCHIVED,
        last_reviewed=_local(4),
    )
    _review(session, item, "good", _local(4))

    row = _stats(session, learner).weakness[0]
    assert row.error_count == 1
    assert row.open_count == 0
    assert row.answered == 1
    assert row.rate_percent is None  # 样本 1 < 3


# ---- AC2 一题多知识点：按题 ID 并集，单知识点内不重复计数 ----


def test_multi_concept_question_is_counted_once_per_concept(session, learner):
    question = _question(session, "数学", "计算", "36 + 47 = ?", "83")
    tag_question(session, question, ["进位加法", "两位数加法"])
    session.commit()
    item = _item(session, learner, question)
    _review(session, item, "again", _local(1))
    _review(session, item, "good", _local(2))
    _review(session, item, "good", _local(3))

    rows = _stats(session, learner).weakness
    assert {row.name for row in rows} == {"进位加法", "两位数加法"}
    for row in rows:
        assert row.error_count == 1
        assert row.answered == 3  # 一道题 3 次作答，不是 6 次
        assert row.wrong_30d == 1
        assert row.rate_percent == 67


# ---- AC3 30 天窗口边界（本地日历日）----


def test_window_boundary_is_local_midnight_29_days_back(session, learner):
    question = _question(session, "英语", "单词", "苹果", "apple")
    tag_question(session, question, ["食物类词汇"])
    session.commit()
    item = _item(session, learner, question)

    window_start = datetime.combine(
        TODAY - timedelta(days=stats_service.WINDOW_DAYS - 1), time.min, tzinfo=TZ
    ).astimezone(timezone.utc)

    _review(session, item, "again", window_start)  # 恰在起点：计入
    _review(session, item, "again", window_start - timedelta(seconds=1))  # 早 1 秒：不计
    _review(session, item, "again", NOW)  # 窗口终点：计入
    _review(session, item, "again", NOW + timedelta(seconds=1))  # 未来：不计

    row = _stats(session, learner).weakness[0]
    assert row.answered == 4  # 样本看全量作答
    assert row.wrong_30d == 2  # 只有起点与 now 两次落在窗口内


# ---- AC4 样本不足 ----


def test_sample_under_three_has_no_rate(session, learner):
    question = _question(session, "数学", "应用", "小明有 5 个苹果，吃了 2 个，还剩几个？", "3")
    tag_question(session, question, ["减法应用题"])
    session.commit()
    item = _item(session, learner, question)
    _review(session, item, "good", _local(1))
    _review(session, item, "again", _local(2))

    row = _stats(session, learner).weakness[0]
    assert row.answered == 2
    assert row.rate_percent is None
    assert row.sample_sufficient is False

    _review(session, item, "good", _local(3))
    row = _stats(session, learner).weakness[0]
    assert row.answered == 3
    assert row.rate_percent == 67
    assert row.sample_sufficient is True


# ---- AC5 错因 / 题型分布：题型 × 学科，again/hard 算错 ----


def test_distribution_counts_wrong_by_type_and_subject(session, learner):
    math_calc = _question(session, "数学", "计算", "45 ÷ 9 = ?", "5")
    math_word = _question(session, "数学", "应用", "每盒 6 支，4 盒共几支？", "24")
    english = _question(session, "英语", "单词", "书", "book")
    chinese = _question(session, "语文", "词语", "春天", "chūn tiān")

    calc_item = _item(session, learner, math_calc)
    _review(session, calc_item, "again", _local(0))
    _review(session, calc_item, "hard", _local(2))
    _review(session, calc_item, "good", _local(3))  # 对：不计

    word_item = _item(session, learner, math_word)
    _review(session, word_item, "again", _local(1))
    _review(session, word_item, "again", _local(40))  # 窗口外：不计

    english_item = _item(session, learner, english)
    _review(session, english_item, "easy", _local(0))  # 对：不计

    chinese_item = _item(session, learner, chinese)
    _review(session, chinese_item, "hard", _local(5))

    report = _stats(session, learner)

    assert report.distribution_subjects == ("数学", "语文")
    rows = {row.question_type: row for row in report.distribution}
    assert set(rows) == {"计算", "应用", "词语"}
    assert rows["计算"].cells == (2, 0)
    assert rows["应用"].cells == (1, 0)
    assert rows["词语"].cells == (0, 1)
    assert [row.total for row in report.distribution] == [2, 1, 1]


def test_weakness_covers_all_three_subjects(session, learner):
    for subject, qtype, content in (
        ("数学", "计算", "7 × 8 = ?"),
        ("英语", "单词", "猫"),
        ("语文", "词语", "秋天"),
    ):
        question = _question(session, subject, qtype, content, "x")
        tag_question(session, question, [f"{subject}知识点"])
        session.commit()
        item = _item(session, learner, question)
        _review(session, item, "good", _local(0))

    subjects = {row.subject for row in _stats(session, learner).weakness}
    assert subjects == {"数学", "英语", "语文"}


# ---- AC6 复习完成度：今天到期 / 今天完成 / 逾期 ----


def test_completion_counts_due_completed_overdue(session, learner):
    due_a = _item(
        session, learner, _question(session, "数学", "计算", "9 + 8 = ?"), due=_local(0, 10)
    )
    due_b = _item(
        session, learner, _question(session, "英语", "单词", "狗"), due=_local(0, 18)
    )
    overdue_done = _item(
        session, learner, _question(session, "语文", "词语", "树叶"), due=_local(1, 9)
    )
    _review(session, overdue_done, "good", _local(0, 11))
    _item(
        session, learner, _question(session, "数学", "计算", "6 + 7 = ?"), due=_local(2, 9)
    )  # 逾期未做
    _item(
        session,
        learner,
        _question(session, "数学", "计算", "5 + 6 = ?"),
        status=REVIEW_STATUS_ARCHIVED,
        due=_local(0, 8),
    )  # 已出库：不计
    _item(session, learner, _question(session, "英语", "单词", "猫"))  # 新题：无到期日

    completion = _stats(session, learner).completion
    assert completion.has_data is True
    assert completion.due_today == 2
    assert completion.completed_today == 1
    assert completion.overdue == 1


# ---- AC7 温和打卡：断一天只数到断点；今天没做从昨天起算 ----


def test_streak_stops_at_gap_without_punishment(session, learner):
    question = _question(session, "数学", "计算", "4 × 6 = ?")
    item = _item(session, learner, question)
    for days in (0, 1, 2, 4):
        _review(session, item, "good", _local(days))

    streak = _stats(session, learner).streak
    assert streak.has_data is True
    assert streak.reviewed_today is True
    assert streak.days == 3  # 今天/昨天/前天；第 3 天断，第 4 天的不接


def test_streak_uses_yesterday_when_today_is_still_empty(session, learner):
    question = _question(session, "英语", "单词", "猫")
    item = _item(session, learner, question)
    _review(session, item, "good", _local(1))
    _review(session, item, "good", _local(2))

    streak = _stats(session, learner).streak
    assert streak.reviewed_today is False
    assert streak.days == 2  # 当天还没作答不惩罚


def test_streak_empty_states(session, learner):
    streak = _stats(session, learner).streak
    assert streak.has_data is False
    assert streak.days == 0


# ---- AC8 掌握度曲线：30 点累计，出库日按最后作答日 ----


def test_mastery_curve_is_cumulative_archived_count(session, learner):
    for days_ago in (5, 3, 1):
        question = _question(session, "数学", "计算", f"{days_ago} 天前出库的题")
        _item(
            session,
            learner,
            question,
            status=REVIEW_STATUS_ARCHIVED,
            last_reviewed=_local(days_ago),
        )

    rejected = _question(session, "语文", "词语", "从未作答就被驳回")
    _item(session, learner, rejected, status=REVIEW_STATUS_ARCHIVED)  # 无 last_reviewed

    curve = _stats(session, learner).mastery
    assert curve.has_data is True
    assert len(curve.points) == stats_service.WINDOW_DAYS
    assert curve.max_count == 3  # 被驳回的题不算「学会」
    assert curve.points[-1].count == 3
    assert curve.points[-1].day == TODAY
    assert curve.points[0].count == 0
    # 第 5 天起有 1 题，第 3 天起 2 题：曲线单调不减
    counts = [point.count for point in curve.points]
    assert counts == sorted(counts)
    assert curve.points[-1].y < curve.points[0].y  # 数量越高，图上越高


# ---- AC10/AC9 页面与导航（中文数据）----


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/stats.db")
    with TestClient(app) as test_client:
        yield test_client, str(tmp_path / "stats.db")


def _seed_page_data(db_path: str) -> None:
    """直接写库：页面测试只关心「服务算出什么，页面就显示什么」。"""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    with factory() as session:
        learner = session.scalars(select(Learner).order_by(Learner.id)).first()
        question = _question(session, "数学", "计算", "28 × 3 = ?", "84")
        tag_question(session, question, ["两位数乘一位数进位"])
        session.commit()
        item = _item(session, learner, question, due=_local(0, 18))
        _review(session, item, "again", datetime.now(timezone.utc))
        _review(session, item, "again", datetime.now(timezone.utc) - timedelta(days=1))
    engine.dispose()


def test_stats_page_shows_summary_and_table(client):
    """G2-7 升级：一句话总结最大 + 简表三行（蓝图 E.11），旧四块撤（决策点 5）。"""
    test_client, db_path = client
    _seed_page_data(db_path)

    page = test_client.get("/stats").text
    assert "最近怎么样" in page
    assert "这周练了" in page  # 一句话总结（数字随种子走）
    for row in ("练了几题", "几题过关", "还在反复错的题"):
        assert row in page, row
    # 旧四块标题全部撤
    for gone in ("薄弱知识点", "错因 / 题型分布", "复习完成度", "打卡与掌握度"):
        assert gone not in page, gone
    # 旧壳页的导航都指得到 /stats。v3 壳页的「最近怎么样」入口在今天页近况行。
    for path in ("/books",):
        assert 'href="/stats"' in test_client.get(path).text


def test_stats_page_repeated_rows_link_to_questions(client):
    """「哪几题还在反复错」逐条给题干摘要 + 链到详情（与错题本同源谓词）。"""
    test_client, db_path = client
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    with factory() as session:
        learner = session.scalars(select(Learner).order_by(Learner.id)).first()
        question = _question(session, "数学", "计算", "两位数乘一位数进位", "48")
        item = _item(session, learner, question, due=_local(0, 18))
        item.wrong_count = 3
        _review(session, item, "again", datetime.now(timezone.utc))
        session.commit()
        question_id = question.id
    engine.dispose()

    page = test_client.get("/stats").text
    assert "两位数乘一位数进位" in page
    assert "还在反复错" in page
    assert f'href="/questions/{question_id}"' in page


def test_stats_page_empty_state_has_no_zeros(client):
    """G2-7 升级：空态 §4.3 三段式，不上「0 题」这类零数字。"""
    test_client, _ = client
    page = test_client.get("/stats").text

    assert "还没有练习记录" in page
    assert "孩子练过的题会在这里汇成一句话。" in page
    assert "先去完成今天的练习" in page
    assert "0 题" not in page and "0 天" not in page


def test_stats_page_keeps_no_chart_library(client):
    """G2-7：无图表（一句话+简表就够，蓝图 E.11）；也不许引图表库/外链。"""
    test_client, db_path = client
    _seed_page_data(db_path)

    page = test_client.get("/stats").text
    # 不要图表（家长不需要曲线）；壳里的图标 svg 除外，曲线/图表库一个不许有
    assert "<polyline" not in page and "<canvas" not in page
    assert "cdn" not in page.lower()
    assert "chart.js" not in page.lower()


# ---- AC11 纯查询：不碰调度器、不碰网络 ----


def test_stats_service_is_query_only():
    source = Path(stats_service.__file__).read_text(encoding="utf-8")
    for forbidden in ("scheduler", "fsrs", "requests", "httpx", "urllib"):
        assert forbidden not in source


def test_stats_does_not_touch_review_state(session, learner):
    """统计是只读的：跑一遍不改任何行的状态与时间戳。"""
    question = _question(session, "数学", "计算", "8 × 9 = ?")
    item = _item(session, learner, question, due=_local(0, 10))
    _review(session, item, "again", _local(0, 9))

    def snapshot():
        return session.execute(
            select(
                ErrorItem.review_status, ErrorItem.next_due_at, ErrorItem.last_reviewed_at
            ).where(ErrorItem.id == item.id)
        ).one()

    before = snapshot()
    _stats(session, learner)
    assert snapshot() == before
