"""练习页（卡 V3a-fix）：四态页面的第一刀，把 V3a 改造的验收补成测试。

只锁 V3a 卡声明的六条：

1. ask 态页头：`<h1>今天这一题</h1>`、`.page-head`、kicker `复习 · 练一题`；
2. ask 态不泄漏答案：种一条已知 answer 的题，答案串不得出现在响应里；
3. `.play` 按钮还在，`onclick` 里仍是 `say(`（听写题的朗读入口不许换形式）；
4. 四个表单 action 都在（分散在四个态）：`/practice/reveal`、`/practice/grade`、
   `/practice/similar/tag`、`/strategies/select`；
5. 页面不含 `<table>`，`.btn--primary` 恰好 1 个（ask 态只有「看答案」）;
6. 有考试数据时 `class="exam-bar"` 在，且 `components.css` 有 `.page .exam-bar` 规则。

另锁两条 V3a 卡的事实（发现即红）：

- 没有考试数据时倒计时条整块不渲染；
- 四态 h1 与规范逐字一致（ask/reveal/similar/graded）。
`reveal` / `similar` / `graded` 三分支的 git diff 零变化是仓库层检查，
进 `docs/EVIDENCE.md`，不在测试里。

V3b-1 补 reveal 支的版式契约（四区块改造后仍然守住的底线）：
h1 逐字、判分表单 `action` / `.ratings` / 四个 `name="rating"` 逐字不变、
整页无 `<table>`、`.btn--primary` 不超过 1 个、`class="play"` 与 `say(` 还在。

V3b-2 补 similar 支的行式列表：h1 与 lede 逐字、底部说明逐字、
列表里至少一个带 href 的 `.list-row`（整行可点，行内不许有按钮）、整页无 `<table>`。

V3b-3 补 graded 支：h1 与 lede 逐字、`.facts` 里 `.facts__value` 恰好 3 格、
动作区两键的 href（`/practice` 与 `/scans`，主按钮恰好 1 个）、整页无 `<table>`。

数据照 `tests/test_practice_image.py` 的方式直接落库（手录题：无 scan_question 行）。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn import web as web_module
from cairn.concept_service import tag_question
from cairn.models import Learner, Question, make_engine, make_session_factory
from cairn.models.error_item import REVIEW_STATUS_NEW, ErrorItem
from cairn.web import create_app

_SRC = Path(web_module.__file__).parent
COMPONENTS_CSS = (_SRC / "static" / "components.css").read_text(encoding="utf-8")

# 唯一性哨兵：全库只有这条 answer 带这个串，ask 态出现即泄漏
SECRET_ANSWER = "XZ-981揭示后才可见"


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "practice.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as c:
        yield c, db


def _session(db: Path):
    engine = make_engine(f"sqlite:///{db}")
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


def _add_question(
    db: Path,
    *,
    content: str = "12 + 15 = ?",
    answer: str | None = SECRET_ANSWER,
    audio_text: str | None = None,
) -> int:
    """一道手录题 + 一条 new 错题；返回 error_item_id（练习页的 item_id）。"""
    with _session(db) as session:
        learner_id = session.scalars(select(Learner).limit(1)).first().id
        question = Question(
            learner_id=learner_id,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content=content,
            answer=answer,
            audio_text=audio_text,
        )
        session.add(question)
        session.flush()
        item = ErrorItem(
            learner_id=learner_id,
            question_id=question.id,
            review_status=REVIEW_STATUS_NEW,
            confidence=1.0,
        )
        session.add(item)
        session.commit()
        return item.id


def _add_exam(c: TestClient, *, days: int) -> None:
    """建一场考试（走 /exam 表单，与真实入口同一条件）。"""
    resp = c.post(
        "/exam",
        data={
            "name": "单元考",
            "exam_date": (date.today() + timedelta(days=days)).isoformat(),
            "subject": "数学",
            "note": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


# ------------------------------------------------------------------ ① 页头


def test_ask_page_has_the_new_head(client):
    """G2-5 升级：ask 态 200，v3 页头 + 蓝图 E.8 文案（练一题 / 答完了 / 提示句）。"""
    c, db = client
    item_id = _add_question(db)

    page = c.get("/practice")
    html = page.text

    assert page.status_code == 200
    assert re.search(r'<h1 class="page-title">\s*练一题', html)
    assert 'class="page-head"' in html
    # 确认这就是带题的 ask 面（不是空态）
    assert "12 + 15 = ?" in html
    assert str(item_id) in html
    assert "想不出来也可以直接点答完了" in html
    assert "答完了" in html


# ------------------------------------------------------------------ ② 不泄漏答案


def test_ask_page_never_leaks_the_answer(client):
    """② 种一条已知 answer，ask 态响应里这个串（以及"标准答案"字样）不许出现。"""
    c, db = client
    _add_question(db)

    html = c.get("/practice").text

    assert SECRET_ANSWER not in html, "ask 态把答案渲染出去了"
    assert "标准答案" not in html, "ask 态不该出现揭示区的字样"


# ------------------------------------------------------------------ ③ 播放按钮


def test_play_button_still_speaks(client):
    """③ 听写题的 `.play` 与 `onclick='say('` 原样在（朗读键唯一入口不许换形式）。"""
    c, db = client
    _add_question(db, content="听写：apple", answer="苹果", audio_text="apple")

    html = c.get("/practice").text

    assert 'class="play"' in html, ".play 按钮不见了"
    assert "onclick='say(" in html, "onclick 里的 say( 调用形式变了"


# ------------------------------------------------------------------ ④ 四个表单 action


def test_all_four_form_actions_are_intact(client):
    """④ 四个 action 分散在四个态，逐个态确认：少一个就红。"""
    c, db = client
    item_id = _add_question(db)

    ask = c.get("/practice").text
    assert 'action="/practice/reveal"' in ask, "ask 态丢了 reveal 表单"
    assert 'action="/strategies/select"' in ask, "ask 态丢了策略表单"

    reveal = c.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "27"}
    ).text
    assert 'action="/practice/grade"' in reveal, "reveal 态丢了判分表单"

    similar = c.get("/practice/similar", params={"item_id": item_id}).text
    assert 'action="/practice/similar/tag"' in similar, "similar 态丢了补标表单"


# ------------------------------------------------------------------ ⑤ 无 table / 唯一主按钮


def test_ask_page_has_no_table_and_one_primary_button(client):
    """⑤ ask 态整页无 `<table>`；`.btn--primary` 恰好 1 个（「看答案」）。"""
    c, db = client
    _add_question(db)

    html = c.get("/practice").text

    assert "<table" not in html.lower(), "ask 态又出现表格了"
    assert html.count('class="btn btn--primary"') == 1, (
        f"主按钮应当恰好 1 个，实际 {html.count('btn--primary')}"
    )


# ------------------------------------------------------------------ ⑥ 倒计时条 + CSS 规则


def test_exam_bar_renders_with_its_css_rule(client):
    """⑥ 有考试时 `class="exam-bar"` 在（离考远：不带 sprint），CSS 规则也在。"""
    c, db = client
    item_id = _add_question(db)
    _add_exam(c, days=20)

    html = c.get("/practice").text

    assert 'class="exam-bar"' in html, "倒计时条没了"
    assert 'class="exam-bar sprint"' not in html, "离考 20 天不该进冲刺态"
    assert "距离单元考还有 20 天" in html
    # G2-5：练习页挂 v3 壳，规则锁到活的 v3.css（旧 components.css 的规则已不是它的家）
    v3_css = (Path(web_module.__file__).parent / "static" / "v3.css").read_text(
        encoding="utf-8"
    )
    assert ".exam-bar {" in v3_css, "v3.css 丢了 .exam-bar 规则"


def test_exam_bar_absent_without_exam(client):
    """V3a 只读检查落成测试：`exam_bar` 为空时整块（含 id）不渲染。"""
    c, db = client
    _add_question(db)

    html = c.get("/practice").text

    assert "exam-bar" not in html, "没考试时倒计时条还在渲染"


# ------------------------------------------------------------------ 四态 h1


def test_four_modes_keep_the_spec_headlines(client):
    """G2-5 升级：四态 h1 逐字（蓝图 E.8/E.9）：ask=练一题（可带第 x/N 题）/
    reveal=这题怎么样 / similar=同类的题 / graded=这题记下了。"""
    c, db = client
    item_id = _add_question(db)

    assert re.search(r'<h1 class="page-title">\s*练一题', c.get("/practice").text)
    assert re.search(
        r'<h1 class="page-title">\s*这题怎么样',
        c.post("/practice/reveal", data={"item_id": str(item_id), "learner_answer": "27"}).text,
    )
    assert re.search(
        r'<h1 class="page-title">\s*同类的题',
        c.get("/practice/similar", params={"item_id": item_id}).text,
    )
    assert re.search(
        r'<h1 class="page-title">\s*这题记下了',
        c.post("/practice/grade", data={"item_id": str(item_id), "rating": "good"}).text,
    )


# ------------------------------------------------------------------ ⑦ reveal 四区块


def test_reveal_page_keeps_its_contract(client):
    """G2-5 升级：reveal 三条底线——h1「这题怎么样」、判分 action 在、`.ratings` 在。"""
    c, db = client
    item_id = _add_question(db)

    html = c.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "27"}
    ).text

    assert re.search(r'<h1 class="page-title">\s*这题怎么样', html), "reveal 态 h1 不是规范文案"
    assert 'action="/practice/grade"' in html, "判分表单的 action 变了"
    assert 'class="ratings"' in html, "判分表单丢了 .ratings"


def test_reveal_page_keeps_play_and_rating_buttons(client):
    """G2-5 升级：无 `<table>`、`btn--primary` ≤ 1、播放键逐字、四个 rating 的
    value 逐字——文案换 v3 双主按钮（我懂了/还迷糊 = 全站唯一双按钮例外，
    勉强会/早就会降为文字链），FSRS 四档一个不删。"""
    c, db = client
    item_id = _add_question(db)

    html = c.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "27"}
    ).text

    assert "<table" not in html.lower(), "reveal 态出现表格了"
    assert html.count("btn--primary") <= 1, (
        f"主按钮应当最多 1 个，实际 {html.count('btn--primary')}"
    )
    assert 'class="play"' in html, "播放答案键不见了"
    assert "onclick='say(" in html, "onclick 里的 say( 调用形式变了"
    # 全站唯一双主按钮例外：恰好两枚 .btn--dual，且同级
    assert html.count('class="btn btn--dual"') == 2, "双主按钮例外不是恰好两枚"
    for label, value in (
        ("我懂了", "good"),
        ("还迷糊", "again"),
        ("勉强会", "hard"),
        ("早就会了", "easy"),
    ):
        assert f'name="rating" value="{value}">{label}</button>' in html, (
            f"四档按钮的文案或 value 变了：{label}"
        )


# ------------------------------------------------------------------ ⑧ similar 行式列表


def _add_similar_pair(db: Path) -> tuple[int, int]:
    """两道题挂同一个知识点（「进位」）；返回 (原题 item_id, 同类题 item_id)。"""
    with _session(db) as session:
        learner_id = session.scalars(select(Learner).limit(1)).first().id
        source = Question(
            learner_id=learner_id,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content="23 × 4 = ?",
            answer="92",
        )
        other = Question(
            learner_id=learner_id,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content="18 × 5 = ?",
            answer="90",
        )
        session.add_all([source, other])
        session.flush()
        tag_question(session, source, ["进位"])
        tag_question(session, other, ["进位"])
        source_item = ErrorItem(
            learner_id=learner_id,
            question_id=source.id,
            review_status=REVIEW_STATUS_NEW,
            confidence=1.0,
        )
        other_item = ErrorItem(
            learner_id=learner_id,
            question_id=other.id,
            review_status=REVIEW_STATUS_NEW,
            confidence=1.0,
        )
        session.add_all([source_item, other_item])
        session.commit()
        return source_item.id, other_item.id


def test_similar_page_head_is_verbatim(client):
    """⑧ similar 态三处逐字：h1 `同类的题`、lede 那句、底部「原题已排除」的说明。"""
    c, db = client
    item_id, _other_item = _add_similar_pair(db)

    page = c.get("/practice/similar", params={"item_id": item_id})
    html = page.text

    assert page.status_code == 200
    assert re.search(r'<h1 class="page-title">\s*同类的题', html), "similar 态 h1 不是规范文案"
    assert "同知识点、已排除原题；点一行就换到那道题上练。" in html, "lede 不是规范文案"
    assert "这些题和刚才那道同一个知识点，原题已排除。" in html, "底部少了排除原题的说明"


def test_similar_list_is_rows_that_link_to_the_question(client):
    """⑧ 列表是一行一个 `.list-row`：至少一行带 href 链到那道题，行内不放按钮。"""
    c, db = client
    item_id, other_item = _add_similar_pair(db)

    html = c.get("/practice/similar", params={"item_id": item_id}).text

    rows = re.findall(r'<a class="list-row" href="([^"]+)"', html)
    assert rows, "同类题列表里没有一个带 href 的 .list-row（整行点不进去）"
    assert f"/practice?item_id={other_item}" in rows, f"整行链到了别处：{rows}"
    for row in re.findall(r'<a class="list-row".*?</a>', html, re.S):
        assert "<button" not in row, "行内放了按钮：整行可点就够了"


def test_similar_page_has_no_table(client):
    """⑧ similar 态整页无 `<table>`（列表是行，不是表格）。"""
    c, db = client
    item_id, _other_item = _add_similar_pair(db)

    html = c.get("/practice/similar", params={"item_id": item_id}).text

    assert "<table" not in html.lower(), "similar 态出现表格了"


# ------------------------------------------------------------------ ⑨ graded 三格 + 两键


def _grade(c: TestClient, item_id: int, rating: str = "good") -> str:
    """走一次判分表单，返回 graded 态页面。"""
    resp = c.post("/practice/grade", data={"item_id": str(item_id), "rating": rating})
    assert resp.status_code == 200, resp.text
    return resp.text


def test_graded_page_head_is_verbatim(client):
    """⑨ graded 态 h1 与 lede 逐字（这一页只回答「下次什么时候再练」）。"""
    c, db = client
    item_id = _add_question(db)

    html = _grade(c, item_id)

    assert re.search(r'<h1 class="page-title">\s*这题记下了', html), "graded 态 h1 不是规范文案"
    assert "这一次已经记下，下次到期的日子在下面。" in html, "lede 不是规范文案"


def test_graded_page_has_three_facts_values(client):
    """⑨ 主体一个 `.facts`，`.facts__value` 恰好 3 格（日期 / 天数 / 自评）。"""
    c, db = client
    item_id = _add_question(db)

    html = _grade(c, item_id)

    assert html.count('class="facts"') == 1, "主体应当只有一个 .facts"
    values = html.count('class="facts__value"')
    assert values == 3, f".facts__value 应当恰好 3 格，实际 {values}"


def test_graded_page_actions_link_to_practice_and_scans(client):
    """⑨ 动作区两键：主按钮「再练一题」→ /practice，次按钮「回卷子」→ /scans。"""
    c, db = client
    item_id = _add_question(db)

    html = _grade(c, item_id)

    assert '<a class="btn btn--primary" href="/practice">再练一题</a>' in html, (
        "主按钮的文案或 href 变了"
    )
    assert '<a class="btn btn--secondary" href="/scans">回卷子</a>' in html, "次按钮的文案或 href 变了"
    assert html.count("btn--primary") == 1, (
        f"主按钮应当恰好 1 个，实际 {html.count('btn--primary')}"
    )


def test_graded_page_has_no_table(client):
    """⑨ graded 态整页无 `<table>`（三格是数据条，不是表格）。"""
    c, db = client
    item_id = _add_question(db)

    html = _grade(c, item_id)

    assert "<table" not in html.lower(), "graded 态出现表格了"
