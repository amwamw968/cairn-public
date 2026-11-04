"""B4 web-layer tests: route -> service -> page wiring via FastAPI TestClient.

Service-layer behavior is already locked by the existing 42 tests; these only
assert that the three screens call the right services and render the right
faces (notably: the answer must never appear before reveal).

All sample data is Chinese on purpose: the urlencoded body path used to be
decoded as latin-1, which mojibaked Chinese while ASCII-only tests stayed
green.
"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn.entry_service import EntryPayload, create_error_item, create_error_items
from cairn.models import Learner, make_engine, make_session_factory
from cairn.web import create_app

SUBJECT = "数学"
QTYPE = "计算"
CONTENT = "三加五等于多少？"
ANSWER = "八"


class _StubVerifier:
    def verify(self, payload: EntryPayload) -> float:
        return 0.5


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


def _learner_id(db_path: str) -> int:
    with _session(db_path) as session:
        return session.scalars(select(Learner).limit(1)).first().id


def _count(db_path: str, table: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        con.close()


def _entry_form(**overrides) -> dict[str, str]:
    data = {
        "subject": SUBJECT,
        "question_type": QTYPE,
        "content": CONTENT,
        "answer": ANSWER,
    }
    data.update(overrides)
    return data


def test_root_redirects_to_entry(client):
    """G2-1 升级（断言只强不弱）：`/` 不再 303 → /entry，直接 200 渲 v3「今天」页。

    旧断言（303 + location=/entry）被新页面顶掉，升级为：不重定向、是今天页空态、
    且不再是那张录入表单（旧「上来就填表」正是 v3 要换掉的）。
    """
    c, _ = client
    resp = c.get("/", follow_redirects=False)
    assert resp.status_code == 200
    assert "location" not in {k.lower() for k in resp.headers}
    assert "Cairn 凯恩" in resp.text
    assert "今天还没有要复习的题" in resp.text
    assert 'action="/entry"' not in resp.text


def test_entry_page_renders(client):
    c, _ = client
    resp = c.get("/entry")
    assert resp.status_code == 200
    assert 'action="/entry"' in resp.text
    assert 'name="subject"' in resp.text
    assert 'href="/practice"' in resp.text
    # G2-6：旧导航整排撤；确认流程入口在待处理卡/流程里，不在录入页
    assert 'href="/export"' in resp.text


def test_ac1_entry_creates_item_and_visible_in_practice(client):
    c, db = client
    resp = c.post("/entry", data=_entry_form())
    assert resp.status_code == 200
    assert "已收进去" in resp.text
    assert _count(db, "error_item") == 1

    practice = c.get("/practice")
    assert CONTENT in practice.text


def test_ac1_chinese_stored_verbatim(client):
    """CHANGES-1: Chinese must survive the form path byte-for-byte."""
    c, db = client
    c.post("/entry", data=_entry_form(analysis="把 3 和 5 合起来数"))
    con = sqlite3.connect(db)
    try:
        subject, content, answer, analysis = con.execute(
            "select subject, content, answer, analysis from question"
        ).fetchone()
    finally:
        con.close()
    assert (subject, content, answer, analysis) == (
        SUBJECT,
        CONTENT,
        ANSWER,
        "把 3 和 5 合起来数",
    )

    page = c.get("/practice")
    assert SUBJECT in page.text
    assert QTYPE in page.text
    assert CONTENT in page.text


def test_ac2_entry_missing_field_rejected_no_write(client):
    c, db = client
    data = _entry_form()
    del data["answer"]
    resp = c.post("/entry", data=data)
    assert resp.status_code == 200
    assert "missing required field: answer" in resp.text
    assert _count(db, "error_item") == 0


def test_entry_error_retry_keeps_empty_analysis_empty(client):
    c, db = client
    page = c.get("/entry")
    assert '类别（必填）' in page.text
    assert re.search(r'name="question_type"\s+required', page.text)
    response = c.post("/entry", data=_entry_form(question_type="", analysis=""))
    assert "missing required field: question_type" in response.text
    assert re.search(r'<textarea[^>]*name="analysis"[^>]*>\s*</textarea>', response.text)
    assert _count(db, "question") == 0
    response = c.post("/entry", data=_entry_form(analysis=""))
    assert "已收进去" in response.text
    with sqlite3.connect(db) as con:
        assert con.execute("select analysis from question").fetchone()[0] is None


def test_ac3_ac4_answer_hidden_until_reveal(client):
    # 非听写路径（数学题无 audio_text）：题目照旧显示，答案不作文本渲染。
    # 听写路径见下方「听写模式」用例：那里念的是 cue，answer 同样不作文本渲染。
    c, _ = client
    c.post("/entry", data=_entry_form())
    page = c.get("/practice")
    assert CONTENT in page.text
    assert ANSWER not in page.text  # 先答后揭示：初始页面不露答案

    revealed = c.post(
        "/practice/reveal", data={"item_id": "1", "learner_answer": "七"}
    )
    assert revealed.status_code == 200
    assert ANSWER in revealed.text


def test_practice_empty_message(client):
    c, _ = client
    resp = c.get("/practice")
    # G2-5：空练习换 v3 三段式口径（旧「今日无题」撤）
    assert "今天没有要练的题了" in resp.text


def test_ac5_grade_updates_state_and_counts_wrong(client):
    c, db = client
    c.post("/entry", data=_entry_form())
    # 判题确定性层：reveal 时自动判分（七=7 ≠ 八=8 → 错 → 自动写 again review）
    c.post("/practice/reveal", data={"item_id": "1", "learner_answer": "七"})
    graded = c.post("/practice/grade", data={"item_id": "1", "rating": "again"})
    assert graded.status_code == 200
    assert "判分成功" in graded.text

    con = sqlite3.connect(db)
    try:
        wrong_count, = con.execute(
            "select wrong_count from error_item where id = 1"
        ).fetchone()
        reviews, = con.execute("select count(*) from review").fetchone()
    finally:
        con.close()
    assert wrong_count == 3  # 录入初始 1 + reveal 自动 again +1 + 手工 again +1
    assert reviews == 2  # reveal 自动判分 1 + 手工判分 1


def test_ac5_grade_rejects_invalid_rating(client):
    c, db = client
    c.post("/entry", data=_entry_form())
    resp = c.post("/practice/grade", data={"item_id": "1", "rating": "ok"})
    assert resp.status_code == 200
    assert "rating must be one of again/hard/good/easy" in resp.text
    assert _count(db, "review") == 0


def test_ac6_review_queue_confirm(client):
    c, db = client
    with _session(db) as session:
        create_error_item(
            session,
            _learner_id(db),
            EntryPayload(subject=SUBJECT, question_type=QTYPE, content=CONTENT, answer=ANSWER),
            verify_entry=True,
            entry_verifier=_StubVerifier(),
        )

    queue = c.get("/review-queue")
    assert CONTENT in queue.text
    assert "机器不太确定" in queue.text

    confirmed = c.post(
        "/review-queue/confirm", data={"item_id": "1"}, follow_redirects=True
    )
    assert confirmed.status_code == 200
    assert CONTENT not in confirmed.text  # 出队

    con = sqlite3.connect(db)
    try:
        confidence, = con.execute(
            "select confidence from error_item where id = 1"
        ).fetchone()
    finally:
        con.close()
    assert confidence == 1.0


def test_ac7_review_queue_reject_archives(client):
    c, db = client
    with _session(db) as session:
        create_error_item(
            session,
            _learner_id(db),
            EntryPayload(subject=SUBJECT, question_type=QTYPE, content=CONTENT, answer=ANSWER),
            verify_entry=True,
            entry_verifier=_StubVerifier(),
        )

    rejected = c.post(
        "/review-queue/reject", data={"item_id": "1"}, follow_redirects=True
    )
    assert rejected.status_code == 200
    assert CONTENT not in rejected.text  # 出队

    practice = c.get("/practice")
    assert CONTENT not in practice.text  # archived 不进取题池

    con = sqlite3.connect(db)
    try:
        status, = con.execute(
            "select review_status from error_item where id = 1"
        ).fetchone()
    finally:
        con.close()
    assert status == "archived"


# ---- 0.2 卡2 AC6：整段粘贴录入 ----

PASTE = (
    "1. apple 苹果\n"
    "2、banana 香蕉\n"
    "Unit 3 pear 梨\n"
    "apple 苹果\n"
    "今天老师讲的这些都是本单元必须掌握的单词，回家要好好复习。\n"
)


def test_batch_preview_renders_rows_and_writes_nothing(client):
    c, db = client
    resp = c.post("/entry/batch/preview", data={"pasted": PASTE})

    assert resp.status_code == 200
    assert 'action="/entry/batch/commit"' in resp.text
    assert 'name="content_0"' in resp.text
    assert 'value="apple"' in resp.text
    assert 'value="苹果"' in resp.text
    assert 'name="lang_0"' in resp.text
    # 预览表里 3 行：编号去掉、批内重复只留一条、长句被丢
    # （粘贴框里仍回显原始文本，所以按预览行的表单名计数，不按原文搜）
    assert resp.text.count('name="content_') == 3
    assert resp.text.count('name="include_') == 3
    # 卡3 起预览表多一列「朗读提示」：英文行的 cue 就是单词本身，
    # 所以 apple 出现 2 次（answer「要写的」+ audio_text），但仍只有 1 行。
    assert resp.text.count('value="apple"') == 2
    assert resp.text.count('name="content_0"') == 1
    assert 'value="今天老师' not in resp.text
    assert _count(db, "question") == 0  # 预览不写库


def test_batch_commit_reports_created_and_skipped(client):
    c, db = client
    c.post("/entry", data=_entry_form())  # 已有 CONTENT=三加五等于多少？

    data = {
        "row_count": "4",
        "include_0": "on",
        "content_0": "apple",
        "answer_0": "苹果",
        "lang_0": "en",
        "include_1": "on",
        "content_1": "banana",
        "answer_1": "香蕉",
        "lang_1": "en",
        "include_2": "on",  # 与库内已有 content 重复 -> 跳过
        "content_2": CONTENT,
        "answer_2": ANSWER,
        "lang_2": "zh",
        # include_3 未勾 -> 该行被删掉
        "content_3": "pear",
        "answer_3": "梨",
        "lang_3": "en",
    }
    resp = c.post("/entry/batch/commit", data=data)

    assert resp.status_code == 200
    assert "收进去 2 条，跳过 1 条（重复）" in resp.text
    assert _count(db, "question") == 3
    assert _count(db, "error_item") == 3

    con = sqlite3.connect(db)
    try:
        rows = con.execute("select content, answer, lang from question order by id").fetchall()
    finally:
        con.close()
    # 单条录入的 CONTENT 是中文题面 -> 现在也落 lang=zh（追加修正 AC11）
    assert rows == [(CONTENT, ANSWER, "zh"), ("apple", "苹果", "en"), ("banana", "香蕉", "en")]


def test_batch_commit_uses_edited_fields(client):
    c, db = client
    data = {
        "row_count": "1",
        "include_0": "on",
        "content_0": "apple",
        "answer_0": "苹果（水果）",
        "lang_0": "zh",
    }
    resp = c.post("/entry/batch/commit", data=data)

    assert resp.status_code == 200
    assert "收进去 1 条，跳过 0 条（重复）" in resp.text
    con = sqlite3.connect(db)
    try:
        row = con.execute("select content, answer, lang, subject from question").fetchone()
    finally:
        con.close()
    assert row == ("apple", "苹果（水果）", "zh", "语文")


# ---- 0.2 卡2 修正 AC8/AC9：学科用中文名，复核队列按学科筛 ----

def test_batch_subject_is_chinese_name(client):
    """AC8：批量入库的 subject 与单条录入一样是中文名，不是 english/chinese。"""
    c, db = client
    data = {
        "row_count": "2",
        "include_0": "on",
        "content_0": "apple",
        "answer_0": "苹果",
        "lang_0": "en",
        "include_1": "on",
        "content_1": "桌子",
        "answer_1": "zhuōzi",
        "lang_1": "zh",
    }
    resp = c.post("/entry/batch/commit", data=data)
    assert "收进去 2 条，跳过 0 条（重复）" in resp.text

    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "select subject, question_type, lang from question order by id"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("英语", "单词", "en"), ("语文", "词语", "zh")]
    assert {"english", "chinese"}.isdisjoint({r[0] for r in rows})


def test_review_queue_shows_needs_variant_blocked(client):
    """0.3 卡2：缺变式卡住的题在 /review-queue 的「缺变式卡住」区可见，不 500。"""
    from datetime import datetime, timedelta, timezone
    from cairn.graduation_service import evaluate_graduation
    from cairn.review_service import grade

    c, db = client
    with _session(db) as session:
        item = create_error_item(
            session,
            _learner_id(db),
            EntryPayload(subject=SUBJECT, question_type=QTYPE, content=CONTENT, answer=ANSWER),
            verify_entry=True,
            entry_verifier=_StubVerifier(),
        )
        item.confidence = 1.0  # 不进低置信度待复核区
        session.commit()
        t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        grade(session, item.id, "good", now=t0)
        grade(session, item.id, "good", now=t0 + timedelta(days=1))
        assert evaluate_graduation(session, item, now=t0 + timedelta(days=4)) is False
        assert item.review_status == "needs_variant"

    page = c.get("/review-queue")
    assert page.status_code == 200
    # G2-4 换 v3 文案：「缺变式卡住」→「还在等的」，解释句讲同类的题
    assert "还在等的" in page.text
    assert CONTENT in page.text
    assert "同类的题" in page.text


def test_review_queue_filters_by_subject(client):
    """AC9：低置信度批量条目能在 /review-queue?subject=英语 里被筛到。"""
    c, db = client
    with _session(db) as session:
        create_error_items(
            session,
            _learner_id(db),
            [
                EntryPayload(
                    subject="英语", question_type="单词", content="apple", answer="苹果", lang="en"
                ),
                EntryPayload(
                    subject="数学", question_type="计算", content="三加五等于多少？", answer="八"
                ),
            ],
            verify_entry=True,
            entry_verifier=_StubVerifier(),  # 0.5 < 0.8 -> 进复核队列
        )

    all_rows = c.get("/review-queue")
    assert "apple" in all_rows.text and "三加五等于多少？" in all_rows.text

    english = c.get("/review-queue", params={"subject": "英语"})
    assert "apple" in english.text
    assert "三加五等于多少？" not in english.text

    math = c.get("/review-queue", params={"subject": "数学"})
    assert "三加五等于多少？" in math.text
    assert "apple" not in math.text


# ---- 追加修正 AC11/AC12：单条录入补 lang（自动判定 + 显式覆盖）----
# 修正（语音 bug）：语种改按 answer（要写出来的词）判，不再按 content（提示）。
# 旧用例用 content=apple/answer=苹果 押 en，属于旧口径；改为新口径后仍验 en。

def _stored_lang(db_path: str) -> str | None:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("select lang from question order by id limit 1").fetchone()[0]
    finally:
        con.close()


def test_single_entry_english_word_gets_en(client):
    """中文意思做提示、answer 是要写的英文单词 → 按 answer 判 en。"""
    c, db = client
    c.post("/entry", data=_entry_form(content="苹果", answer="apple", subject="英语", question_type="单词"))
    assert _stored_lang(db) == "en"


def test_single_entry_pinyin_hint_gets_zh(client):
    """拼音提示 + 汉字答案：按 answer（汉字）判 zh，不被拼音的拉丁字母带成 en。"""
    c, db = client
    c.post("/entry", data=_entry_form(content="chūn tiān", answer="春天", subject="语文", question_type="词语"))
    assert _stored_lang(db) == "zh"


def test_single_entry_undecidable_defaults_to_zh(client):
    """answer 也无中文也无拉丁（纯数字/符号）判断不了，默认 zh。"""
    c, db = client
    c.post("/entry", data=_entry_form(content="3 + 5 = ?", answer="8"))
    assert _stored_lang(db) == "zh"


def test_single_entry_explicit_lang_overrides_auto(client):
    """显式选 en 覆盖自动判定（拼音提示本会判 zh）。"""
    c, db = client
    c.post("/entry", data=_entry_form(content="chūn tiān", answer="春天", lang="en"))
    assert _stored_lang(db) == "en"


def test_single_entry_form_has_lang_select_default_auto(client):
    c, _ = client
    page = c.get("/entry")
    assert 'name="lang"' in page.text
    assert "自动判断" in page.text


# ---- 0.2 卡3 AC2–AC6：听写朗读（只念 cue，不念 answer）----

def _stored_audio(db_path: str) -> str | None:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("select audio_text from question order by id limit 1").fetchone()[0]
    finally:
        con.close()


def _english_form(**overrides) -> dict[str, str]:
    data = _entry_form(subject="英语", question_type="单词", content="apple", answer="苹果")
    data.update(overrides)
    return data


def test_ask_page_renders_play_button_with_cue_and_no_answer(client):
    """AC2/AC3：有 cue 的题出播放键，念的是 cue，答案仍然搜不到。"""
    c, _ = client
    c.post("/entry", data=_english_form())

    page = c.get("/practice")

    assert 'class="play"' in page.text
    assert 'say("apple"' in page.text  # 英文词念单词本身
    assert "苹果" not in page.text  # 答题前不泄露答案
    assert "speechSynthesis" in page.text


def test_ask_page_without_cue_renders_no_play_button(client):
    """AC4：数学题没有 audio_text → 正常显示题目、不出现播放键。"""
    c, _ = client
    c.post("/entry", data=_entry_form())

    page = c.get("/practice")

    assert CONTENT in page.text
    assert 'class="play"' not in page.text
    assert ANSWER not in page.text


def test_reveal_page_can_read_answer_aloud(client):
    """AC5：揭示后可以念答案（已经公开）。"""
    c, _ = client
    c.post("/entry", data=_entry_form())

    revealed = c.post("/practice/reveal", data={"item_id": "1", "learner_answer": "七"})

    assert 'class="play"' in revealed.text
    assert "播放答案" in revealed.text


def test_single_entry_saves_audio_text(client):
    """AC6：单条录入可填朗读提示，落库到 question.audio_text。"""
    c, db = client
    c.post(
        "/entry",
        data=_entry_form(
            subject="语文", question_type="词语", content="桌子", answer="书桌", audio_text="zhuōzi"
        ),
    )
    assert _stored_audio(db) == "zhuōzi"


def test_single_entry_derives_audio_text_when_blank(client):
    """AC6：不填时按同一规则推导（英文词 → 单词本身）。"""
    c, db = client
    c.post("/entry", data=_english_form(audio_text=""))
    assert _stored_audio(db) == "apple"


def test_single_entry_math_gets_no_audio_text(client):
    c, db = client
    c.post("/entry", data=_entry_form())
    assert _stored_audio(db) is None


def test_batch_preview_shows_audio_text_column(client):
    """AC8：预览表能改朗读提示，中文+拼音行的拼音自动填进来。"""
    c, db = client
    resp = c.post("/entry/batch/preview", data={"pasted": "apple 苹果\n苹果 píngguǒ"})

    assert 'name="audio_text_0"' in resp.text
    assert 'name="audio_text_1"' in resp.text
    assert resp.text.count('value="píngguǒ"') == 2  # 提示(content=拼音) + 朗读提示
    assert _count(db, "question") == 0


def test_batch_commit_saves_audio_text(client):
    c, db = client
    data = {
        "row_count": "1",
        "include_0": "on",
        "content_0": "桌子",
        "answer_0": "zhuōzi",
        "lang_0": "zh",
        "audio_text_0": "zhuōzi",
    }
    resp = c.post("/entry/batch/commit", data=data)

    assert "收进去 1 条，跳过 0 条（重复）" in resp.text
    assert _stored_audio(db) == "zhuōzi"


# ---- 0.2 卡3 修正：听写模式「显示提示、念答案、藏答案」----

def _dictation_form(**overrides) -> dict[str, str]:
    data = _entry_form(
        subject="语文",
        question_type="词语",
        content="píngguǒ",  # 提示：拼音（显示）
        answer="苹果",       # 答案：汉字（藏文本，念出来）
        audio_text="píngguǒ",  # 读音覆盖：播放键念它
        lang="zh",
    )
    data.update(overrides)
    return data


def test_dictation_ask_shows_content_hides_answer_text(client):
    """听写：ask 页显示 content（提示），answer 文本搜不到，播放键念 cue。"""
    c, _ = client
    c.post("/entry", data=_dictation_form())

    page = c.get("/practice")

    assert "píngguǒ" in page.text  # content（提示）在
    assert "苹果" not in page.text  # answer（汉字）不作为文本出现
    assert 'class="play"' in page.text
    # tojson 把非 ASCII 转成 \u 转义，按转义后的形态匹配（念的仍是 cue）
    assert f"say({json.dumps('píngguǒ')}" in page.text


def test_dictation_ask_plays_word_for_english(client):
    """英文听写：显示中文意思、播放念英文单词。"""
    c, _ = client
    c.post(
        "/entry",
        data=_dictation_form(
            subject="英语",
            question_type="单词",
            content="苹果",
            answer="apple",
            audio_text="apple",
            lang="en",
        ),
    )

    page = c.get("/practice")

    assert "苹果" in page.text  # 显示中文意思（提示）
    assert 'class="play"' in page.text
    assert 'say("apple"' in page.text  # 播放念英文单词


def test_non_dictation_content_equals_answer_shows_no_play(client):
    """content==answer 的题不进听写模式：照旧显示题目、不渲染播放键。"""
    c, _ = client
    c.post(
        "/entry",
        data=_entry_form(
            subject="语文",
            question_type="词语",
            content="八",
            answer="八",
            audio_text="bā",
            lang="zh",
        ),
    )

    page = c.get("/practice")

    assert "八" in page.text
    assert 'class="play"' not in page.text


def test_reveal_shows_answer_for_dictation(client):
    """听写揭示后 answer 出现（先答后揭示）。"""
    c, _ = client
    c.post("/entry", data=_dictation_form())

    revealed = c.post(
        "/practice/reveal", data={"item_id": "1", "learner_answer": "苹果"}
    )

    assert revealed.status_code == 200
    assert "苹果" in revealed.text


def test_batch_chinese_word_dictation_shows_pinyin_not_hanzi(client):
    """批量中文词（拼音提示 + 汉字）→ 练习页显示拼音、汉字不出现、有播放键。"""
    c, _ = client
    prev = c.post("/entry/batch/preview", data={"pasted": "春天 chūn tiān"})
    # 预览按统一口径：提示=拼音、要写的=汉字
    assert 'name="content_0" value="chūn tiān"' in prev.text
    assert 'name="answer_0" value="春天"' in prev.text

    c.post(
        "/entry/batch/commit",
        data={
            "row_count": "1",
            "include_0": "on",
            "content_0": "chūn tiān",
            "answer_0": "春天",
            "lang_0": "zh",
        },
    )

    page = c.get("/practice")
    assert "chūn tiān" in page.text  # 显示拼音（提示）
    assert "春天" not in page.text  # 汉字（要写的）不作为文本出现
    assert 'class="play"' in page.text  # 有播放键
    assert f"say({json.dumps('chūn tiān')}" in page.text
