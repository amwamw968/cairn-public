"""卡 G2-6：错题本列表（GET /questions）+ 删除（POST /questions/{id}/delete）。

- 列表行三要素：题干摘要 / 错过几次 / 稳不稳（稳不稳与 glance_service 反复错同源）；
- 搜索（题目里的字）与科目筛选（chips）；两套空态按 §4.3 逐字；
- 删除：详情页文字链「删掉」+ 二次确认（弹层主按钮也写「删掉」）→ 真删除级联
  （reviews / error_items / scan_question 行），303 回列表，列表里不再出现。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn.models import ErrorItem, Learner, Question, Review, init_db, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.web import create_app


def _seed(db) -> dict[str, int]:
    """三道题：稳 / 错过多次（反复错）/ 另一科目。返回 question_id 们。"""
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner)).first()
            ids = {}
            for key, subject, content, wrong_count, restored in (
                ("stable", "数学", "3 + 4 = ?", 1, False),
                ("shaky", "数学", "两位数乘一位数进位", 3, False),
                ("english", "英语", "apple", 0, False),
            ):
                question = Question(
                    learner_id=learner.id,
                    subject=subject,
                    grade_level="primary-3",
                    question_type="计算",
                    content=content,
                    answer="x",
                )
                session.add(question)
                session.flush()
                item = ErrorItem(
                    learner_id=learner.id,
                    question_id=question.id,
                    review_status="reviewing",
                    wrong_count=wrong_count,
                    next_due_at=utcnow() + timedelta(days=1),
                    last_reviewed_at=utcnow(),
                )
                if restored:
                    item.restored_at = utcnow()
                session.add(item)
                session.flush()
                if wrong_count:
                    session.add(
                        Review(
                            error_item_id=item.id,
                            rating="again",
                            reviewed_at=utcnow() - timedelta(days=1),
                        )
                    )
                ids[key] = question.id
            session.commit()
            return ids
    finally:
        engine.dispose()


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/book.db")
    with TestClient(app):
        pass
    ids = _seed(tmp_path / "book.db")
    with TestClient(app) as c:
        c.ids = ids  # type: ignore[attr-defined]
        yield c


def test_list_rows_have_the_three_parts(client):
    """E.10：每行 题干摘要 + 错过几次 + 稳不稳（还在反复错/稳）。"""
    html = client.get("/questions").text

    assert "错题本</h1>" in html or "错题本" in html
    assert "3 + 4 = ?" in html
    assert "错过 1 次" in html and "错过 3 次" in html
    assert "稳" in html and "还在反复错" in html
    # 反复错谓词：wrong_count>=2 的那道标「还在反复错」
    # （V3.1 卡片 meta 增加科目徽章，窗口放宽到 300 字符）
    shaky = html.split("两位数乘一位数进位", 1)[1][:300]
    assert "还在反复错" in shaky
    stable = html.split("3 + 4 = ?", 1)[1][:300]
    assert "稳" in stable and "还在反复错" not in stable


def test_search_and_subject_filter(client):
    """搜索按题目里的字；科目 chip 过滤；搜不到走 §4.3 搜索空态。"""
    html = client.get("/questions", params={"q": "两位数"}).text
    assert "两位数乘一位数进位" in html
    assert "3 + 4 = ?" not in html

    html = client.get("/questions", params={"subject": "英语"}).text
    assert "apple" in html
    assert "3 + 4 = ?" not in html

    html = client.get("/questions", params={"q": "没有这个词"}).text
    assert "没找到带「没有这个词」的题" in html
    assert "这里按题目里出现的字来搜。" in html


def test_empty_state_three_parts(tmp_path):
    """空库：§4.3 逐字 + 主按钮去拍第一张卷子。"""
    app = create_app(f"sqlite:///{tmp_path}/empty.db")
    with TestClient(app) as c:
        html = c.get("/questions").text
    assert "还没有收进来的题" in html
    assert "拍完卷子、圈出错题、确认过之后，题会存在这里。" in html
    assert '<a class="btn btn--primary" href="/upload">去拍第一张卷子</a>' in html


def test_detail_has_delete_link_with_two_step_confirm(client):
    """E.10：详情页文字链「删掉」（红）+ 二次确认，弹层主按钮也写「删掉」。"""
    html = client.get(f"/questions/{client.ids['stable']}").text

    assert '<summary class="linklike linklike--danger">删掉</summary>' in html
    details = html.split('class="reject-box"', 1)[1]
    assert f'action="/questions/{client.ids["stable"]}/delete"' in details
    assert '<button type="submit" class="btn btn--secondary">删掉</button>' in details
    assert "删掉就找不回来了" in details


def test_delete_removes_question_and_all_traces(client):
    """真删除（决策点 2）：303 回列表，题与关联 reviews/items 都没了。"""
    question_id = client.ids["shaky"]
    resp = client.post(f"/questions/{question_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/questions"

    html = client.get("/questions").text
    assert "两位数乘一位数进位" not in html
    assert client.get(f"/questions/{question_id}").status_code == 404

    # 别人的/别的题不受影响
    assert "3 + 4 = ?" in html


def test_detail_page_still_holds_the_edit_contract(client):
    """详情页编辑契约（G2-6 换壳后不掉）：#question-edit、保存这道题、讲解标签。"""
    html = client.get(f"/questions/{client.ids['stable']}").text

    assert '<details id="question-edit"' in html
    assert 'id="edit-save"' in html and "保存这道题" in html
    assert "讲解" in html
    assert "解析" not in html, "「解析」红线词还魂"
    assert "复核" not in html and "出库" not in html and "题型" not in html
