"""0.2 卡9 — 教材基本库：导入幂等、勾选入库、搜索、空书。

测试数据是少量自造词条（真实 158 词不落仓库、不进种子）；文字一律中文，
口径与 `tests/test_web_app.py` 一致：中文必须原样存取。
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from cairn import book_service
from cairn.book_service import BookDataError
from cairn.migrations import SCHEMA_VERSION
from cairn.models import (
    Book,
    ErrorItem,
    Question,
    REVIEW_STATUS_NEW,
    Unit,
    Word,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.web import create_app

ENGLISH_BOOK = {
    "subject": "英语",
    "publisher": "上海教育出版社",
    "grade": 5,
    "semester": "上",
    "name": "沪教版（沪教牛津版·三起）五年级上册（测试样例）",
    "source_note": "测试样例，不是真实词表",
    "units": [
        {
            "unit_no": 1,
            "title": "My future",
            "words": [
                {"cue": "将来；未来", "answer": "future", "extra": "ˈfjuːtʃə(r)"},
                {"cue": "想要", "answer": "want", "extra": "wɒnt"},
            ],
        },
        {
            "unit_no": 2,
            "title": "Seeing the doctor",
            "words": [
                {"cue": "发烧", "answer": "fever", "extra": "ˈfiːvə(r)"},
            ],
        },
    ],
}

CHINESE_BOOK = {
    "subject": "语文",
    "publisher": "人民教育出版社",
    "grade": 5,
    "semester": "上",
    "name": "人教版 五年级上册 语文（测试样例）",
    "source_note": "测试样例，不是真实生字表",
    "units": [
        {
            "unit_no": 1,
            "title": "第一单元",
            "words": [{"cue": "píngguǒ", "answer": "苹果", "extra": "píngguǒ"}],
        }
    ],
}


# --------------------------------------------------------------------------- 装置


def _import(session, data=None):
    return book_service.import_book(session, data or ENGLISH_BOOK)


def _word_id(session, answer: str) -> int:
    return session.scalars(select(Word.id).where(Word.answer == answer)).one()


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/books.db")
    with TestClient(app) as c:
        yield c, str(tmp_path / "books.db")


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


def _learner_id(db_path: str) -> int:
    from cairn.models import Learner

    with _session(db_path) as session:
        return session.scalars(select(Learner.id).order_by(Learner.id.asc())).first()


def _seed_english(db_path: str) -> int:
    with _session(db_path) as session:
        return book_service.import_book(session, ENGLISH_BOOK).book_id


def _book_id_by_name(db_path: str, name: str) -> int:
    with _session(db_path) as session:
        return session.scalars(select(Book.id).where(Book.name == name)).one()


# --------------------------------------------------------------------------- AC1


def test_schema_three_tables_one_structure():
    """一套结构装三科：book/unit/word 三表，学科只是 book.subject 的一个值。"""
    engine = make_engine("sqlite://")
    init_db(engine)
    inspector = inspect(engine)
    assert {"book", "unit", "word"} <= set(inspector.get_table_names())
    assert {c["name"] for c in inspector.get_columns("book")} == {
        "id",
        "subject",
        "publisher",
        "grade",
        "semester",
        "name",
        "source_note",
        "imported_at",  # v10：词表导入时间（可空；空书/存量行 NULL）
    }
    assert {c["name"] for c in inspector.get_columns("unit")} == {
        "id",
        "book_id",
        "unit_no",
        "title",
    }
    assert {c["name"] for c in inspector.get_columns("word")} == {
        "id",
        "unit_id",
        "position",
        "cue",
        "answer",
        "extra",
    }

    factory = make_session_factory(engine)
    with factory() as session:
        _import(session, ENGLISH_BOOK)
        _import(session, CHINESE_BOOK)
        _import(
            session,
            {
                "subject": "数学",
                "publisher": "人民教育出版社",
                "grade": 5,
                "semester": "上",
                "name": "人教版 五年级上册 数学（测试样例）",
                "units": [
                    {
                        "unit_no": 1,
                        "words": [
                            {"cue": "小数乘整数", "answer": "0.8×3", "extra": "知识点"}
                        ],
                    }
                ],
            },
        )
        # 三科都落在同一组表里，按 subject 区分
        assert session.scalars(select(Book.subject).order_by(Book.subject)).all() == [
            "数学",
            "英语",
            "语文",
        ]
        for answer in ("苹果", "0.8×3"):
            assert session.scalars(select(Word).where(Word.answer == answer)).one().unit_id
    engine.dispose()


def test_migrations_v6_adds_book_tables(tmp_path):
    """只加表：新库直接 v6；v5 老库启动后补齐三张表、版本前进一格。"""
    db = tmp_path / "v6.db"
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    assert SCHEMA_VERSION >= 6  # 卡10 起是 7；这里只要求「教材表所在的 v6 已在」
    assert {"book", "unit", "word"} <= set(inspect(engine).get_table_names())
    engine.dispose()

    # 把库退回到「v5 且没有教材表」的样子，再让 init_db 走一遍
    engine = make_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE word")
        conn.exec_driver_sql("DROP TABLE unit")
        conn.exec_driver_sql("DROP TABLE book")
        conn.exec_driver_sql("UPDATE schema_version SET version = 5 WHERE id = 1")
    init_db(engine)
    assert {"book", "unit", "word"} <= set(inspect(engine).get_table_names())
    engine.dispose()

    import sqlite3

    con = sqlite3.connect(db)
    try:
        assert (
            con.execute("select version from schema_version where id = 1").fetchone()[0]
            == SCHEMA_VERSION
        )
    finally:
        con.close()


# --------------------------------------------------------------------------- AC2/AC3


def test_import_creates_book_units_words(session):
    result = _import(session)
    assert (result.created, result.skipped, result.units_created) == (3, 0, 2)
    assert result.summary() == "新增 3 条、跳过 0 条"
    assert session.scalars(select(Book)).one().subject == "英语"
    assert [unit.unit_no for unit in session.scalars(select(Unit).order_by(Unit.unit_no))] == [1, 2]
    first = session.scalars(select(Word).order_by(Word.position)).first()
    assert (first.position, first.cue, first.answer, first.extra) == (
        1,
        "将来；未来",
        "future",
        "ˈfjuːtʃə(r)",
    )


def test_import_idempotent(session):
    """同一本书连导两次：第二次全跳过，三张表行数不变。"""
    _import(session)
    second = _import(session)
    assert (second.created, second.skipped) == (0, 3)
    assert second.summary() == "新增 0 条、跳过 3 条"
    assert session.scalars(select(Book.id).where(Book.name == ENGLISH_BOOK["name"])).one() == second.book_id
    assert len(session.scalars(select(Book)).all()) == 1
    assert len(session.scalars(select(Unit)).all()) == 2
    assert len(session.scalars(select(Word)).all()) == 3


def test_dedupe_scope(session):
    """同单元内同答案只留一条；不同单元的同名答案各留一条。"""
    data = copy.deepcopy(ENGLISH_BOOK)
    data["units"][0]["words"].append({"cue": "未来（重复）", "answer": "Future"})
    data["units"][1]["words"].append({"cue": "未来（另一单元）", "answer": "future"})
    result = _import(session, data)
    assert (result.created, result.skipped) == (4, 1)
    unit1, unit2 = session.scalars(select(Unit).order_by(Unit.unit_no)).all()
    answers1 = session.scalars(
        select(Word.answer).where(Word.unit_id == unit1.id)
    ).all()
    answers2 = session.scalars(
        select(Word.answer).where(Word.unit_id == unit2.id)
    ).all()
    assert answers1 == ["future", "want"]  # Future 被同单元去重挡掉
    assert sorted(answers2) == ["fever", "future"]  # 另一个单元可以再出现


def test_import_rejects_bad_data_and_rolls_back(session):
    """一条坏数据整批回滚：不会留下半本书。"""
    data = copy.deepcopy(ENGLISH_BOOK)
    data["units"][1]["words"].append({"cue": "缺答案"})
    with pytest.raises(BookDataError):
        _import(session, data)
    assert session.scalars(select(Book)).all() == []
    assert session.scalars(select(Unit)).all() == []
    assert session.scalars(select(Word)).all() == []


# --------------------------------------------------------------------------- AC4/AC5


def test_add_words_creates_items(session, learner):
    _import(session)
    ids = [_word_id(session, "future"), _word_id(session, "want")]
    result = book_service.add_words_to_library(session, learner.id, ids)
    assert (result.created_count, result.skipped_count) == (2, 0)

    rows = session.execute(
        select(Question, ErrorItem).join(ErrorItem, ErrorItem.question_id == Question.id)
    ).all()
    assert len(rows) == 2
    for question, item in rows:
        assert question.subject == "英语"
        assert question.question_type == "单词"
        assert question.lang == "en"
        assert item.learner_id == learner.id
        assert item.review_status == REVIEW_STATUS_NEW  # 从新题（红）起步
        assert item.wrong_count == 1
    future = session.scalars(select(Question).where(Question.answer == "future")).one()
    assert future.content == "将来；未来"
    assert future.audio_text == "future"  # 英文词念它自己


def test_add_words_chinese_gets_zh(session, learner):
    """语文条目：subject/lang/question_type 按书与汉字判，拼音进朗读 cue。"""
    _import(session, CHINESE_BOOK)
    result = book_service.add_words_to_library(
        session, learner.id, [_word_id(session, "苹果")]
    )
    assert result.created_count == 1
    question = session.scalars(select(Question)).one()
    assert (question.subject, question.question_type, question.lang) == ("语文", "词语", "zh")
    assert question.content == "píngguǒ"
    assert question.answer == "苹果"
    assert question.audio_text == "píngguǒ"


def test_added_not_duplicated(session, learner):
    """同一批词再加一次：一条也不新增，错题库行数不变。"""
    _import(session)
    ids = [_word_id(session, "future"), _word_id(session, "want")]
    book_service.add_words_to_library(session, learner.id, ids)
    before = len(session.scalars(select(ErrorItem)).all())

    again = book_service.add_words_to_library(session, learner.id, ids)
    assert (again.created_count, again.skipped_count) == (0, 2)
    assert len(session.scalars(select(ErrorItem)).all()) == before
    assert len(session.scalars(select(Question)).all()) == before


# --------------------------------------------------------------------------- AC5/AC6 页面


def test_books_page(client):
    c, db = client
    book_id = _seed_english(db)
    page = c.get(f"/books?book_id={book_id}")
    assert page.status_code == 200
    assert "Unit 1" in page.text and "My future" in page.text
    assert "将来；未来" in page.text and "future" in page.text
    assert "加入错题库" in page.text
    # 左栏有种子空书（人教版五上语文/数学），只是没有条目
    assert "人教版 五年级上册 语文" in page.text
    assert "人教版 五年级上册 数学" in page.text


def test_page_marks_added(client):
    c, db = client
    book_id = _seed_english(db)
    with _session(db) as session:
        book_service.add_words_to_library(session, _learner_id(db), [_word_id(session, "future")])
    page = c.get(f"/books?book_id={book_id}")
    assert page.text.count("已加入") == 1
    # 已加入的那条复选框是 disabled，其余仍可勾
    assert page.text.count("disabled") == 1


def test_add_redirects_back(client):
    c, db = client
    book_id = _seed_english(db)
    with _session(db) as session:
        ids = [_word_id(session, "future"), _word_id(session, "fever")]
    response = c.post(
        "/books/add",
        data={
            "book_id": str(book_id),
            "q": "",
            "pick_count": "3",
            "word_0": str(ids[0]),
            "word_1": str(ids[1]),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/books?book_id={book_id}")
    assert "added=2" in location and "skipped=0" in location

    page = c.get(location)
    assert "新增 2 条，跳过 0 条" in page.text
    assert page.text.count("已加入") == 2

    # 再加一次：全部跳过，页面照样说实话
    again = c.post(
        "/books/add",
        data={"book_id": str(book_id), "q": "", "pick_count": "2",
              "word_0": str(ids[0]), "word_1": str(ids[1])},
        follow_redirects=True,
    )
    assert "新增 0 条，跳过 2 条" in again.text


def test_add_without_selection_reports_error(client):
    c, db = client
    book_id = _seed_english(db)
    response = c.post(
        "/books/add",
        data={"book_id": str(book_id), "pick_count": "0"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "没有选中的条目" in response.text


def test_search_across_units(session, learner):
    """搜索跨单元：一个字母能同时命中 Unit 1 和 Unit 2 的词。"""
    _import(session)
    view = book_service.build_books_view(session, learner.id, query="f")
    assert view.matched == 2
    assert [section.unit_no for section in view.units] == [1, 2]
    assert [word.answer for section in view.units for word in section.words] == [
        "future",
        "fever",
    ]
    # 按中文提示也能搜到
    assert book_service.build_books_view(session, learner.id, query="发烧").matched == 1
    assert book_service.build_books_view(session, learner.id, query="不存在的词").matched == 0


def test_empty_book(client):
    """语文/数学空书：页面 200，老实写「等导入」。"""
    c, db = client
    math_id = _book_id_by_name(db, "人教版 五年级上册 数学")
    page = c.get(f"/books?book_id={math_id}")
    assert page.status_code == 200
    assert "这本书还没有词表，等导入" in page.text
    assert "加入错题库" not in page.text

    with _session(db) as session:
        view = book_service.build_books_view(session, _learner_id(db), math_id)
    assert view.units == ()
    assert view.matched == 0
    assert view.empty_message == "这本书还没有词表，等导入"
    assert view.selected.subject == "数学"


def test_default_books_seeded_once(client):
    """启动种子只建一次：两本空书，重复起服务不变多。"""
    c, db = client
    with _session(db) as session:
        assert book_service.ensure_default_books(session) == 0
        names = session.scalars(select(Book.name).order_by(Book.name)).all()
        assert names == ["人教版 五年级上册 数学", "人教版 五年级上册 语文"]
        assert session.scalars(select(Word)).all() == []
