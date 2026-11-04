"""V5 · 「复核队列」页面级改造（主流程第四页）。

这一页只回答一件事：哪几道题机器判不准、等我点一下；哪几道卡住不走、为什么。
断言分三层（与 test_scans_page 同一套口径）：

- **结构层**（真 HTTP 响应）：extends `_base.html`、没有 `<table>`、页头三件与唯一主按钮、
  计数条两个数、每一块待判题的三个动作（看原图链接 + 两个 POST 表单，action / 字段名逐字）、
  缺变式用 `.verdict--warn`。
- **视觉层**（真浏览器，390 视口、`is_mobile=False`）：`scrollWidth == 390`，
  行内三个控件都在 `.row-actions` 里、都放得下（右边界不出 390），并留一张截图。
- **文案层**：页头 kicker / h1 / lede、机器值行、原因、脚注逐字。

数据一律现造：3 条低置信度（进待复核）+ 1 条 needs_variant（缺变式卡住）。
两个 POST 表单的 action 与字段名由本文件逐字钉住——页面换皮不许改提交语义。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn import web as web_module
from cairn.entry_service import EntryPayload, create_error_item
from cairn.models import (
    REVIEW_STATUS_NEEDS_VARIANT,
    ErrorItem,
    Learner,
    make_engine,
    make_session_factory,
)
from cairn.models.learner import utcnow
from cairn.web import create_app

_SRC = Path(web_module.__file__).parent
TEMPLATE = _SRC / "templates" / "confirm.html"
V3_CSS = _SRC / "static" / "v3.css"
SHOT_390 = Path("/tmp/v5_review_queue_390.png")

PENDING_COUNT = 3
BLOCKED_COUNT = 1

# 长的题干用来量「390 下这一行会不会被题干撑宽」——短题撑不出问题
LONG_STEM = "把下面的分数化成小数再比较大小，写出每一步的算法和理由：3/8 与 0.4 哪个大？"


class _StubVerifier:
    """固定置信度 0.5 → 进待复核队列（< 0.8 且没复核过）。"""

    def verify(self, payload: EntryPayload) -> float:
        return 0.5


# ------------------------------------------------------------------ 造数据


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


def _payload(content: str) -> EntryPayload:
    return EntryPayload(subject="数学", question_type="计算", content=content, answer="27")


def _add_doubtful(db: Path, content: str) -> tuple[int, int]:
    """一条低置信度错题（进待复核队列）；返回 (error_item_id, question_id)。"""
    with _session(db) as session:
        learner_id = session.scalars(select(Learner).limit(1)).first().id
        item = create_error_item(
            session,
            learner_id,
            _payload(content),
            verify_entry=True,
            entry_verifier=_StubVerifier(),
        )
        return item.id, item.question_id


def _park_needs_variant(db: Path, content: str) -> tuple[int, int]:
    """一条 needs_variant（缺变式卡住那一段）。

    顺手写 `last_reviewed_at`：needs_variant 的本义是「已经复习过几轮」，
    待复核队列只收没复习过的；不写这一笔，同一道题会在两段里各出现一次。
    """
    item_id, question_id = _add_doubtful(db, content)
    with _session(db) as session:
        item = session.get(ErrorItem, item_id)
        item.review_status = REVIEW_STATUS_NEEDS_VARIANT
        item.last_reviewed_at = utcnow()
        session.commit()
    return item_id, question_id


def _seed(db: Path) -> dict[str, list[int]]:
    pending = [_add_doubtful(db, LONG_STEM if index == 0 else f"待判题-{index}") for index in range(PENDING_COUNT)]
    blocked = [_park_needs_variant(db, "缺变式的题") for _ in range(BLOCKED_COUNT)]
    return {
        "pending": [item_id for item_id, _ in pending],
        "blocked": [item_id for item_id, _ in blocked],
    }


@pytest.fixture()
def app_for_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    db = tmp_path / "queue.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app):
        pass  # 先让库把表建出来
    _seed(db)
    return app


@pytest.fixture()
def client(app_for_queue):
    with TestClient(app_for_queue) as c:
        yield c


@pytest.fixture()
def empty_client(tmp_path, monkeypatch):
    """一条都没有的库：空态那一屏。"""
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    db = tmp_path / "empty.db"
    with TestClient(create_app(f"sqlite:///{db}")) as c:
        yield c


# ------------------------------------------------------------------ 解析小件

FORM = re.compile(
    r'<form method="post" action="([^"]+)">\s*'
    r'<input type="hidden" name="item_id" value="(\d+)">\s*'
    r'<button type="submit" class="btn ([^"]+)">(.*?)</button>',
    re.S,
)
# 焦点区唯一允许的两个链接：改一下（编辑页）/ 看原图（详情页）
EDIT_LINK = re.compile(r'<a class="btn--ghost" href="/questions/(\d+)/edit\?back=queue">读得不对，改一下</a>')
VIEW_LINK = re.compile(r'<a class="btn--ghost" href="/questions/(\d+)">看原图</a>')


# ------------------------------------------------------------------ 结构层


def test_template_extends_the_v3_shell():
    """G2-4 升级：模板挂 v3 壳，不带自己的骨架、没有表格、零内联样式。"""
    source = TEMPLATE.read_text(encoding="utf-8")

    assert source.startswith('{% extends "_v3.html" %}')
    for tag in ("<!DOCTYPE", "<head", "<nav", "<style", "<table", "<tr", "<td", "style="):
        assert tag not in source, f"confirm.html 还有：{tag}"
    assert '{% include "_nav.html" %}' not in source
    assert source.count("{% block content %}") == 1


def test_page_html_has_no_table(client):
    """整页 HTML 不许出现 `<table` / `<tr` / `<td`。"""
    html = client.get("/review-queue").text

    for tag in ("<table", "<tr", "<td"):
        assert tag not in html, f"待确认屏还有 {tag}"


def test_entry_forms_keep_their_actions_and_field_names(client):
    """G2-4 升级：一屏一题，两个表单（确认/不收），action 与字段名逐字不变。

    这一页唯一的写操作：提交地址与字段名一个字都不许动；两个表单打同一条题。
    """
    html = client.get("/review-queue").text

    assert html.count('action="/review-queue/confirm"') == 1
    assert html.count('action="/review-queue/reject"') == 1
    assert html.count('<input type="hidden" name="item_id"') == 2

    forms = FORM.findall(html)
    assert [(action, cls, label) for action, _id, cls, label in forms] == [
        ("/review-queue/confirm", "btn--primary", "读得对，下一道"),
        ("/review-queue/reject", "btn--secondary", "真的不收"),
    ], forms
    assert forms[0][1] == forms[1][1], "两个表单必须打同一条题"


def test_last_item_says_collect_in(tmp_path, monkeypatch):
    """末题（只剩 1 道）主按钮换「读得对，收进去」（蓝图 E.6 逐字）。"""
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    db = tmp_path / "one.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app):
        pass
    _add_doubtful(db, "最后一道")
    with TestClient(app) as c:
        html = c.get("/review-queue").text
    assert "读得对，收进去" in html
    assert "读得对，下一道" not in html


def test_focus_block_links_and_reject_details(client):
    """G2-4 升级：焦点区两个链接（改一下 → 编辑页 / 看原图 → 详情页），
    「这道不收」是 <details> 二次确认（B.5），确认后才提交 reject。"""
    html = client.get("/review-queue").text

    edit = EDIT_LINK.findall(html)
    view = VIEW_LINK.findall(html)
    assert len(edit) == 1, f"焦点区恰好一个「改一下」：{edit}"
    assert len(view) >= 1, "缺「看原图」"
    assert edit[0] == view[0], "改一下 / 看原图必须指同一道题"

    assert '<details class="reject-box">' in html
    assert '<summary class="linklike">这道不收</summary>' in html
    # reject 表单必须在 details 里（先确认再提交）
    details = html.split('<details class="reject-box">', 1)[1]
    assert 'action="/review-queue/reject"' in details
    assert "真的不收" in details


def test_the_confirm_button_is_the_only_primary(client):
    """G2-4 升级：有题时全页唯一主按钮 = 「读得对，下一道」。"""
    html = client.get("/review-queue").text

    assert html.count('class="btn btn--primary"') == 1
    assert "读得对，下一道" in html


def test_title_and_queue_counter(client):
    """标题带进度「第 1/3 道」，后面排的题给一行「后面还有 2 道」。"""
    html = client.get("/review-queue").text

    assert f"看看读得对不对（第 1/{PENDING_COUNT} 道）" in html
    assert f"后面还有 {PENDING_COUNT - 1} 道" in html


def test_machine_values_line_keeps_the_essentials(client):
    """G2-4 升级：机器值行「机器读的 · 科目」；低置信度翻译成人话（V3.1b）。
    题干大字是焦点题。"""
    html = client.get("/review-queue").text

    assert "机器读的 · 数学 · 机器不太确定，请核对" in html
    assert LONG_STEM in html, "焦点题的题干没上大字"


def test_blocked_items_explain_and_have_no_buttons(client):
    """G2-4 升级：还在等的那批只列题 + 一句人话（同类的题才学会），没有动作按钮。"""
    html = client.get("/review-queue").text

    assert "还在等的 1 道" in html
    assert "还要再答对一道同类的题，才算学会" in html
    # 旧标签/红线词不许还魂（种子内容里含「缺变式的题」字样不算标签）
    assert "缺变式卡住" not in html
    assert "出库" not in html and "复核" not in html and "队列" not in html

    blocked = html.split("还在等的", 1)[1]
    assert "<button" not in blocked, "还在等的那批不该有按钮"
    assert "<form" not in blocked


def test_empty_queue_uses_the_three_part_empty_state(empty_client):
    """G2-4 升级：空态三段式（§4.3）——现在没有 / 为什么 / 去练一题。"""
    html = empty_client.get("/review-queue").text

    assert "现在没有要确认的题" in html
    assert "机器读完题，拿不准的会排到这里，等你看一眼。" in html
    assert html.count('class="btn btn--primary"') == 1
    assert '<a class="btn btn--primary" href="/practice">去练一题</a>' in html
    assert '<div class="counts">' not in html
    assert "<table" not in html


def test_actions_css_keeps_primary_full_width():
    """G2-4 升级：动作区整列、按钮 48 高（v3 红线），锁到活的 v3 样式。"""
    css = V3_CSS.read_text(encoding="utf-8")

    def declarations(selector: str) -> dict[str, str]:
        found = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
        assert found is not None, f"v3.css 里没有这条规则：{selector}"
        return {
            prop.strip(): " ".join(value.split())
            for prop, value in re.findall(r"([-\w]+)\s*:\s*([^;]+)", found.group(1))
        }

    actions = declarations(".select-actions")
    assert actions.get("display") == "flex", actions
    assert actions.get("flex-direction") == "column", actions
    button = declarations(".btn")
    assert button.get("height") == "var(--v3-btn-h)", button


# ------------------------------------------------- AC4 真浏览器 390 实测


def _browser_python() -> str | None:
    """找一个装了 playwright 的解释器（与 test_pages_visual 同口径）。"""
    candidates = [
        os.environ.get("CAIRN_BROWSER_PYTHON"),
        "/usr/bin/python3",
        shutil.which("python3"),
    ]
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

# **is_mobile=False**：移动仿真开着时浏览器会把视口一起放大，撑到 492 也会报 390
# （W1 被这个假绿骗过一次）。这里量的是真视口。
_BROWSER = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, shot = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=False)
    page.goto(base + "/review-queue", wait_until="load")
    page.wait_for_selector(".confirm-stem")
    page.screenshot(path=shot, full_page=True)

    metrics = page.evaluate(
        """() => {
            const de = document.documentElement;
            const stem = document.querySelector(".confirm-stem").getBoundingClientRect();
            const primary = document.querySelector(".btn--primary").getBoundingClientRect();
            const links = [...document.querySelectorAll(".confirm-links a")].map((a) => {
                const r = a.getBoundingClientRect();
                return {text: a.textContent.trim(), right: Math.round(r.right)};
            });
            return {
                scrollWidth: de.scrollWidth,
                clientWidth: de.clientWidth,
                innerWidth: window.innerWidth,
                stemRight: Math.round(stem.right),
                primaryText: document.querySelector(".btn--primary").textContent.trim(),
                primaryRight: Math.round(primary.right),
                links: links,
            };
        }"""
    )
    browser.close()
print(json.dumps(metrics, ensure_ascii=False))
'''


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _live_server(app):
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


def _measure(app, tmp_path: Path) -> dict:
    script = tmp_path / "browse_queue.py"
    script.write_text(_BROWSER, encoding="utf-8")
    with _live_server(app) as base:
        done = subprocess.run(
            [BROWSER_PY, str(script), base, str(SHOT_390)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    assert done.returncode == 0, f"浏览器脚本失败：\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_browser
def test_390_viewport_has_no_overflow(app_for_queue, tmp_path):
    """AC4：固定 390 视口 `scrollWidth == 390`，并留一张截图。"""
    metrics = _measure(app_for_queue, tmp_path)

    assert metrics["scrollWidth"] == 390, f"390 宽下有横向溢出：{metrics}"
    assert metrics["clientWidth"] == 390, metrics
    assert metrics["innerWidth"] == 390, metrics
    assert metrics["primaryText"] == "读得对，下一道", metrics
    assert SHOT_390.is_file(), f"截图没生成：{SHOT_390}"


@needs_browser
def test_focus_controls_fit_at_390(app_for_queue, tmp_path):
    """AC4：390 下焦点区的主按钮与两个链接都放得下（右边界不出 390）。"""
    metrics = _measure(app_for_queue, tmp_path)

    assert metrics["primaryRight"] <= 390, f"主按钮越出 390：{metrics}"
    assert metrics["stemRight"] <= 390, f"题干越出 390：{metrics}"
    assert len(metrics["links"]) == 2, metrics
    for link in metrics["links"]:
        assert link["right"] <= 390, f"链接越出 390：{link}"
    print(f"390×844 截图：{SHOT_390} | {json.dumps(metrics, ensure_ascii=False)}")


# ------------------------------------------------ 改一下（E.7）+ 等待屏计时（B.4）


def _seed_one_question(tmp_path: Path, monkeypatch) -> tuple[object, int, int]:
    """一张假照片 + 一条待确认错题；返回 (app, question_id, scan_id)。"""
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    db = tmp_path / "edit.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app):
        pass
    item_id, question_id = _add_doubtful(db, "原题干")
    with _session(db) as session:
        learner_id = session.scalars(select(Learner).limit(1)).first().id
        from cairn.models import Scan
        from cairn.models.learner import utcnow as _utcnow

        scan = Scan(
            learner_id=learner_id,
            device_id="upload",
            device_name="测试",
            width=400,
            height=100,
            captured_at=_utcnow(),
            path="scans/fake.png",
            source="upload",
        )
        session.add(scan)
        session.commit()
        scan_id = scan.id
    image = tmp_path / "fake.png"
    image.write_bytes(b"fake")
    (tmp_path / "scans").mkdir()
    image.rename(tmp_path / "scans" / "fake.png")
    return app, question_id, scan_id


def test_edit_page_returns_to_queue_when_asked(tmp_path, monkeypatch):
    """E.7：改一下页两按钮逐字；?back=queue 时保存与返回都回待确认屏。"""
    app, question_id, _scan_id = _seed_one_question(tmp_path, monkeypatch)
    with TestClient(app) as c:
        html = c.get(f"/questions/{question_id}/edit?back=queue").text
        assert "改一下这道题" in html
        assert "改好了" in html
        assert "不改了，返回" in html
        assert f'action="/questions/{question_id}/edit?back=queue"' in html
        for field in ('name="content"', 'name="answer"', 'name="analysis"'):
            assert field in html, field
        assert 'href="/review-queue"' in html, "「不改了，返回」应回待确认屏"

        # 保存：303 回待确认屏（老调用方不带 back 的行为一字不变，见 test_question_edit）
        resp = c.post(
            f"/questions/{question_id}/edit?back=queue",
            data={"content": "改后的题干", "answer": "", "analysis": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/review-queue"

        # 题干空：400 且留在编辑页（可读提示，原样回填）
        resp = c.post(
            f"/questions/{question_id}/edit?back=queue",
            data={"content": "  ", "answer": "", "analysis": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "题干不能为空" in resp.text
        assert "改一下这道题" in resp.text


def test_reading_page_exposes_the_two_timer_hooks(tmp_path, monkeypatch):
    """B.4：30 秒露「重试」、90 秒露「跳过这道」——钩子与阈值逐字在渲染结果里。"""
    app, _question_id, scan_id = _seed_one_question(tmp_path, monkeypatch)
    with TestClient(app) as c:
        html = c.get(f"/scans/{scan_id}/ocr").text

    assert "正在读题" in html
    assert "通常 10-30 秒" in html
    assert 'id="reading-slow" href="/scans/%d/ocr" hidden' % scan_id in html
    assert 'id="reading-skip" href="/review-queue" hidden' in html
    assert "有点慢，点这里重试这道题" in html
    assert "这道题先跳过，稍后在『待你确认』里找它" in html
    # 计时阈值与测试钩子（条件可测：window.readingShowSlow / readingShowSkip）
    assert "setTimeout(showSlow, 30000)" in html
    assert "setTimeout(showSkip, 90000)" in html
    assert "window.readingShowSlow = showSlow" in html
    assert "window.readingShowSkip = showSkip" in html
    # 等待屏无主按钮（去向是链接，不是主行动）
    assert 'class="btn btn--primary"' not in html
