"""卡 A · 一个家庭两个孩子，两套独立题库（数据层隔离）。

一个部署服务一个家庭，家庭里可能有多个孩子；`question` / `scan` 带 `learner_id`
之后，「A 录过的词」不能再拦 B、B 的同类题不能捞到 A 的题、导出/统计/取题/考试
提前各算各的。这里造固定的两个孩子 A、B 当夹具，逐条钉住这些边界。

公共内容（book / unit / word / concepts）不隔离，本文件不测它们的可见性。
"""

from __future__ import annotations

import sqlite3
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import pytest
from sqlalchemy import select

from cairn import stats_service
from cairn.anki_export import build_anki_package
from cairn.book_service import add_words_to_library
from cairn.capture.fake import FakeCameraBackend
from cairn.concept_service import tag_question
from cairn.entry_service import (
    DuplicateEntryError,
    EntryPayload,
    ValidationError,
    create_error_item,
    create_error_items,
)
from cairn.export_service import build_export
from cairn.import_service import load_backup
from cairn.models import (
    Book,
    ErrorItem,
    Learner,
    Question,
    Review,
    Unit,
    Word,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.models.review import RATING_AGAIN
from cairn.practice_service import fetch_practice, get_practice_item, submit_answer
from cairn.scan_service import capture_snapshot, get_scan, list_scans
from cairn.similar_service import find_similar_questions

NOW = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)


@pytest.fixture()
def family(session):
    """老大 A、老二 B —— 顺序固定，A 是最小 id。"""
    a = Learner(name="老大", grade_level="primary-3")
    b = Learner(name="老二", grade_level="primary-1")
    session.add_all([a, b])
    session.commit()
    return a, b


def _payload(content: str, answer: str, subject: str = "英语", qtype: str = "单词") -> EntryPayload:
    return EntryPayload(subject=subject, question_type=qtype, content=content, answer=answer)


def _enter(session, learner: Learner, content: str, answer: str, **kwargs) -> ErrorItem:
    subject = kwargs.pop("subject", "英语")
    qtype = kwargs.pop("qtype", "单词")
    return create_error_item(
        session,
        learner.id,
        _payload(content, answer, subject=subject, qtype=qtype),
        reject_duplicates=kwargs.pop("reject_duplicates", True),
    )


def test_web_grade_and_review_actions_cannot_change_another_learner(tmp_path):
    from fastapi.testclient import TestClient
    from cairn.web import create_app

    app = create_app(f"sqlite:///{tmp_path / 'web-isolation.db'}")
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            other = Learner(id=999, name="Other")
            session.add(other); session.flush()
            item = create_error_item(session, other.id, _payload("Other question", "other answer"))
            item.confidence = 0.3
            mismatched = ErrorItem(learner_id=app.state.learner_id, question_id=item.question_id, confidence=0.3)
            session.add(mismatched)
            session.commit(); item_id, mismatched_id = item.id, mismatched.id
        grade = client.post("/practice/grade", data={"item_id": str(item_id), "rating": "again"})
        assert grade.status_code == 200 and "不在你的题库里" in grade.text
        for route in ("confirm", "reject"):
            foreign = client.post(f"/review-queue/{route}", data={"item_id": str(item_id)})
            missing = client.post(f"/review-queue/{route}", data={"item_id": "999999"})
            assert foreign.status_code == missing.status_code == 409 and foreign.json() == missing.json()
            assert client.post(f"/review-queue/{route}", data={"item_id": str(mismatched_id)}).status_code == 409
        forged = client.post("/practice/grade", data={"item_id": str(mismatched_id), "rating": "again"})
        assert "不在你的题库里" in forged.text
        assert "Other question" not in client.get("/questions").text
        assert "Other question" not in client.get("/review-queue").text
        blocked = client.get("/export")
        assert blocked.status_code == 409 and "归属或关联异常" in blocked.text
        with app.state.session_factory() as session:
            item = session.get(ErrorItem, item_id)
            assert item.confidence == 0.3 and item.review_status == "new"
            assert session.scalar(select(Review.id).where(Review.error_item_id == item_id)) is None
            assert session.scalar(select(Review.id).where(Review.error_item_id == mismatched_id)) is None
            assert stats_service.practice_today(session, app.state.learner_id, now=NOW).total_items == 0
        app.state.learner_id = 999
        assert client.post("/practice/grade", data={"item_id": str(item_id), "rating": "good"}).status_code == 200
        with app.state.session_factory() as session:
            assert session.scalar(select(Review.id).where(Review.error_item_id == item_id)) is not None


