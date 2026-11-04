"""标错收进去卡（迁移 v16）：切题结果落 `scan_question`，家长点选后只入勾选题。

覆盖 AC2–AC6：

- AC2 迁移：v15 形状的库（有存量数据、没有 `scan_question`）升到 16，
  数据条数不变；
- AC3 落库幂等：同一 scan 连切两次，行数不变；
- AC4 部分勾选：勾 3 题 → `question` 恰好 +3，`scan_question.question_id` 回填；
- AC5 残题干：少于 4 字拒绝收进去，页面提示"第 N 题题干需人工补录"；
- AC6 重复提交：同一批勾选再提交一次，`question` 不再增加。

服务层用内存库直接测；页面用 TestClient（先落好切题行，GET 就不会去跑真 OCR）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn import scan_service
from cairn.migrations import SCHEMA_VERSION, apply_migrations
from cairn.models import Learner, Question, Scan, init_db, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.models.scan import ScanQuestion
from cairn.ocr.cut import QuestionBlock
from cairn.web import create_app

WIDTH, HEIGHT = 1000, 1400


def _block(
    no: int,
    stem: str,
    bbox: tuple[int, int, int, int] | None = None,
    *,
    inferred: bool = False,
) -> QuestionBlock:
    """一块切题结果；bbox 默认按题号往下排，方便断言叠加样式。"""
    return QuestionBlock(
        no=no,
        bbox=bbox or (20, no * 40, 480, no * 40 + 30),
        stem_text=stem,
        line_indexes=[no - 1],
        inferred_number=inferred,
    )


def _five_blocks() -> list[QuestionBlock]:
    return [
        _block(1, "1. 计算 3 + 4 ="),
        _block(2, "2. 计算 12 - 5 ="),
        _block(3, "3. 小明有 8 个苹果，吃了 3 个，还剩几个？"),
        _block(4, "4. 计算 6 × 7 ="),
        _block(5, "5. 一个长方形长 5 米，宽 3 米，面积是多少？"),
    ]


def _scan(session, learner, path: str = "scans/2026/11/shot.jpg") -> Scan:
    row = Scan(
        learner_id=learner.id,
        device_id="upload",
        device_name="浏览器上传",
        width=WIDTH,
        height=HEIGHT,
        captured_at=utcnow(),
        path=path,
        source="upload",
    )
    session.add(row)
    session.commit()
    return row


def _question_count(session) -> int:
    return len(session.scalars(select(Question.id)).all())


# ---------------------------------------------------------------- AC2 迁移 v16


def _make_v15_db(db_path) -> None:
    """v15 形状的库：其他表都在、`scan_question` 还没有、版本钉在 15。

    先用当前模型建库（表结构齐全、版本 16），再摘掉 v16 新增的那张表并把版本
    退回 15——这样既有的是「v15 老库」而不是「被 create_all 提前修好的库」。
    """
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE scan_question")
        conn.exec_driver_sql("UPDATE schema_version SET version = 15 WHERE id = 1")
    engine.dispose()


def _scalar(db_path, sql: str):
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def _columns(db_path, table: str) -> set[str]:
    con = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def test_v16_creates_scan_question_without_touching_data(tmp_path):
    """AC2：v15 老库升级 → 版本 16、新表在、字段齐、存量数据条数不变。"""
    db = tmp_path / "v16.db"
    _make_v15_db(db)
    assert _columns(db, "scan_question") == set()  # 前提：确实还没有这张表

    engine = make_engine(f"sqlite:///{db}")
    factory = make_session_factory(engine)
    with factory() as session:
        learner = Learner(name="老库孩子", grade_level="primary")
        session.add(learner)
        session.commit()
        scan = _scan(session, learner)
        session.add(
            Question(
                learner_id=learner.id,
                subject="数学",
                grade_level="primary",
                question_type="计算",
                content="存量题：2 + 2 =",
                answer="4",
                source="manual",
            )
        )
        session.commit()
        scan_id = scan.id
    engine.dispose()

    assert _scalar(db, "select count(*) from question") == 1
    assert _scalar(db, "select count(*) from scan") == 1

    engine = make_engine(f"sqlite:///{db}")
    try:
        assert apply_migrations(engine) == SCHEMA_VERSION
    finally:
        engine.dispose()

    assert _scalar(db, "select version from schema_version where id = 1") == SCHEMA_VERSION
    assert {"id", "scan_id", "no", "bbox_json", "stem_text", "is_wrong", "created_at",
            "question_id"} <= _columns(db, "scan_question")
    # 纯加表：存量数据一条不动
    assert _scalar(db, "select count(*) from question") == 1
    assert _scalar(db, "select count(*) from scan") == 1
    assert _scalar(db, "select content from question where id = 1") == "存量题：2 + 2 ="
    assert _scalar(db, f"select count(*) from scan where id = {scan_id}") == 1
    # 新表是空的，不凭空造行
    assert _scalar(db, "select count(*) from scan_question") == 0


# ---------------------------------------------------------------- 迁移 v18（推测题号）


def _make_v17_db(db_path) -> None:
    """v17 形状的库：scan_question 有存量行、**没有** inferred_number，版本钉在 17。

    先用当前模型建库并写入两条切题行，再把 v18 新增的那一列摘掉、版本退回 17——
    这样既有的是「v17 老库」而不是「被 create_all 提前补好列的库」。
    """
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = Learner(name="老库孩子", grade_level="primary")
            session.add(learner)
            session.commit()
            scan = _scan(session, learner)
            for no, stem in ((21, "21. 存量题甲"), (22, "22. 存量题乙")):
                session.add(
                    ScanQuestion(
                        scan_id=scan.id,
                        no=no,
                        bbox_json=json.dumps(
                            {"bbox": [0, no * 10, 100, no * 10 + 10],
                             "page": [WIDTH, HEIGHT], "angle": 0}
                        ),
                        stem_text=stem,
                        is_wrong=no == 22,
                    )
                )
            session.commit()
    finally:
        engine.dispose()
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("ALTER TABLE scan_question DROP COLUMN inferred_number")
        con.execute("UPDATE schema_version SET version = 17 WHERE id = 1")
        con.commit()
    finally:
        con.close()


def test_v18_adds_inferred_number_without_touching_data(tmp_path):
    """v18：v17 老库升级 → 版本 18、列在、存量行逐条不变、新列默认 0；重复升级幂等。"""
    db = tmp_path / "v18.db"
    _make_v17_db(db)
    assert "inferred_number" not in _columns(db, "scan_question")  # 前提：列确实还没有
    assert _scalar(db, "select count(*) from scan_question") == 2

    engine = make_engine(f"sqlite:///{db}")
    try:
        assert apply_migrations(engine) == SCHEMA_VERSION
        # 幂等：列已存在时 checkfirst 跳过，不报 duplicate column
        assert apply_migrations(engine) == SCHEMA_VERSION
    finally:
        engine.dispose()

    assert SCHEMA_VERSION >= 18
    assert _scalar(db, "select version from schema_version where id = 1") == SCHEMA_VERSION
    assert "inferred_number" in _columns(db, "scan_question")
    # 纯加列：存量行一条不增不减，字段内容原样
    assert _scalar(db, "select count(*) from scan_question") == 2
    assert _scalar(db, "select no from scan_question where id = 1") == 21
    assert _scalar(db, "select stem_text from scan_question where id = 1") == "21. 存量题甲"
    assert _scalar(db, "select is_wrong from scan_question where id = 2") == 1
    # 新列默认 0 = 题号是认出来的；没有一行是 NULL
    assert _scalar(db, "select count(*) from scan_question where inferred_number = 0") == 2
    assert _scalar(db, "select count(*) from scan_question where inferred_number is null") == 0


# ---------------------------------------------------------------- AC3 落库幂等


def test_store_questions_is_idempotent(session, learner):
    """AC3：连跑两次切题，`scan_question` 行数不变，且不覆盖已存在的行。"""
    scan = _scan(session, learner)

    first = scan_service.store_questions(
        session, scan.id, _five_blocks(), page_width=WIDTH, page_height=HEIGHT
    )
    assert [row.no for row in first] == [1, 2, 3, 4, 5]
    assert len(first) == 5

    # 第二次故意换一份"重切"结果（第 1 题题干改了一个字 + 多出一题 6）
    again = [
        _block(1, "1. 计算 3 + 4 =（重切）"),
        _block(6, "6. 计算 9 + 1 ="),
    ]
    second = scan_service.store_questions(
        session, scan.id, again, page_width=WIDTH, page_height=HEIGHT
    )
    # 已有题号原样保留，只有真正的新题号落行
    assert len(second) == 6
    assert session.scalar(
        select(ScanQuestion.stem_text).where(
            ScanQuestion.scan_id == scan.id, ScanQuestion.no == 1
        )
    ) == "1. 计算 3 + 4 ="

    # 再跑一遍同样的结果，行数不再变
    third = scan_service.store_questions(
        session, scan.id, _five_blocks() + [_block(6, "6. 计算 9 + 1 =")],
        page_width=WIDTH, page_height=HEIGHT,
    )
    assert len(third) == 6
    assert len(session.scalars(select(ScanQuestion.id)).all()) == 6


def test_store_questions_keeps_page_geometry(session, learner):
    """题框存的是摆正后坐标，页面尺寸与旋转角一起存（否则叠加会错位）。"""
    scan = _scan(session, learner)
    rows = scan_service.store_questions(
        session, scan.id, [_block(1, "1. 计算 3 + 4 =")],
        page_width=1400, page_height=1000, angle=90,
    )
    assert scan_service.page_geometry(rows) == (1400, 1000, 90)
    assert scan_service.row_bbox(rows[0]) == (20, 40, 480, 70)


def test_store_questions_persists_inferred_number(session, learner):
    """切题的 `QuestionBlock.inferred_number` 必须落库：以前只在内存对象上，落库就丢了。"""
    scan = _scan(session, learner)
    scan_service.store_questions(
        session,
        scan.id,
        [_block(21, "21. 计算 3 + 4 ="), _block(22, "22. 计算 12 - 5 =", inferred=True)],
        page_width=WIDTH,
        page_height=HEIGHT,
    )
    session.expire_all()  # 从库里重读，确认标记真的落了盘
    rows = {row.no: row for row in scan_service.list_questions(session, scan.id)}
    assert rows[21].inferred_number is False
    assert rows[22].inferred_number is True


# ---------------------------------------------------------------- AC4 部分勾选


def test_import_only_selected_questions(session, learner):
    """AC4：勾 3 题 → `question` 恰好 +3，且只有这 3 行回填 question_id。"""
    scan = _scan(session, learner)
    scan_service.store_questions(
        session, scan.id, _five_blocks(), page_width=WIDTH, page_height=HEIGHT
    )
    before = _question_count(session)

    result = scan_service.import_wrong_questions(
        session, learner.id, scan, [1, 3, 5, 7],
        subject="数学", question_type="计算",
    )

    assert result.created == (1, 3, 5)  # 7 不在这张卷子上：忽略，不凭空建题
    assert _question_count(session) == before + 3

    rows = {row.no: row for row in scan_service.list_questions(session, scan.id)}
    assert rows[1].question_id is not None
    assert rows[3].question_id is not None
    assert rows[5].question_id is not None
    assert rows[1].is_wrong is True and rows[5].is_wrong is True
    # 未勾选的两题：不收进去、不标错
    assert rows[2].question_id is None and rows[4].question_id is None
    assert rows[2].is_wrong is False and rows[4].is_wrong is False
    assert result.unselected == (2, 4)


def test_imported_question_has_no_fake_answer(session, learner):
    """扫描收进去没有答案：占位串只用于过 entry_service 校验，绝不留进库。"""
    scan = _scan(session, learner)
    scan_service.store_questions(
        session, scan.id, _five_blocks(), page_width=WIDTH, page_height=HEIGHT
    )
    scan_service.import_wrong_questions(
        session, learner.id, scan, [3], subject="数学", question_type="计算"
    )
    question = session.get(
        Question,
        {row.no: row for row in scan_service.list_questions(session, scan.id)}[3].question_id,
    )
    assert question.answer is None
    assert question.source == "scan"
    assert question.content == "3. 小明有 8 个苹果，吃了 3 个，还剩几个？"


# ---------------------------------------------------------------- AC5 残题干


def test_short_stem_is_refused(session, learner):
    """AC5（服务层）：题干少于 4 字不收进去，题号进 short 清单。"""
    scan = _scan(session, learner)
    scan_service.store_questions(
        session,
        scan.id,
        [_block(1, "1. 计算 3 + 4 ="), _block(2, "2."), _block(3, "")],
        page_width=WIDTH,
        page_height=HEIGHT,
    )
    before = _question_count(session)

    result = scan_service.import_wrong_questions(
        session, learner.id, scan, [1, 2, 3], subject="数学", question_type="计算"
    )

    assert result.created == (1,)
    assert result.short == (2, 3)
    assert _question_count(session) == before + 1
    session.expire_all()  # 从库里重读：is_wrong 必须真的落了盘，不是内存里的脏值
    rows = {row.no: row for row in scan_service.list_questions(session, scan.id)}
    assert rows[2].question_id is None and rows[3].question_id is None
    # 家长点过"错了"这件事仍然记下来，等人工补录
    assert rows[2].is_wrong is True and rows[3].is_wrong is True


# ---------------------------------------------------------------- AC6 重复提交


def test_repeat_submit_does_not_create_questions(session, learner):
    """AC6（服务层）：同一批勾选再提交，`question` 不再增加，题号进 existing。"""
    scan = _scan(session, learner)
    scan_service.store_questions(
        session, scan.id, _five_blocks(), page_width=WIDTH, page_height=HEIGHT
    )
    first = scan_service.import_wrong_questions(
        session, learner.id, scan, [1, 4], subject="数学", question_type="计算"
    )
    after_first = _question_count(session)
    assert first.created == (1, 4)
    assert after_first == 2

    second = scan_service.import_wrong_questions(
        session, learner.id, scan, [1, 4], subject="数学", question_type="计算"
    )
    assert second.created == ()
    assert second.existing == (1, 4)
    assert _question_count(session) == after_first
    session.expire_all()  # 第二次提交的 is_wrong/existing 判定也要落盘
    assert all(row.is_wrong for row in scan_service.list_questions(session, scan.id)
               if row.no in (1, 4))


# ---------------------------------------------------------------- 页面（AC4/AC5/AC6）


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as c:
        yield c, db


def _seed(client_db, blocks=None) -> tuple[int, list[int]]:
    """在 app 的库里落一张照片 + 一批切题行（落好行，GET 就不会去跑真 OCR）。"""
    db_path = client_db
    blocks = blocks if blocks is not None else _five_blocks()
    root = db_path.parent
    target = root / "scans" / "2026" / "11" / "shot.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")

    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            scan = _scan(session, learner)
            rows = scan_service.store_questions(
                session, scan.id, blocks, page_width=WIDTH, page_height=HEIGHT
            )
            return scan.id, [row.no for row in rows]
    finally:
        engine.dispose()


def _question_rows(db_path) -> int:
    return int(_scalar(db_path, "select count(*) from question"))


def test_page_lists_questions_with_boxes(client):
    """AC4 页面：顶部"共 N 题"、每题一个框、题干摘要有内容。"""
    c, db = client
    scan_id, nos = _seed(db)
    assert nos == [1, 2, 3, 4, 5]

    resp = c.get(f"/scans/{scan_id}/questions")

    assert resp.status_code == 200
    body = resp.text
    assert "共 5 题" in body
    assert body.count('class="box"') == 5
    assert 'name="no" value="3"' in body
    assert "小明有 8 个苹果" in body


def test_page_marks_only_inferred_numbers(client):
    """AC2/AC3/AC4/AC5：两条切题行只有 inferred 那条带标记，汇总数字正确，且照常可收进去。"""
    c, db = client
    blocks = [
        _block(1, "1. 计算 3 + 4 ="),
        _block(2, "2. 计算 12 - 5 =", inferred=True),
    ]
    scan_id, nos = _seed(db, blocks)
    assert nos == [1, 2]

    body = c.get(f"/scans/{scan_id}/questions").text

    # AC3 顶部汇总：只有真有推测题时才出现这半句
    assert "共 2 题，其中 1 题题号是推测的" in body
    # AC2 只有推测的那题题号带 ?，另一题不带
    assert "第 2 题?" in body
    assert "第 1 题?" not in body
    # AC2 那句提示只出现一次（对应唯一一条推测题）
    assert body.count("题号是猜的，请核对") == 1
    assert body.count('class="infer"') == 1

    # AC4 推测题照常可勾可收进去（标记只是提示）
    resp = c.post(
        f"/scans/{scan_id}/questions",
        data={"no": ["2"], "subject": "数学", "question_type": "计算"},
    )
    assert resp.status_code == 200
    assert "已收好 1 题" in resp.text
    assert _question_rows(db) == 1


def test_submit_imports_only_checked(client):
    """AC4/AC6 页面：勾 3 题收进去 +3；再提交同一批，题目数不变。"""
    c, db = client
    scan_id, _ = _seed(db)

    resp = c.post(
        f"/scans/{scan_id}/questions",
        data={"no": ["2", "3", "5"], "subject": "数学", "question_type": "计算"},
    )
    assert resp.status_code == 200
    assert "已收好 3 题" in resp.text
    assert _question_rows(db) == 3

    conn = sqlite3.connect(str(db))
    try:
        linked = conn.execute(
            "select no, question_id from scan_question order by no"
        ).fetchall()
    finally:
        conn.close()
    assert [no for no, qid in linked if qid is not None] == [2, 3, 5]

    again = c.post(
        f"/scans/{scan_id}/questions",
        data={"no": ["2", "3", "5"], "subject": "数学", "question_type": "计算"},
    )
    assert again.status_code == 200
    assert "已收好 0 题" in again.text
    assert "之前已收好" in again.text
    assert _question_rows(db) == 3  # AC6：重复提交不增题


def test_submit_warns_on_short_stem(client):
    """AC5 页面：残题干题被拒 + 页面提示"第 N 题题干需人工补录"。"""
    c, db = client
    blocks = [
        _block(1, "1. 计算 3 + 4 ="),
        _block(2, "2."),
        _block(3, "3. 计算 9 - 4 ="),
    ]
    scan_id, _ = _seed(db, blocks)

    resp = c.post(
        f"/scans/{scan_id}/questions",
        data={"no": ["1", "2", "3"], "subject": "数学", "question_type": "计算"},
    )

    assert resp.status_code == 200
    assert "已收好 2 题" in resp.text
    assert "第 2 题题干需人工补录" in resp.text
    assert _question_rows(db) == 2

    conn = sqlite3.connect(str(db))
    try:
        short_id = conn.execute(
            "select question_id from scan_question where no = 2"
        ).fetchone()[0]
    finally:
        conn.close()
    assert short_id is None


def test_unchecked_questions_are_not_imported(client):
    """未勾选的题不收进去（页面路径也要有这条钉子）。"""
    c, db = client
    scan_id, _ = _seed(db)

    resp = c.post(
        f"/scans/{scan_id}/questions",
        data={"no": ["4"], "subject": "数学", "question_type": "计算"},
    )
    assert resp.status_code == 200
    assert _question_rows(db) == 1

    conn = sqlite3.connect(str(db))
    try:
        linked = conn.execute(
            "select no from scan_question where question_id is not null order by no"
        ).fetchall()
    finally:
        conn.close()
    assert linked == [(4,)]


def test_page_overlay_follows_oriented_page(client):
    """侧拍（90°）时叠加要按"摆正后"的页面算，否则框会整体错位。"""
    c, db = client
    root = db.parent
    target = root / "scans" / "2026" / "11" / "shot.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")

    engine = make_engine(f"sqlite:///{db}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            scan = _scan(session, learner)
            # 原图 1000×1400 躺倒，摆正后是 1400×1000
            scan_service.store_questions(
                session,
                scan.id,
                [_block(1, "1. 计算 3 + 4 =", bbox=(140, 100, 700, 180))],
                page_width=1400,
                page_height=1000,
                angle=90,
            )
            scan_id = scan.id
    finally:
        engine.dispose()

    body = c.get(f"/scans/{scan_id}/questions").text

    assert "aspect-ratio: 1400 / 1000" in body
    # 原图按摆正后坐标系铺：宽 1000/1400、高 1400/1000，再反向转回 -90°
    assert "width:71.43%" in body and "height:140.00%" in body
    assert "rotate(-90deg)" in body
    assert "left:10.00%;top:10.00%;width:40.00%;height:8.00%;" in body


def test_page_reports_ocr_unavailable_instead_of_500(client, monkeypatch):
    """OCR 不可用时页面报一句话就够：家长不该看到堆栈（也没有行可标）。"""
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    c, db = client
    root = db.parent
    target = root / "scans" / "2026" / "11" / "shot.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")

    engine = make_engine(f"sqlite:///{db}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            scan_id = _scan(session, learner).id
    finally:
        engine.dispose()

    resp = c.get(f"/scans/{scan_id}/questions")

    assert resp.status_code == 200
    assert "识别失败" in resp.text
    assert "共 0 题" in resp.text


def test_page_404_for_unknown_scan(client):
    """别的孩子的/不存在的照片一律 404，不暴露库里的行。"""
    c, db = client
    _seed(db)
    assert c.get("/scans/9999/questions").status_code == 404
    assert c.post("/scans/9999/questions", data={"no": ["1"]}).status_code == 404
