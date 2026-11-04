"""0.2 卡6 — 录入时模糊查重（中文数据，全部走真服务/真路由）。

三层各测一段：
- 纯函数：规范化、相似度、命中判定（不碰 DB）
- 服务层：单条录入完全相同被拦、近似题照常入库、跨 learner 不算重复
- web：单条相近题给「仍然添加」、批量预览逐行标注且默认不勾（含批内互查）
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cairn import duplicate
from cairn.entry_service import (
    DuplicateEntryError,
    EntryPayload,
    create_error_item,
    create_error_items,
    find_entry_duplicate,
)
from cairn.models import ErrorItem, Question
from cairn.web import create_app

MATH_STEM = "小明有25个苹果，吃了4个，还剩几个？"
MATH_STEM_NEAR = "小明有25个苹果，吃了5个，还剩几个？"
MATH_ANSWER = "21"


def _math(content: str = MATH_STEM, answer: str = MATH_ANSWER) -> EntryPayload:
    return EntryPayload(
        subject="数学", question_type="应用题", content=content, answer=answer
    )


def _word(content: str, answer: str, lang: str = "en") -> EntryPayload:
    return EntryPayload(
        subject="英语", question_type="单词", content=content, answer=answer, lang=lang
    )


def _counts(session) -> tuple[int, int]:
    return (len(session.query(Question).all()), len(session.query(ErrorItem).all()))


# ---- AC1 规范化 ----


def test_normalize_fullwidth_and_case():
    """全角字母数字转半角，英文小写（NFKC + casefold）。"""
    assert duplicate.normalize_text("ＡＢＣ　１２３") == "abc123"
    assert duplicate.normalize_text("Apple") == "apple"


def test_normalize_drops_punctuation_and_spaces():
    """中文标点、句读、空白都不参与比较。"""
    assert duplicate.normalize_text("  三加五，等于多少？  ") == "三加五等于多少"
    assert duplicate.normalize_text("12 + 15 = ?") == "12+15="
    assert duplicate.normalize_text("“苹果”、《香蕉》。") == "苹果香蕉"


def test_normalize_keeps_math_operators():
    """运算符和小数点不能当标点去掉：3-5 与 35 是两道题。"""
    assert duplicate.normalize_text("25×4=?") == "25×4="
    assert duplicate.normalize_text("3-5=?") != duplicate.normalize_text("35=?")
    assert duplicate.normalize_text("3.5×2") != duplicate.normalize_text("35×2")


def test_normalize_none_and_blank():
    assert duplicate.normalize_text(None) == ""
    assert duplicate.normalize_text("   ") == ""


def test_similarity_threshold_is_085():
    assert duplicate.SIMILARITY_THRESHOLD == 0.85
    assert duplicate.similarity("苹果", "苹果") == 1.0
    assert duplicate.similarity(MATH_STEM, MATH_STEM_NEAR) >= 0.85
    assert duplicate.similarity("苹果", "香蕉") < 0.85


# ---- AC3 完全相同被拦 ----


def test_exact_repeat_is_blocked_and_writes_nothing(session, learner):
    first = create_error_item(session, learner.id, _math(), reject_duplicates=True)

    with pytest.raises(DuplicateEntryError) as excinfo:
        create_error_item(session, learner.id, _math(), reject_duplicates=True)

    message = str(excinfo.value)
    assert "这道题已经录过" in message
    assert f"第 {first.question_id} 条" in message
    assert re.search(r"录入于 \d+ 月 \d+ 日", message)
    assert "0.9" not in message and "%" not in message  # 不露相似度分数
    assert _counts(session) == (1, 1)


def test_exact_repeat_ignores_spacing_and_punctuation(session, learner):
    create_error_item(
        session,
        learner.id,
        _math("25×4=?", "100"),
        reject_duplicates=True,
    )

    with pytest.raises(DuplicateEntryError):
        create_error_item(
            session, learner.id, _math("２５ × ４ ＝ ？", "100"), reject_duplicates=True
        )

    assert _counts(session) == (1, 1)


def test_same_answer_different_question_is_not_an_exact_duplicate(session, learner):
    """3+5=? 和 2+6=? 答案都是 8，但不是同一道题。"""
    create_error_item(session, learner.id, _math("3+5=?", "8"), reject_duplicates=True)

    with pytest.raises(DuplicateEntryError):
        create_error_item(
            session, learner.id, _math("3 + 5 = ?", "8"), reject_duplicates=True
        )

    create_error_item(session, learner.id, _math("2+6=?", "8"), reject_duplicates=True)
    assert _counts(session) == (2, 2)


def test_same_question_for_another_learner_is_not_a_duplicate(session, learner):
    from cairn.models import Learner

    other = Learner(name="另一个孩子")
    session.add(other)
    session.commit()

    create_error_item(session, other.id, _math(), reject_duplicates=True)
    create_error_item(session, learner.id, _math(), reject_duplicates=True)

    assert _counts(session) == (2, 2)


# ---- AC4 高度相似只提示不拦 ----


def test_similar_question_is_flagged_but_still_persisted(session, learner):
    create_error_item(session, learner.id, _math(), reject_duplicates=True)

    match = find_entry_duplicate(session, learner.id, _math(MATH_STEM_NEAR, "20"))
    assert match is not None
    assert match.kind == duplicate.KIND_SIMILAR
    assert match.candidate.content == MATH_STEM

    # 服务层不拦近似题：家长点过「仍然添加」走的就是这条路径
    create_error_item(
        session, learner.id, _math(MATH_STEM_NEAR, "20"), reject_duplicates=True
    )
    assert _counts(session) == (2, 2)


# ---- AC5 不相关不提示 ----


def test_unrelated_question_is_silent(session, learner):
    create_error_item(session, learner.id, _math(), reject_duplicates=True)

    assert find_entry_duplicate(session, learner.id, _word("apple", "苹果")) is None
    create_error_item(session, learner.id, _word("apple", "苹果"), reject_duplicates=True)
    assert _counts(session) == (2, 2)


# ---- AC7 批内互查（纯函数层） ----


def test_batch_rows_are_checked_against_each_other():
    """批内第二条和第一条近似 → 命中，且指向「本批里前面那条」。"""
    candidates = [duplicate.Candidate(content="苹果", answer="apple")]

    match = duplicate.find_duplicate("苹果", "app1e", candidates)
    assert match is not None
    assert match.kind == duplicate.KIND_SIMILAR
    assert match.candidate.question_id is None
    assert "本批" in duplicate.describe_exact(
        duplicate.DuplicateMatch(duplicate.KIND_EXACT, candidates[0], 1.0)
    )


def test_preview_note_uses_row_number_and_no_score():
    candidates = [duplicate.Candidate(content="25×4=?", answer="100", question_id=7)]
    match = duplicate.find_duplicate("25×4=?", "100", candidates)

    note = duplicate.describe_preview_row(match, 3, "25×4=?")
    assert note == "第 3 条：25×4=?（已录过）"


# ---- web：单条录入路径 ----


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/dup.db")
    with TestClient(app) as c:
        yield c, str(tmp_path / "dup.db")


def _count(db_path: str, table: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        con.close()


def _form(content: str, answer: str = MATH_ANSWER, **extra) -> dict[str, str]:
    data = {
        "subject": "数学",
        "question_type": "应用题",
        "content": content,
        "answer": answer,
    }
    data.update(extra)
    return data


def test_web_exact_repeat_shows_message_and_writes_nothing(client):
    c, db = client
    assert "已收进去" in c.post("/entry", data=_form(MATH_STEM)).text

    resp = c.post("/entry", data=_form(MATH_STEM))

    assert resp.status_code == 200
    assert "这道题已经录过" in resp.text
    assert re.search(r"第 \d+ 条，录入于 \d+ 月 \d+ 日", resp.text)
    assert _count(db, "question") == 1


def test_web_similar_question_warns_then_force_adds(client):
    c, db = client
    c.post("/entry", data=_form(MATH_STEM))

    warn = c.post("/entry", data=_form(MATH_STEM_NEAR, "20"))

    assert warn.status_code == 200
    assert "几乎一样" in warn.text
    assert 'name="force" value="1"' in warn.text
    assert "仍然添加" in warn.text
    assert _count(db, "question") == 1  # 还没写库

    forced = c.post("/entry", data=_form(MATH_STEM_NEAR, "20", force="1"))
    assert "已收进去" in forced.text
    assert _count(db, "question") == 2


def test_web_unrelated_question_has_no_warning(client):
    c, db = client
    c.post("/entry", data=_form(MATH_STEM))

    resp = c.post("/entry", data=_form("一箱牛奶有12盒，3箱共几盒？", "36"))

    assert "已收进去" in resp.text
    assert "几乎一样" not in resp.text
    assert _count(db, "question") == 2


# ---- web：批量预览逐行标注（含批内互查） ----


def _include_checked(html: str, index: int) -> bool:
    match = re.search(
        rf'<input type="checkbox" name="include_{index}"[^>]*>', html
    )
    assert match is not None, f"预览里没有第 {index} 行的勾选框"
    return "checked" in match.group(0)


def test_preview_marks_duplicate_row_unchecked(client):
    c, db = client
    c.post("/entry", data=_form("苹果", "apple"))  # 库里已有「苹果 / apple」

    resp = c.post(
        "/entry/batch/preview",
        data={"pasted": "apple 苹果\nbanana 香蕉"},
    )

    assert "第 1 条：苹果（已录过）" in resp.text
    assert _include_checked(resp.text, 0) is False
    assert _include_checked(resp.text, 1) is True
    assert _count(db, "question") == 1  # 预览不写库


def test_preview_catches_duplicates_inside_one_paste(client):
    """批内互查：第二条只是第一条打错一个字母，也要标出来。"""
    c, _ = client

    resp = c.post("/entry/batch/preview", data={"pasted": "apple 苹果\napp1e 苹果"})

    assert "第 2 条：苹果（疑似重复：苹果）" in resp.text
    assert _include_checked(resp.text, 0) is True
    assert _include_checked(resp.text, 1) is False


def test_batch_commit_still_allows_forced_similar_rows(session, learner):
    """预览里勾回来的近似行照常入库（批量不拦相似，只拦完全相同的 answer）。"""
    create_error_item(session, learner.id, _word("苹果", "apple"))

    result = create_error_items(
        session, learner.id, [_word("苹果", "app1e"), _word("香蕉", "banana")]
    )

    assert (result.created_count, result.skipped_count) == (2, 0)
    assert _counts(session) == (3, 3)


def test_message_shows_local_date_for_naive_utc_timestamp():
    """SQLite 读回来的是 naive UTC；按本地时区显示，别把昨晚录的写成前一天。"""
    from datetime import datetime, timezone

    stored = datetime(2026, 10, 30, 16, 30)  # naive UTC（本地 +8 已是 31 日）
    match = duplicate.DuplicateMatch(
        duplicate.KIND_EXACT,
        duplicate.Candidate(content="25×4=?", answer="100", question_id=3, created_at=stored),
        1.0,
    )
    local = stored.replace(tzinfo=timezone.utc).astimezone()

    assert f"录入于 {local.month} 月 {local.day} 日" in duplicate.describe_exact(match)
