"""0.2 卡2 AC3–AC5 — batch entry service (one transaction, dedupe by content)."""

from __future__ import annotations

import pytest

from cairn.entry_service import (
    EntryPayload,
    ValidationError,
    create_error_item,
    create_error_items,
)
from cairn.models import ErrorItem, Question


def _payload(content: str, answer: str, lang: str = "en") -> EntryPayload:
    return EntryPayload(
        subject="英语", question_type="单词", content=content, answer=answer, lang=lang
    )


def _counts(session) -> tuple[int, int]:
    return (
        len(session.query(Question).all()),
        len(session.query(ErrorItem).all()),
    )


class FlakyVerifier:
    """Succeeds until call N, then blows up — stands in for a mid-batch failure."""

    def __init__(self, fail_on: int):
        self.fail_on = fail_on
        self.calls = 0

    def verify(self, payload: EntryPayload) -> float:
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("verifier exploded")
        return 1.0


# ---- AC3 批量入库 ----

def test_batch_creates_question_and_item_per_word(session, learner):
    payloads = [
        _payload("apple", "苹果"),
        _payload("banana", "香蕉"),
        _payload("桌子", "zhuōzi", lang="zh"),
    ]

    result = create_error_items(session, learner.id, payloads)

    assert result.created_count == 3
    assert result.skipped_count == 0
    assert _counts(session) == (3, 3)

    rows = {
        q.content: (q.answer, q.lang, q.subject, q.question_type)
        for q in session.query(Question).all()
    }
    assert rows["apple"] == ("苹果", "en", "英语", "单词")
    assert rows["桌子"] == ("zhuōzi", "zh", "英语", "单词")

    items = session.query(ErrorItem).all()
    assert all(i.learner_id == learner.id for i in items)
    assert all(i.review_status == "new" and i.wrong_count == 1 for i in items)
    assert all(i.confidence == 1.0 for i in items)
    assert [i.id for i in result.created] == [i.id for i in items]


def test_batch_with_empty_list_writes_nothing(session, learner):
    result = create_error_items(session, learner.id, [])
    assert (result.created_count, result.skipped_count) == (0, 0)
    assert _counts(session) == (0, 0)


# ---- AC4 重复被跳过并计数 ----

def test_duplicate_content_skipped_and_counted(session, learner):
    create_error_item(session, learner.id, _payload("apple", "苹果"))

    result = create_error_items(
        session,
        learner.id,
        [_payload("apple", "苹果"), _payload("pear", "梨")],
    )

    assert result.created_count == 1
    assert result.skipped_count == 1
    assert result.skipped == ("apple",)
    assert _counts(session) == (2, 2)
    assert [q.content for q in session.query(Question).all()] == ["apple", "pear"]


def test_duplicate_inside_one_batch_skipped(session, learner):
    result = create_error_items(
        session,
        learner.id,
        [_payload("apple", "苹果"), _payload("APPLE", "苹果"), _payload("pear", "梨")],
    )

    assert result.created_count == 2
    assert result.skipped_count == 1
    assert _counts(session) == (2, 2)


def test_same_content_for_another_learner_is_not_a_duplicate(session, learner):
    from cairn.models import Learner

    other = Learner(name="other-child")
    session.add(other)
    session.commit()
    create_error_item(session, other.id, _payload("apple", "苹果"))

    result = create_error_items(session, learner.id, [_payload("apple", "苹果")])

    assert (result.created_count, result.skipped_count) == (1, 0)
    assert _counts(session) == (2, 2)


# ---- AC5 中途失败整批回滚 ----

def test_mid_batch_failure_rolls_back_everything(session, learner):
    verifier = FlakyVerifier(fail_on=2)

    with pytest.raises(RuntimeError, match="exploded"):
        create_error_items(
            session,
            learner.id,
            [_payload("apple", "苹果"), _payload("banana", "香蕉"), _payload("pear", "梨")],
            verify_entry=True,
            entry_verifier=verifier,
        )

    assert verifier.calls == 2  # 第 1 条已经写过，第 2 条炸掉
    assert _counts(session) == (0, 0)


def test_invalid_payload_mid_batch_rolls_back(session, learner):
    with pytest.raises(ValidationError, match="answer"):
        create_error_items(
            session,
            learner.id,
            [_payload("apple", "苹果"), _payload("banana", "")],
        )

    assert _counts(session) == (0, 0)


def test_batch_rejects_unknown_learner(session):
    with pytest.raises(ValidationError, match="learner"):
        create_error_items(session, 999, [_payload("apple", "苹果")])
    assert _counts(session) == (0, 0)


# ---- 0.2 卡3 AC8：批量入库写入朗读 cue ----

def test_batch_persists_audio_text(session, learner):
    payloads = [
        EntryPayload(
            subject="英语",
            question_type="单词",
            content="apple",
            answer="苹果",
            lang="en",
            audio_text="apple",
        ),
        EntryPayload(
            subject="语文",
            question_type="词语",
            content="桌子",
            answer="zhuōzi",
            lang="zh",
            audio_text="zhuōzi",
        ),
        # 没填 cue 的（数学题）留空，不硬造
        EntryPayload(
            subject="数学", question_type="计算", content="3 + 5 = ?", answer="8"
        ),
    ]

    create_error_items(session, learner.id, payloads)

    rows = {
        q.content: q.audio_text for q in session.query(Question).all()
    }
    assert rows == {"apple": "apple", "桌子": "zhuōzi", "3 + 5 = ?": None}