def test_streamed_grade_uses_learner_at_start_of_request(tmp_path):
    import asyncio
    import httpx
    from fastapi.testclient import TestClient
    from cairn.web import create_app

    app = create_app(f"sqlite:///{tmp_path / 'grade-stream.db'}")
    with TestClient(app):
        with app.state.session_factory() as session:
            owner_id = app.state.learner_id
            session.add(Learner(id=999, name="Other")); session.flush()
            item = create_error_item(session, owner_id, _payload("Question A", "Answer A"))
            item_id = item.id
        async def send():
            async def form():
                yield f"item_id={item_id}&".encode()
                app.state.learner_id = 999
                yield b"rating=good"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/practice/grade", content=form(),
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert asyncio.run(send()).status_code == 200
        with app.state.session_factory() as session:
            assert session.scalar(select(Review.id).where(Review.error_item_id == item_id)) is not None


def test_foreign_scan_crop_is_not_exposed_through_owned_question(tmp_path):
    from fastapi.testclient import TestClient
    from cairn.models.scan import Scan, ScanQuestion
    from cairn.web import create_app

    app = create_app(f"sqlite:///{tmp_path / 'crop-isolation.db'}")
    (tmp_path / "foreign.png").write_bytes(b"private image")
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            owner_id = app.state.learner_id
            session.add(Learner(id=999, name="Other")); session.flush()
            item = create_error_item(session, owner_id, _payload("Own question", "Own answer"))
            scan = Scan(learner_id=999, device_id="d", device_name="d", width=1, height=1, path="foreign.png")
            session.add(scan); session.flush()
            session.add(ScanQuestion(scan_id=scan.id, no=1, bbox_json="{}", question_id=item.question_id,
                                     crop_path="foreign.png", stem_text="Own question"))
            session.commit(); question_id, item_id = item.question_id, item.id
            assert get_practice_item(session, owner_id, item_id).crop_url is None
        assert client.get(f"/questions/{question_id}/crop").status_code == 404


def _texts(document: dict) -> set[str]:
    return {row["content"] for row in document["question"]}


# ------------------------------------------------------------------ 查重


def test_duplicate_detection_does_not_cross_learners(session, family):
    """A 录过 "future"，B 再录同一个词不能被拦；A 自己重复录入仍然要拦。"""
    a, b = family
    payload = _payload("future", "future")

    first = create_error_item(session, a.id, payload, reject_duplicates=True)
    second = create_error_item(session, b.id, payload, reject_duplicates=True)

    assert first.question_id != second.question_id
    assert session.get(Question, first.question_id).learner_id == a.id
    assert session.get(Question, second.question_id).learner_id == b.id

    with pytest.raises(DuplicateEntryError):
        create_error_item(session, a.id, payload, reject_duplicates=True)


def test_batch_dedupe_does_not_cross_learners(session, family):
    """批量录入的「本孩子已有答案」也只数自己的题。"""
    a, b = family
    create_error_items(session, a.id, [_payload("apple", "apple")])

    result = create_error_items(session, b.id, [_payload("apple", "apple")])

    assert result.created_count == 1
    assert result.skipped == ()


# ---------------------------------------------------------------- 同类题


def test_similar_stays_inside_one_learner(session, family):
    """B 的同类题结果里不能出现 A 的题（哪怕共享同一个知识点）。"""
    a, b = family
    a1 = _enter(session, a, "1/2 + 1/2 = ?", "1", subject="数学", qtype="计算")
    a2 = _enter(session, a, "3/4 - 1/4 = ?", "1/2", subject="数学", qtype="计算")
    b1 = _enter(session, b, "2/3 + 1/3 = ?", "1", subject="数学", qtype="计算")

    for item in (a1, a2, b1):
        tag_question(session, session.get(Question, item.question_id), ["分数"])
    session.commit()

    assert find_similar_questions(session, b.id, b1.id) == []
    assert {row.question_id for row in find_similar_questions(session, a.id, a1.id)} == {
        a2.question_id
    }


# ------------------------------------------------------------------ 导出


def test_export_only_contains_one_learners_bank(session, family):
    a, b = family
    a1 = _enter(session, a, "future", "future")
    a2 = _enter(session, a, "past", "past")
    b1 = _enter(session, b, "apple", "apple")
    session.add(Review(error_item_id=a1.id, rating=RATING_AGAIN, reviewed_at=NOW))
    session.commit()

    doc_a = build_export(session, a.id, now=NOW)
    doc_b = build_export(session, b.id, now=NOW)

    assert [row["id"] for row in doc_a["question"]] == [a1.question_id, a2.question_id]
    assert {row["learner_id"] for row in doc_a["question"]} == {a.id}
    assert [row["id"] for row in doc_b["question"]] == [b1.question_id]
    assert {row["learner_id"] for row in doc_b["question"]} == {b.id}

    assert [row["id"] for row in doc_a["error_item"]] == [a1.id, a2.id]
    assert [row["id"] for row in doc_b["error_item"]] == [b1.id]
    assert len(doc_a["review"]) == 1
    assert doc_b["review"] == []
    assert [row["id"] for row in doc_a["learner"]] == [a.id]

    # 给 A 加题，B 的导出条数一分不变。
    _enter(session, a, "again", "again")
    assert len(build_export(session, b.id)["question"]) == 1


