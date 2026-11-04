"""卡 T3-a：题干 / 答案 / 解析可以人工改，改过要留痕。

这一页只认三件事：

- **AC3 改得动**：`POST /questions/{id}/edit` → 303 → 详情页是新的题干；
  库里 `content` 是新值、`edited_at` 非空、`edited_by='human'`。
- **AC4 改不坏**：题干去空白后为空 → 400，库里的值一个字段都不许变，
  页面回填的还是**原值**（不是家长刚提交的那串空白）。
- **AC5 看得见**：页面有 `<details>`，三个字段预填当前值，保存键是印刷表单的
  整行主按钮；`edited_at` 有值时 `.facts` 第三格写「2026-11-02 04:12」（P4-a 起）。

迁移 v21 的幂等与「存量行一字未动」在 tests/test_migrations.py（AC2），
390 宽无横向溢出在文件末尾的真浏览器用例（AC7）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn.models import Learner, Question, init_db, make_engine, make_session_factory
from cairn.web import create_app

ORIGINAL_CONTENT = "12 + 15 = ?"
ORIGINAL_ANSWER = "27"
ORIGINAL_ANALYSIS = "先加后减：12 + 15 = 27"
# UTC+8 的本地时间戳：秒级、带偏移。写进库里的就是这一串，页面只截到分钟。
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00")
_TZ8 = timezone(timedelta(hours=8))


@pytest.fixture()
def web(tmp_path):
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as client:
        yield client, db


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
    content: str = ORIGINAL_CONTENT,
    answer: str | None = ORIGINAL_ANSWER,
    analysis: str | None = ORIGINAL_ANALYSIS,
    edited_at: str | None = None,
    learner_id: int | None = None,
) -> int:
    with _session(db) as session:
        if learner_id is None:
            learner = session.scalars(
                select(Learner).order_by(Learner.id.asc()).limit(1)
            ).first()
            if learner is None:  # 浏览器用例不经过 lifespan，默认孩子自己建
                learner = Learner(name="test-child", grade_level="primary-3")
                session.add(learner)
                session.commit()
            learner_id = learner.id
        question = Question(
            learner_id=learner_id,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content=content,
            answer=answer,
            analysis=analysis,
            edited_at=edited_at,
            edited_by="human" if edited_at else None,
        )
        session.add(question)
        session.commit()
        return question.id


def _row(db: Path, question_id: int) -> dict:
    with _session(db) as session:
        question = session.get(Question, question_id)
        return {
            "content": question.content,
            "answer": question.answer,
            "analysis": question.analysis,
            "edited_at": question.edited_at,
            "edited_by": question.edited_by,
        }


def _fingerprint(row: dict) -> str:
    """三个正文字段的 sha256：AC4 用它一眼看出「一个字都没动」。"""
    joined = "\x1f".join(str(row[key]) for key in ("content", "answer", "analysis"))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _set_stamp(db: Path, question_id: int, stamp: str) -> None:
    """直接往库里写一个固定时间戳：页面那行小字按它显示，不依赖跑测试的此刻。"""
    with _session(db) as session:
        question = session.get(Question, question_id)
        question.edited_at = stamp
        question.edited_by = "human"
        session.commit()


# --- AC3：改得动、留得下痕 --------------------------------------------------


def test_edit_content_lands_and_stamps_human(web):
    """AC3：改题干 → 303 回详情页 → 新题干在页面上；库里三样都对。"""
    client, db = web
    question_id = _add_question(db)

    started = datetime.now(_TZ8)
    saved = client.post(
        f"/questions/{question_id}/edit",
        data={
            "content": "  13 + 15 = ?  ",
            "answer": "28",
            "analysis": "先算 13 + 15，再把结果写下来",
        },
        follow_redirects=False,
    )

    assert saved.status_code == 303
    assert saved.headers["location"] == f"/questions/{question_id}"

    page = client.get(f"/questions/{question_id}")
    assert page.status_code == 200
    assert "13 + 15 = ?" in page.text          # 首尾空白已去掉
    assert "12 + 15 = ?" not in page.text       # 旧题干不许还在
    assert "28" in page.text

    row = _row(db, question_id)
    assert row["content"] == "13 + 15 = ?"
    assert row["answer"] == "28"
    assert row["analysis"] == "先算 13 + 15，再把结果写下来"
    assert row["edited_by"] == "human"
    assert _STAMP.fullmatch(row["edited_at"] or ""), row["edited_at"]
    stamped = datetime.fromisoformat(row["edited_at"])
    assert stamped.utcoffset() == timedelta(hours=8)
    assert abs((stamped - started).total_seconds()) < 120  # 就是刚才那一刻
    print(f"editing: content={row['content']!r} edited_by={row['edited_by']!r} edited_at={row['edited_at']}")


def test_edit_writes_stamp_even_when_nothing_changed(web):
    """三个字段都没变也一样留痕：人确实点过保存（卡上明说的一条）。"""
    client, db = web
    question_id = _add_question(db)
    assert _row(db, question_id)["edited_at"] is None

    saved = client.post(
        f"/questions/{question_id}/edit",
        data={
            "content": ORIGINAL_CONTENT,
            "answer": ORIGINAL_ANSWER,
            "analysis": ORIGINAL_ANALYSIS,
        },
        follow_redirects=False,
    )

    assert saved.status_code == 303
    row = _row(db, question_id)
    assert row["content"] == ORIGINAL_CONTENT
    assert row["edited_at"] is not None and row["edited_by"] == "human"


def test_edit_blank_answer_and_analysis_become_null(web):
    """答案 / 解析可空：提交空白 = NULL（与「补答案」那条路同一口径）。"""
    client, db = web
    question_id = _add_question(db)

    saved = client.post(
        f"/questions/{question_id}/edit",
        data={"content": ORIGINAL_CONTENT, "answer": "   ", "analysis": ""},
        follow_redirects=False,
    )

    assert saved.status_code == 303
    row = _row(db, question_id)
    assert row["answer"] is None and row["analysis"] is None
    assert row["edited_at"] is not None


# --- AC4：空题干 400，库里一个字段都不动 ------------------------------------


def test_blank_content_is_400_and_keeps_the_original_row(web):
    """AC4：`content="   "` → 400；库里三字段 + 留痕一个都没变，页面回填原值。"""
    client, db = web
    question_id = _add_question(db, edited_at="2026-11-01T09:30:00+08:00")
    before = _row(db, question_id)
    digest = _fingerprint(before)

    resp = client.post(
        f"/questions/{question_id}/edit",
        data={
            "content": "   ",
            "answer": "改坏了",
            "analysis": "改坏了",
        },
    )

    assert resp.status_code == 400
    assert "题干不能为空" in resp.text
    # 回填的是库里的原值：被拒的那次提交（空题干 + 改坏的答案）一个字都不许上页面
    assert f">{ORIGINAL_CONTENT}</textarea>" in resp.text
    assert f'value="{ORIGINAL_ANSWER}"' in resp.text
    assert "改坏了" not in resp.text
    assert 'open' in resp.text.split('id="question-edit"', 1)[1].split(">", 1)[0]  # 展开着给人改

    after = _row(db, question_id)
    assert after == before, after
    assert _fingerprint(after) == digest
    print(f"400 后库指纹不变：{digest[:16]}… content={after['content']!r} edited_at={after['edited_at']!r}")


def test_edit_someone_elses_question_is_404(web):
    """别人的题当不存在：404，且那道题一个字都没动。"""
    client, db = web
    with _session(db) as session:
        other = Learner(name="另一个孩子", grade_level="primary-3")
        session.add(other)
        session.commit()
        other_id = other.id
    question_id = _add_question(db, learner_id=other_id)
    before = _row(db, question_id)

    resp = client.post(
        f"/questions/{question_id}/edit",
        data={"content": "偷看别人的题", "answer": "1", "analysis": "1"},
    )

    assert resp.status_code == 404
    assert _row(db, question_id) == before


# --- AC5：页面形状 ----------------------------------------------------------


def test_page_has_details_with_prefilled_fields(web):
    """AC5：`<details>` + 三字段预填 + 印刷表单的整行主按钮；没改过就没有那行小字。"""
    client, db = web
    question_id = _add_question(db)

    page = client.get(f"/questions/{question_id}")
    html = page.text

    assert page.status_code == 200
    assert '<details id="question-edit"' in html
    assert 'action="/questions/' + str(question_id) + '/edit"' in html
    assert f">{ORIGINAL_CONTENT}</textarea>" in html          # 题干预填
    assert f'value="{ORIGINAL_ANSWER}"' in html               # 答案预填
    assert f">{ORIGINAL_ANALYSIS}</textarea>" in html         # 解析预填
    # 保存键：印刷表单那一套（动作区当表格行，主按钮整行）
    assert '<div class="select-actions">' in html
    assert 'class="btn btn--primary">保存这道题</button>' in html
    assert "人工改过" not in html                              # 没改过就不显示

    # 模板自身：没有内联样式、没有脚本（硬约束）
    template = Path("src/cairn/templates/question_detail.html").read_text(encoding="utf-8")
    assert "<style" not in template and "<script" not in template
    assert "style=" not in template


def test_page_shows_the_hand_edit_stamp(web):
    """AC5：`edited_at` 有值时 `.facts` 第三格出时间，分钟级、不带秒。"""
    client, db = web
    question_id = _add_question(db, edited_at="2026-11-02T04:12:33+08:00")

    html = client.get(f"/questions/{question_id}").text

    assert "2026-11-02 04:12" in html
    assert "2026-11-02 04:12:33" not in html    # 秒不显示
    line = re.search(r'<p class="facts__meta" id="question-edited">([^<]+)</p>', html)
    assert line is not None, html[:400]
    assert line.group(1).strip() == "2026-11-02 04:12"


def test_page_without_stamp_has_no_line(web):
    """对照组：没人改过的题，第三格写「未改过」——这句话不能是编出来的时间。"""
    client, db = web
    question_id = _add_question(db)

    html = client.get(f"/questions/{question_id}").text

    assert "未改过" in html
    assert "人工改过" not in html
    assert 'id="question-edited"' not in html


# --- AC7：390 宽无横向溢出（真浏览器） --------------------------------------


def _browser_python() -> str | None:
    import os

    candidates = [os.environ.get("CAIRN_BROWSER_PYTHON"), "/usr/bin/python3", shutil.which("python3")]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            done = subprocess.run(
                [candidate, "-c", "import playwright"], capture_output=True, timeout=60
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return candidate
    return None


BROWSER_PY = _browser_python()
needs_browser = pytest.mark.skipif(
    BROWSER_PY is None, reason="本机没有带 playwright 的 python3，浏览器用例跳过"
)

_BROWSER = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, qid = sys.argv[1], sys.argv[2]
out = {}
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(base + "/questions/" + qid, wait_until="load")
    page.click("#question-edit > summary")
    out["scroll"] = page.evaluate(
        "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
    )
    out["open"] = page.evaluate("() => document.getElementById('question-edit').open")
    out["edited_line"] = page.inner_text("#question-edited")
    out["content"] = page.input_value("#edit-content")
    out["save"] = page.evaluate(
        "() => {"
        " var el = document.getElementById('edit-save');"
        " var s = getComputedStyle(el);"
        " var after = getComputedStyle(el, '::after');"
        " return {"
        "  w: Math.round(el.getBoundingClientRect().width),"
        "  form: Math.round(el.closest('form').getBoundingClientRect().width),"
        "  border: s.borderBottomWidth,"
        "  arrow: after.content"
        " };"
        "}"
    )
    browser.close()

print(json.dumps(out, ensure_ascii=False))
'''


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def live_server(app):
    """把 app 挂到真地址上（浏览器只能走 HTTP，不能走 TestClient）。"""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("本地服务没起来（uvicorn 线程超时）")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@needs_browser
def test_browser_390_no_horizontal_overflow_and_whole_row_save(tmp_path):
    """AC7 + AC5：390 宽 scrollWidth == 390；展开后保存键是整行、2px 墨线、带箭头。"""
    db = tmp_path / "web.db"
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)  # TestClient 会跑 lifespan；浏览器用例不经过它，这里自己建表
    engine.dispose()
    app = create_app(f"sqlite:///{db}")
    question_id = _add_question(db, edited_at="2026-11-02T04:12:33+08:00")

    with live_server(app) as base:
        script = tmp_path / "browse.py"
        script.write_text(_BROWSER, encoding="utf-8")
        done = subprocess.run(
            [BROWSER_PY, str(script), base, str(question_id)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    assert done.returncode == 0, f"浏览器脚本失败：\n{done.stdout}\n{done.stderr}"
    seen = json.loads(done.stdout.strip().splitlines()[-1])

    assert seen["scroll"] == [390, 390], f"390 宽下有横向溢出：{seen['scroll']}"
    assert seen["open"] is True
    assert seen["content"] == ORIGINAL_CONTENT
    assert seen["edited_line"].strip() == "2026-11-02 04:12"
    # 「整行」= 按钮宽度铺满表单；「2px 墨线」+「箭头」= 印刷表单的主按钮形态
    assert seen["save"]["w"] == seen["save"]["form"], seen["save"]
    # G2-6：保存键随 v3 换成实心主按钮（无底线描边、无箭头）；整行宽这条不掉
    assert seen["save"]["border"] == "0px", seen["save"]
    assert seen["save"]["arrow"] == "none", seen["save"]
    print(
        f"390×844：scrollWidth {seen['scroll'][0]} | 保存键 {seen['save']['w']}px"
        f"（表单 {seen['save']['form']}px）| 墨线 {seen['save']['border']}"
        f" | 箭头 {seen['save']['arrow']}"
    )
