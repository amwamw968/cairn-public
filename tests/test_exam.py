"""0.2 卡10 — 考试倒计时：考试表、倒计时、冲刺期提前拉题、页面闭环。

数据一律中文（单元考 / 期中考试 / 数学 / 三加五等于多少？）：这一批的功能全是
家长用中文填、孩子看中文，ASCII 样例测不出编码与口径问题。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from cairn.entry_service import ValidationError
from cairn.exam_service import (
    SPRINT_DAYS,
    active_exam,
    build_exam_view,
    days_left,
    delete_exam,
    in_sprint,
    plan_exam_pull_forward,
    set_exam,
)
from cairn.migrations import SCHEMA_VERSION
from cairn.models import (
    ErrorItem,
    Exam,
    Learner,
    Question,
    REVIEW_STATUS_ARCHIVED,
    REVIEW_STATUS_REVIEWING,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.web import create_app

TODAY = date(2026, 11, 5)


def _mk_item(
    session,
    learner,
    *,
    subject: str = "数学",
    due: datetime,
    status: str = REVIEW_STATUS_REVIEWING,
    content: str = "三加五等于多少？",
):
    question = Question(
        learner_id=learner.id,
        subject=subject,
        grade_level="primary-5",
        question_type="计算" if subject == "数学" else "单词",
        content=content,
        answer="八" if subject == "数学" else "apple",
    )
    session.add(question)
    session.flush()
    item = ErrorItem(
        learner_id=learner.id,
        question_id=question.id,
        review_status=status,
        next_due_at=due,
        confidence=1.0,
    )
    session.add(item)
    session.commit()
    return item


def _due(item) -> datetime:
    return item.next_due_at


# --------------------------------------------------------------- AC1 建表


def test_schema_exam_table(session):
    """exam 表按约定列建出；subject 可空（NULL = 全部学科）。"""
    inspector = inspect(session.bind)
    assert "exam" in inspector.get_table_names()
    columns = {c["name"]: c for c in inspector.get_columns("exam")}
    assert set(columns) == {
        "id",
        "learner_id",
        "name",
        "exam_date",
        "subject",
        "note",
        "created_at",
    }
    assert columns["subject"]["nullable"] is True
    assert columns["note"]["nullable"] is True
    assert columns["exam_date"]["nullable"] is False


# --------------------------------------------------------------- AC2 迁移


def test_migrations_v7_adds_exam_table(tmp_path):
    """老库（v6、没有 exam 表、有中文数据）→ init_db：补表、版本 +1、数据一字不差。"""
    db = tmp_path / "v7.db"
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    assert SCHEMA_VERSION >= 7

    factory = make_session_factory(engine)
    with factory() as session:
        learner = Learner(name="小明", grade_level="primary-5")
        session.add(learner)
        session.commit()
        question = Question(
            learner_id=learner.id,
            subject="数学",
            grade_level="primary-5",
            question_type="计算",
            content="三加五等于多少？",
            answer="八",
        )
        session.add(question)
        session.flush()
        session.add(ErrorItem(learner_id=learner.id, question_id=question.id))
        session.commit()

    # 退回「v6 且没有 exam 表」的样子
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE exam")
        conn.exec_driver_sql("UPDATE schema_version SET version = 6 WHERE id = 1")

    init_db(engine)
    assert "exam" in set(inspect(engine).get_table_names())
    engine.dispose()

    con = sqlite3.connect(db)
    try:
        version = con.execute("select version from schema_version where id = 1").fetchone()[0]
        name = con.execute("select name from learner where id = 1").fetchone()[0]
        content = con.execute("select content from question where id = 1").fetchone()[0]
        items = con.execute("select count(*) from error_item").fetchone()[0]
    finally:
        con.close()
    assert version == SCHEMA_VERSION
    assert (name, content, items) == ("小明", "三加五等于多少？", 1)


# --------------------------------------------------------------- AC3 倒计时


def test_set_exam_and_countdown(session, learner):
    """设置考试后倒计时正确；多场考试时取最近的一场。"""
    exam = set_exam(
        session,
        learner.id,
        "单元考",
        date(2026, 11, 10),
        subject="数学",
        note="第三单元 小数乘法",
    )
    assert exam.id is not None
    assert exam.subject == "数学"

    # 再来一场更晚的：生效的仍是单元考
    set_exam(session, learner.id, "期中考试", date(2026, 11, 20))

    current = active_exam(session, learner.id, TODAY)
    assert current is not None and current.name == "单元考"
    assert days_left(current, TODAY) == 5
    assert in_sprint(current, TODAY) is True

    view = build_exam_view(session, learner.id, TODAY)
    assert [row.name for row in view.rows] == ["单元考", "期中考试"]
    assert [row.days_left for row in view.rows] == [5, 15]
    assert view.active_id == exam.id
    assert all(row.active for row in view.rows)


def test_set_exam_validation(session, learner):
    """空名称 / 非法学科 / 不是日期：都不写库。"""
    with pytest.raises(ValidationError):
        set_exam(session, learner.id, "   ", date(2026, 11, 10))
    with pytest.raises(ValidationError):
        set_exam(session, learner.id, "单元考", date(2026, 11, 10), subject="科学")
    with pytest.raises(ValidationError):
        set_exam(session, learner.id, "单元考", datetime(2026, 11, 10, 8, 0))
    assert session.scalars(select(Exam)).first() is None


def test_delete_exam(session, learner):
    """只能删自己的记录；别人删不动。"""
    other = Learner(name="小华")
    session.add(other)
    session.commit()
    exam = set_exam(session, learner.id, "单元考", date(2026, 11, 10))

    assert delete_exam(session, other.id, exam.id) is False
    assert session.get(Exam, exam.id) is not None
    assert delete_exam(session, learner.id, exam.id) is True
    assert session.get(Exam, exam.id) is None


# --------------------------------------------------------------- AC4 拉题


def test_pull_forward_in_sprint(session, learner):
    """冲刺期：考后 7 天内到期的题被均匀铺到「今天 → 考前一天」。"""
    set_exam(session, learner.id, "单元考", date(2026, 11, 10))
    early = _mk_item(session, learner, due=datetime(2026, 11, 11, 8, 0))
    middle = _mk_item(session, learner, due=datetime(2026, 11, 13, 8, 0), content="十二减四等于多少？")
    late = _mk_item(session, learner, due=datetime(2026, 11, 15, 20, 0), content="七乘八等于多少？")
    already_due = _mk_item(session, learner, due=datetime(2026, 11, 2, 8, 0), content="九加一等于多少？")

    result = plan_exam_pull_forward(session, learner.id, TODAY)

    assert result.exam is not None and result.exam.name == "单元考"
    assert (result.days_left, result.in_sprint, result.pulled) == (5, True, 3)
    targets = [_due(item) for item in (early, middle, late)]
    assert all(TODAY <= target.date() <= date(2026, 11, 9) for target in targets)
    assert targets == sorted(targets)  # 到期越晚 → 越靠近考试
    assert len({target.date() for target in targets}) == 3  # 均匀铺开，不挤在同一天
    # 时刻保留，只换日期
    assert [target.time() for target in targets] == [
        datetime(2026, 11, 11, 8, 0).time(),
        datetime(2026, 11, 13, 8, 0).time(),
        datetime(2026, 11, 15, 20, 0).time(),
    ]
    # 考前就到期的题不动
    assert _due(already_due) == datetime(2026, 11, 2, 8, 0)


def test_pull_forward_is_idempotent(session, learner):
    """重复执行不会越拉越前：第二次 pulled == 0，到期日一字不变。"""
    set_exam(session, learner.id, "单元考", date(2026, 11, 10))
    items = [
        _mk_item(session, learner, due=datetime(2026, 11, 12, 8, 0), content=f"第{i}题：三加五")
        for i in range(4)
    ]
    first = plan_exam_pull_forward(session, learner.id, TODAY)
    assert first.pulled == 4
    snapshot = [_due(item) for item in items]

    second = plan_exam_pull_forward(session, learner.id, TODAY)
    assert second.pulled == 0
    assert [_due(item) for item in items] == snapshot

    # 换到第二天再跑一次：已经提前的题仍在考前，不会再被拉
    third = plan_exam_pull_forward(session, learner.id, TODAY + timedelta(days=1))
    assert third.pulled == 0
    assert [_due(item) for item in items] == snapshot


def test_pull_forward_spreads_evenly(session, learner):
    """5 道题、3 个可用日：均匀铺开（2 / 2 / 1），最早一天也有题。"""
    set_exam(session, learner.id, "单元考", date(2026, 11, 8))  # 今天 11-05 → 3 天
    items = [
        _mk_item(session, learner, due=datetime(2026, 11, 9, 9, 0) + timedelta(days=i), content=f"第{i}题")
        for i in range(5)
    ]
    result = plan_exam_pull_forward(session, learner.id, TODAY)
    assert result.pulled == 5
    days = [_due(item).date() for item in items]
    assert days == [
        date(2026, 11, 5),
        date(2026, 11, 5),
        date(2026, 11, 6),
        date(2026, 11, 6),
        date(2026, 11, 7),
    ]


# --------------------------------------------------------------- AC5 非冲刺期


def test_no_pull_outside_sprint(session, learner):
    """离考试还有 26 天：不动任何到期日。"""
    set_exam(session, learner.id, "期中考试", date(2026, 12, 1), subject="数学")
    item = _mk_item(session, learner, due=datetime(2026, 12, 2, 8, 0))

    result = plan_exam_pull_forward(session, learner.id, TODAY)

    assert (result.days_left, result.in_sprint, result.pulled) == (26, False, 0)
    assert _due(item) == datetime(2026, 12, 2, 8, 0)


def test_exam_day_has_no_room_to_pull(session, learner):
    """考试当天没有「考前一天」可用：只标冲刺，不改到期日。"""
    set_exam(session, learner.id, "单元考", date(2026, 11, 5))
    item = _mk_item(session, learner, due=datetime(2026, 11, 12, 8, 0))

    result = plan_exam_pull_forward(session, learner.id, TODAY)

    assert (result.days_left, result.in_sprint, result.pulled) == (0, True, 0)
    assert _due(item) == datetime(2026, 11, 12, 8, 0)


# --------------------------------------------------------------- AC6 过期


def test_expired_exam_inactive(session, learner):
    """考过之后不再生效：active_exam 为 None、不拉题，但记录还在。"""
    exam = set_exam(session, learner.id, "单元考", date(2026, 11, 1), subject="数学")
    item = _mk_item(session, learner, due=datetime(2026, 11, 12, 8, 0))

    assert active_exam(session, learner.id, TODAY) is None
    result = plan_exam_pull_forward(session, learner.id, TODAY)
    assert result.exam is None and result.pulled == 0
    assert _due(item) == datetime(2026, 11, 12, 8, 0)

    assert session.get(Exam, exam.id) is not None
    view = build_exam_view(session, learner.id, TODAY)
    assert view.active_id is None
    assert [(row.name, row.active, row.days_left) for row in view.rows] == [
        ("单元考", False, -4)
    ]


def test_future_exam_wins_over_expired(session, learner):
    """过期的考试不影响下一场：生效的是下一场未过期的。"""
    set_exam(session, learner.id, "单元考", date(2026, 11, 1))
    upcoming = set_exam(session, learner.id, "期中考试", date(2026, 11, 9))

    current = active_exam(session, learner.id, TODAY)
    assert current is not None and current.id == upcoming.id
    assert current.name == "期中考试"


# --------------------------------------------------------------- AC7 学科


def test_subject_filter(session, learner):
    """考试指定数学：只拉数学题，英语题原样。"""
    set_exam(session, learner.id, "单元考", date(2026, 11, 10), subject="数学")
    math_item = _mk_item(session, learner, subject="数学", due=datetime(2026, 11, 12, 8, 0))
    english_item = _mk_item(
        session,
        learner,
        subject="英语",
        due=datetime(2026, 11, 12, 8, 0),
        content="苹果",
    )

    result = plan_exam_pull_forward(session, learner.id, TODAY)

    assert result.pulled == 1
    assert _due(math_item).date() <= date(2026, 11, 9)
    assert _due(english_item) == datetime(2026, 11, 12, 8, 0)


def test_all_subjects_when_null(session, learner):
    """学科留空 = 全部学科：两科都拉。"""
    set_exam(session, learner.id, "期末考", date(2026, 11, 10))
    math_item = _mk_item(session, learner, subject="数学", due=datetime(2026, 11, 12, 8, 0))
    chinese_item = _mk_item(
        session,
        learner,
        subject="语文",
        due=datetime(2026, 11, 13, 8, 0),
        content="一( )当先",
    )

    result = plan_exam_pull_forward(session, learner.id, TODAY)

    assert result.pulled == 2
    assert _due(math_item).date() <= date(2026, 11, 9)
    assert _due(chinese_item).date() <= date(2026, 11, 9)


# --------------------------------------------------------------- AC8 窗口外


def test_out_of_window_untouched(session, learner):
    """窗口外（考后第 8 天及以后）不动；窗口最后一天照拉。"""
    set_exam(session, learner.id, "单元考", date(2026, 11, 10))
    outside = _mk_item(session, learner, due=datetime(2026, 11, 18, 9, 0), content="窗口外")
    edge = _mk_item(session, learner, due=datetime(2026, 11, 17, 23, 0), content="窗口最后一天")

    result = plan_exam_pull_forward(session, learner.id, TODAY)

    assert result.pulled == 1
    assert _due(outside) == datetime(2026, 11, 18, 9, 0)
    assert _due(edge).date() <= date(2026, 11, 9)


def test_archived_untouched(session, learner):
    """已出库的题不参与冲刺（它们已经不在练习池里）。"""
    set_exam(session, learner.id, "单元考", date(2026, 11, 10))
    archived = _mk_item(
        session,
        learner,
        due=datetime(2026, 11, 12, 8, 0),
        status=REVIEW_STATUS_ARCHIVED,
    )
    active = _mk_item(session, learner, due=datetime(2026, 11, 12, 8, 0), content="还在练")

    result = plan_exam_pull_forward(session, learner.id, TODAY)

    assert result.pulled == 1
    assert _due(archived) == datetime(2026, 11, 12, 8, 0)
    assert _due(active).date() <= date(2026, 11, 9)


# --------------------------------------------------------------- AC9/AC10 页面


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/web.db")
    with TestClient(app) as c:
        yield c, str(tmp_path / "web.db")


def _session(db_path: str):
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)

    class _Ctx:
        def __enter__(self):
            self.session = factory()
            return self.session

        def __exit__(self, *exc):
            self.session.close()
            engine.dispose()
            return False

    return _Ctx()


def _today() -> date:
    return datetime.now().astimezone().date()


def _add_exam(db_path: str, **fields) -> int:
    with _session(db_path) as session:
        learner_id = session.scalars(select(Learner).limit(1)).first().id
        exam = set_exam(session, learner_id, **fields)
        return exam.id


def _count(db_path: str, table: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        con.close()


def test_exam_page(client):
    c, db = client
    resp = c.get("/exam")
    assert resp.status_code == 200
    assert 'action="/exam"' in resp.text
    assert 'name="exam_date"' in resp.text
    assert "全部学科" in resp.text
    assert "还没有考试" in resp.text
    assert 'href="/practice"' in resp.text


def test_create_via_form(client):
    c, db = client
    exam_date = (_today() + timedelta(days=3)).isoformat()
    resp = c.post(
        "/exam",
        data={
            "name": "单元考",
            "exam_date": exam_date,
            "subject": "数学",
            "note": "第三单元 小数乘法",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/exam"
    assert _count(db, "exam") == 1

    page = c.get("/exam")
    assert "单元考" in page.text
    assert "还有 3 天" in page.text
    assert "数学" in page.text
    assert "第三单元 小数乘法" in page.text


def test_create_form_rejects_bad_date(client):
    c, db = client
    resp = c.post(
        "/exam",
        data={"name": "单元考", "exam_date": "下周三", "subject": ""},
    )
    assert resp.status_code == 200
    assert "考试日期请按 YYYY-MM-DD 填写" in resp.text
    assert _count(db, "exam") == 0


def test_delete_exam(client):
    c, db = client
    exam_id = _add_exam(db, name="单元考", exam_date=_today() + timedelta(days=3))
    assert _count(db, "exam") == 1

    resp = c.post("/exam/delete", data={"exam_id": str(exam_id)}, follow_redirects=False)
    assert resp.status_code == 303
    assert _count(db, "exam") == 0
    assert "单元考" not in c.get("/exam").text


def test_practice_shows_countdown(client):
    """考前 7 天内：倒计时条出现且带冲刺高亮。"""
    c, db = client
    _add_exam(db, name="单元考", exam_date=_today() + timedelta(days=3))

    page = c.get("/practice")
    assert "距离单元考还有 3 天" in page.text
    assert 'class="exam-bar sprint"' in page.text


def test_practice_outside_sprint_not_highlighted(client):
    """离考试还早：显示天数，但不高亮。"""
    c, db = client
    _add_exam(db, name="期中考试", exam_date=_today() + timedelta(days=20))

    page = c.get("/practice")
    assert "距离期中考试还有 20 天" in page.text
    assert 'class="exam-bar sprint"' not in page.text


def test_practice_without_exam(client):
    c, _ = client
    page = c.get("/practice")
    assert 'id="exam-bar"' not in page.text


def test_practice_after_exam_no_bar(client):
    """考过之后倒计时条消失，但 /exam 里记录还在（标已结束）。"""
    c, db = client
    _add_exam(db, name="单元考", exam_date=_today() - timedelta(days=1))

    assert 'id="exam-bar"' not in c.get("/practice").text
    page = c.get("/exam")
    assert "单元考" in page.text
    assert "已结束" in page.text


def test_practice_pulls_items_when_entering_sprint(client):
    """打开练习页就会拉题：不用另外点按钮。"""
    c, db = client
    _add_exam(db, name="单元考", exam_date=_today() + timedelta(days=3))
    with _session(db) as session:
        learner_id = session.scalars(select(Learner).limit(1)).first().id
        item = _mk_item(
            session,
            session.get(Learner, learner_id),
            due=datetime.combine(_today() + timedelta(days=4), datetime.min.time())
            + timedelta(hours=8),
        )
        item_id = item.id

    c.get("/practice")

    with _session(db) as session:
        moved = session.get(ErrorItem, item_id)
        assert moved.next_due_at.date() <= _today() + timedelta(days=2)