def test_anki_export_only_contains_one_learners_bank(session, family, tmp_path):
    a, b = family
    _enter(session, a, "future", "future")
    _enter(session, a, "past", "past")
    _enter(session, b, "apple", "apple")

    package = build_anki_package(session, b.id, now=NOW)
    (tmp_path / "b.anki2").write_bytes(
        zipfile.ZipFile(BytesIO(package)).read("collection.anki2")
    )
    connection = sqlite3.connect(tmp_path / "b.anki2")
    try:
        notes = connection.execute("select flds from notes").fetchall()
    finally:
        connection.close()

    assert len(notes) == 1
    assert "apple" in notes[0][0]
    assert "future" not in notes[0][0]


# ------------------------------------------------------------------ 统计


def test_stats_count_only_own_items(session, family):
    a, b = family
    a1 = _enter(session, a, "future", "future")
    _enter(session, a, "past", "past")
    _enter(session, a, "again", "again")
    b1 = _enter(session, b, "apple", "apple")
    session.add_all(
        [
            Review(error_item_id=a1.id, rating=RATING_AGAIN, reviewed_at=NOW),
            Review(error_item_id=a1.id, rating=RATING_AGAIN, reviewed_at=NOW),
            Review(error_item_id=b1.id, rating=RATING_AGAIN, reviewed_at=NOW),
        ]
    )
    session.commit()

    assert stats_service.practice_today(session, a.id, now=NOW).total_items == 3
    assert stats_service.practice_today(session, b.id, now=NOW).total_items == 1

    report_a = stats_service.build_stats(session, a.id, now=NOW)
    report_b = stats_service.build_stats(session, b.id, now=NOW)
    assert sum(row.total for row in report_a.distribution) == 2
    assert sum(row.total for row in report_b.distribution) == 1
    assert report_b.completion.has_data


# ------------------------------------------------------------------ 取题


def test_practice_and_reveal_are_per_learner(session, family):
    a, b = family
    a1 = _enter(session, a, "future", "future")
    b1 = _enter(session, b, "apple", "apple")

    assert [item.error_item_id for item in fetch_practice(session, a.id, NOW, limit=10)] == [
        a1.id
    ]
    assert [item.error_item_id for item in fetch_practice(session, b.id, NOW, limit=10)] == [
        b1.id
    ]

    assert get_practice_item(session, b.id, a1.id) is None
    with pytest.raises(ValidationError):
        submit_answer(session, b.id, a1.id, "future")
    assert submit_answer(session, a.id, a1.id, "future").answer == "future"


# ------------------------------------------------------- 写入必带归属


def test_batch_entry_and_books_write_the_current_learner(session, family):
    a, b = family
    result = create_error_items(
        session, b.id, [_payload("apple", "apple"), _payload("banana", "banana")]
    )
    assert result.created_count == 2
    assert {
        session.get(Question, item.question_id).learner_id for item in result.created
    } == {b.id}

    book = Book(subject="英语", publisher="人教版", grade=3, semester="上", name="三年级上册")
    session.add(book)
    session.flush()
    unit = Unit(book_id=book.id, unit_no=1, title="Unit 1")
    session.add(unit)
    session.flush()
    word = Word(unit_id=unit.id, cue="未来", answer="future", position=1)
    session.add(word)
    session.commit()

    added = add_words_to_library(session, a.id, [word.id])
    assert added.created_count == 1
    question = session.get(Question, added.created[0].question_id)
    assert question.learner_id == a.id
    assert question.content == "未来"


def test_import_old_backup_without_learner_id_lands_on_smallest_learner(session, family):
    """v8 及更早的备份没有 question.learner_id：恢复后必须补上，不能写进无主题。"""
    a, _ = family
    _enter(session, a, "future", "future")
    document = build_export(session, a.id, now=NOW)
    document["schema_version"] = 8
    for row in document["question"]:
        row.pop("learner_id", None)

    engine = make_engine("sqlite://")
    init_db(engine)
    try:
        with make_session_factory(engine)() as target:
            load_backup(target, document)
            questions = target.scalars(select(Question)).all()
            assert len(questions) == 1
            assert questions[0].learner_id == min(
                learner_id for learner_id in target.scalars(select(Learner.id))
            )
    finally:
        engine.dispose()


# ------------------------------------------------------------- 拍照读路径


def test_scans_are_only_visible_to_their_owner(tmp_path):
    """A、B 各拍一张（假后端）：列表里不串，拿对方 id 查一律当「不存在」。"""
    db_url = f"sqlite:///{tmp_path / 'family.db'}"
    engine = make_engine(db_url)
    init_db(engine)
    session = make_session_factory(engine)()
    try:
        a = Learner(name="老大", grade_level="primary-3")
        b = Learner(name="老二", grade_level="primary-1")
        session.add_all([a, b])
        session.commit()

        stamps = iter(
            [
                datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 31, 5, 0, tzinfo=timezone.utc),
            ]
        )
        backend = FakeCameraBackend(now=lambda: next(stamps))
        scan_a = capture_snapshot(backend, "fake:uvc-pro", session, a.id, db_url=db_url)
        scan_b = capture_snapshot(backend, "fake:uvc-pro", session, b.id, db_url=db_url)

        assert (scan_a.learner_id, scan_b.learner_id) == (a.id, b.id)
        assert [scan.id for scan in list_scans(session, a.id)] == [scan_a.id]
        assert [scan.id for scan in list_scans(session, b.id)] == [scan_b.id]

        assert get_scan(session, b.id, scan_a.id) is None
        assert get_scan(session, a.id, scan_b.id) is None
        assert get_scan(session, b.id, scan_b.id).id == scan_b.id
    finally:
        session.close()
        engine.dispose()


# ------------------------------------------- 交叉归属（错题记录与题面不同主人）


def _foreign_question(
    session,
    owner: Learner,
    *,
    content: str,
    subject: str = "数学",
    qtype: str = "计算",
) -> Question:
    """造一条属于 *owner* 的题；用来模拟「错题记录指向别人家题面」的脏数据。"""
    question = Question(
        learner_id=owner.id,
        subject=subject,
        grade_level="primary-3",
        question_type=qtype,
        content=content,
        answer="0",
    )
    session.add(question)
    session.commit()
    return question


def _item_on(session, owner: Learner, question: Question) -> ErrorItem:
    """把错题记录挂在 *owner* 名下，题面却是 *question*（可能是别人的）。"""
    item = ErrorItem(learner_id=owner.id, question_id=question.id)
    session.add(item)
    session.commit()
    return item


def test_practice_skips_item_whose_question_belongs_to_another_learner(session, family):
    """B 名下有一条错题记录，但题面是 A 的题：取题不能把它发给 B。"""
    a, b = family
    own = _enter(session, b, "apple", "apple")
    alien_question = _foreign_question(session, a, content="5 + 7 = ?")
    _item_on(session, b, alien_question)

    items = fetch_practice(session, b.id, NOW, limit=10)

    assert [item.error_item_id for item in items] == [own.id]


def test_stats_skip_item_whose_question_belongs_to_another_learner(session, family):
    """同一个交叉项：B 的统计不能把 A 的题（数学/计算）算进分布。"""
    a, b = family
    own = _enter(session, b, "apple", "apple")
    alien_question = _foreign_question(session, a, content="5 + 7 = ?")
    alien = _item_on(session, b, alien_question)
    session.add_all(
        [
            Review(error_item_id=own.id, rating=RATING_AGAIN, reviewed_at=NOW),
            Review(error_item_id=alien.id, rating=RATING_AGAIN, reviewed_at=NOW),
        ]
    )
    session.commit()

    report = stats_service.build_stats(session, b.id, now=NOW)

    assert [(row.question_type, row.total) for row in report.distribution] == [("单词", 1)]


def test_similar_rejects_foreign_question_under_own_item(session, family):
    """B 的错题记录指向 A 的题：同类题入口直接判「不属于当前 learner」。"""
    a, b = family
    alien_question = _foreign_question(session, a, content="2/3 + 1/3 = ?")
    alien = _item_on(session, b, alien_question)

    with pytest.raises(ValidationError):
        find_similar_questions(session, b.id, alien.id)


def test_similar_does_not_fish_foreign_questions(session, family):
    """共享知识点也不行：A 的题不能因为挂在 B 的错题记录下被当同类题捞出来。"""
    a, b = family
    source = _enter(session, b, "1/2 + 1/2 = ?", "1", subject="数学", qtype="计算")
    alien_question = _foreign_question(session, a, content="3/4 - 1/4 = ?")
    _item_on(session, b, alien_question)

    tag_question(session, session.get(Question, source.question_id), ["分数"])
    tag_question(session, alien_question, ["分数"])
    session.commit()

    assert find_similar_questions(session, b.id, source.id) == []
