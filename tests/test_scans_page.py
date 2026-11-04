"""V1 · 「已拍的卷子」页面级改造（第一批第 1 页）。

这一页是后面 11 页的样板，所以断言分成三层：

- **结构层**（不走浏览器）：没有 `<table>`、页头只有一个主按钮、每行是一个 `<a class="list-row">`、
  汇总格卡三个数从 `scans / stages` 就地算出来。这些用真 HTTP 响应 + 字符串/正则验。
- **视觉层**（真浏览器）：固定 390 视口（`is_mobile=False`，不靠移动仿真把视口撑宽）
  量 `documentElement.scrollWidth == 390`、三格是否一行、数字字号；1440 下量最大内容宽并截图。
  键盘焦点环（`:focus-visible`）也只能在真浏览器里量。
- **文案层**：页头 kicker / h1 / lede、行内三种步骤文案逐字锁死——换皮不许改字。

数据一律现造：5 张照片 = 3 张待裁边 + 1 张待选题 + 1 张已收好（1 道题），
于是汇总格卡必须是 `5 共 张 / 4 待处理 / 1 已收好`。
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
from PIL import Image
from sqlalchemy import select

from cairn import web as web_module
from cairn.models import Learner, Question, Scan, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.models.scan import ScanQuestion
from cairn.web import create_app

_SRC = Path(web_module.__file__).parent
TEMPLATE = _SRC / "templates" / "scans.html"
COMPONENTS_CSS = _SRC / "static" / "components.css"

WIDTH, HEIGHT = 900, 1200
# 3 张待裁边 + 1 张待选题 + 1 张已收好 → 共 5 / 待处理 4 / 已收好 1
PLAN = ("crop", "crop", "crop", "select", "ready")
DEVICE = "浏览器上传"
SHOT_390 = Path("/tmp/v1_scans_390.png")
SHOT_1440 = Path("/tmp/v1_scans_1440.png")


# ------------------------------------------------------------------ 造数据


def _seed(db_path: Path) -> dict[str, list[int]]:
    """按 PLAN 造 5 张照片：sidecar 与 scan_question 行都直接写库，不跑检测器。"""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    ids: dict[str, list[int]] = {"crop": [], "select": [], "ready": []}
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            for index, stage in enumerate(PLAN):
                rel = f"scans/2026/11/v1-{index}.jpg"
                target = db_path.parent / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (WIDTH, HEIGHT), "white").save(target, format="JPEG", quality=90)
                scan = Scan(
                    learner_id=learner.id,
                    device_id="browser-upload-9f3c2a71",
                    device_name=DEVICE,
                    width=WIDTH,
                    height=HEIGHT,
                    # 后插入的更新：列表「新的在前」才是可断言的
                    captured_at=utcnow().replace(microsecond=index),
                    path=rel,
                    source="upload",
                )
                session.add(scan)
                session.commit()
                ids[stage].append(scan.id)
                if stage == "crop":
                    continue  # 待裁边：图旁边没有 sidecar
                target.with_suffix(".crop.json").write_text(
                    '{"reason": "ok", "area_ratio": 0.44, "note": "完成"}', encoding="utf-8"
                )
                if stage != "ready":
                    continue
                # 收进去一道题：stored_question_count 数的是 scan_question.question_id，
                # 只写切题行、不回链题目，stage_for 仍会判成「待选题」。
                question = Question(
                    learner_id=learner.id,
                    subject="数学",
                    grade_level="primary-3",
                    question_type="计算",
                    content="1. 计算 1 + 1 =",
                    answer="2",
                )
                session.add(question)
                session.commit()
                session.add(
                    ScanQuestion(
                        scan_id=scan.id,
                        no=1,
                        bbox_json='{"bbox": [0, 0, 10, 10], "page": [900, 1200], "angle": 0}',
                        stem_text="1. 计算 1 + 1 =",
                        is_wrong=True,
                        question_id=question.id,
                    )
                )
                session.commit()
        return ids
    finally:
        engine.dispose()


@pytest.fixture()
def app_for_scans(tmp_path, monkeypatch):
    """真文件库 + 5 张照片；OCR 关掉，页面只走降级路径。"""
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    db = tmp_path / "scans.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app):
        pass  # 先让库把表建出来
    _seed(db)
    return app


@pytest.fixture()
def client(app_for_scans):
    with TestClient(app_for_scans) as c:
        yield c


@pytest.fixture()
def empty_client(tmp_path, monkeypatch):
    """一张都没拍的库：空状态那一屏。"""
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    db = tmp_path / "empty.db"
    with TestClient(create_app(f"sqlite:///{db}")) as c:
        yield c


def _summary_cells(html: str) -> list[tuple[str, str]]:
    """汇总格卡里的 (数字, 标签)，按页面顺序。"""
    return [
        (" ".join(value.split()), " ".join(label.split()))
        for value, label in re.findall(
            r'<div class="summary__item">\s*'
            r'<span class="summary__value">([^<]*)</span>\s*'
            r'<span class="summary__label">([^<]*)</span>\s*</div>',
            html,
        )
    ]


def _rows(html: str) -> list[tuple[str, str]]:
    return re.findall(r'<a class="list-row" href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)


# ------------------------------------------------------------------ 结构层


def test_template_extends_the_shell_and_drops_its_own_skeleton():
    """AC：整页改成 extends _base，模板里不再有自己的骨架，也没有表格。"""
    source = TEMPLATE.read_text(encoding="utf-8")

    assert source.startswith('{% extends "_base.html" %}')
    for tag in ("<!DOCTYPE", "<head", "<nav", "<style", "<table", "<tr", "<td", "style="):
        assert tag not in source, f"scans.html 还有：{tag}"
    assert '{% extends "_v3.html" %}' in (TEMPLATE.parent / "_base.html").read_text(encoding="utf-8")
    assert source.count("{% block main %}") == 1


def test_page_html_contains_no_table(client):
    """AC2：整页 HTML 不许出现 `<table`（行式列表替代六列表格）。"""
    html = client.get("/scans").text

    assert "<table" not in html
    assert "<tr" not in html
    assert "<td" not in html


def test_page_head_says_where_you_are_and_what_to_do(client):
    """页头文案逐字：kicker / h1 / lede，一行一个说法。"""
    html = client.get("/scans").text

    assert html.count("<h1") == 1
    assert "<h1>已拍的卷子</h1>" in html
    assert '<p class="kicker">卷子 · 已拍</p>' in html
    assert '<p class="lede">按拍摄时间从新到旧排；每行点进去是那张照片的详情。</p>' in html
    assert "这一页只指路，改动在照片详情页里做。" in html


def test_exactly_one_primary_button_links_to_upload(client):
    """AC4：全页只有一个主按钮，且它在页头动作区、指向 /upload。"""
    html = client.get("/scans").text

    assert html.count('class="btn btn--primary"') == 1
    actions = re.search(r'<div class="page-head__actions">(.*?)</div>', html, re.S)
    assert actions is not None, "页头没有动作区"
    body = actions.group(1)
    assert body.count("<a ") == 1, f"页头动作区只许一个出口：{body.strip()}"
    assert '<a class="btn btn--primary" href="/upload">拍一张</a>' in body


def test_summary_cards_are_computed_from_scans_and_stages(client):
    """AC2：三格从 scans / stages 就地算——共 5 张 / 待处理 4 / 已收好 1。"""
    html = client.get("/scans").text

    assert '<div class="counts">' not in html, "计数条已换成 .summary 三格"
    assert html.count('<div class="summary">') == 1
    assert html.count('class="summary__value"') == 3
    assert _summary_cells(html) == [("5", "共 张"), ("4", "待处理"), ("1", "已收好")]


def test_every_row_is_one_link_with_a_thumb_and_a_step(client):
    """AC6：每行一个 `<a href="/scans/{id}">`，行内 0 个 button，步骤列三选一。"""
    html = client.get("/scans").text
    rows = _rows(html)

    assert len(rows) == 5, f"应该有 5 行，实际 {len(rows)}"
    steps = []
    for href, body in rows:
        assert re.fullmatch(r"/scans/\d+", href), href
        assert "<button" not in body, "列表页不放按钮，整行只是指路"
        assert body.count("<a ") == 0, "行里不许有第二个出口"
        assert 'class="thumb"' in body, f"这一行没有缩略图：{body[:60]}"
        assert f'src="/scans/{href.rsplit("/", 1)[1]}/image"' in body
        assert 'class="list-row__main"' in body and 'class="list-row__meta"' in body
        step = re.search(r'<span class="list-row__step">(.*?)</span>', body, re.S)
        assert step is not None, f"这一行没有步骤列：{body[:80]}"
        steps.append(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", step.group(1))).strip())

    assert sorted(steps) == sorted(["待裁边", "待裁边", "待裁边", "待选题", "已收好 1"]), steps


def test_rows_are_newest_first(client):
    """列表按拍摄时间从新到旧：后插入的 id 更大，必须排在前面。"""
    html = client.get("/scans").text
    ids = [int(href.rsplit("/", 1)[1]) for href, _ in _rows(html)]

    assert ids == sorted(ids, reverse=True), ids


def test_row_shows_device_size_and_file_name_not_the_full_path(client):
    """AC2：次行是「设备 · 分辨率 · 文件名」三段，整条落盘路径留在详情页。"""
    html = client.get("/scans").text
    metas = re.findall(r'<span class="list-row__meta">(.*?)</span>', html, re.S)

    assert len(metas) == 5
    for meta in metas:
        text = meta.strip()
        # 三段、两个「 · 」：设备 / 宽×高 / 文件名
        assert text.count(" · ") == 2, text
        device, size, name = text.split(" · ")
        assert device == DEVICE, text
        assert size == f"{WIDTH}×{HEIGHT}", text
        # 文件名只取 basename：带 .jpg/.png，但不带目录
        assert re.fullmatch(r"v1-\d\.(jpg|png)", name), text
        assert "scans/2026/11" not in meta, "落盘路径不该出现在列表里"


def test_empty_library_uses_the_state_block(empty_client):
    """AC3：空态是一个 `.state`——标题 / 说明（带 /entry 链接）/ 动作行，主按钮恰好一个。"""
    html = empty_client.get("/scans").text

    assert '<div class="summary">' not in html
    assert html.count('<div class="state">') == 1
    assert '<p class="state__title">还没有拍过一张纸。</p>' in html
    # 说明：一行话 + 一个真链接，文案逐字
    detail = re.search(r'<p class="state__detail">(.*?)</p>', html, re.S)
    assert detail is not None, "空状态没有说明行"
    detail_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", detail.group(1))).strip()
    assert detail_text == "也可以从 手动输入 开始 → /entry", detail_text
    assert '<a href="/entry">手动输入</a>' in detail.group(1)
    # 动作行：只放一个出口，就是那个主按钮
    actions = re.search(r'<div class="state__actions">(.*?)</div>', html, re.S)
    assert actions is not None, "空状态没有动作行"
    assert actions.group(1).count("<a ") == 1, actions.group(1).strip()
    assert '<a class="btn btn--primary" href="/upload">拍第一张</a>' in actions.group(1)
    # 空态下页头那个「拍一张」不渲染，全页主按钮恰好一个
    assert 'class="page-head__actions"' not in html
    assert "拍一张" not in html
    assert html.count('class="btn btn--primary"') == 1
    assert "<table" not in html


def test_state_block_covers_empty_loading_and_error_in_css():
    """AC5：`.state--error` / `.state--loading` 这一卡只写 CSS、不渲染，但两条规则必须在。

    两态各只改一处颜色（error 换标题、loading 换说明）；骨架共用 `.state`，
    所以它们必须写成 `.state--x .state__y`，不能另起一套布局。
    """
    css = COMPONENTS_CSS.read_text(encoding="utf-8")

    assert re.search(
        r"\.state--error\s+\.state__title\s*\{[^}]*color:\s*var\(--color-danger\)", css
    ), "缺 .state--error 的标题色"
    assert re.search(
        r"\.state--loading\s+\.state__detail\s*\{[^}]*color:\s*var\(--color-text-tertiary\)", css
    ), "缺 .state--loading 的说明色"
    # 这一卡不渲染这两态：页面上不该出现（渲染了就说明有页面提前用了）
    assert ".state--error" not in TEMPLATE.read_text(encoding="utf-8")


def test_list_row_focus_rule_uses_the_focus_token():
    """AC4 的静态半边：焦点环是 2px 实线 var(--focus)，offset 2px。"""
    css = COMPONENTS_CSS.read_text(encoding="utf-8")

    rule = re.search(r"\.list-row:focus-visible\s*\{([^}]*)\}", css)
    assert rule is not None, "components.css 里没有 .list-row:focus-visible"
    declarations = dict(
        (prop.strip(), " ".join(value.split()))
        for prop, value in re.findall(r"([-\w]+)\s*:\s*([^;]+)", rule.group(1))
    )
    assert declarations.get("outline") == "2px solid var(--focus)", declarations
    assert declarations.get("outline-offset") == "2px", declarations


# ------------------------------------------------- AC3 / AC8 真浏览器实测


def _browser_python() -> str | None:
    """找一个装了 playwright 的解释器（与 test_pages_visual / test_select_page_ui 同口径）。"""
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

# 关键：**is_mobile=False**。移动仿真开着时，浏览器会把视口一起放大，
# 页面撑到 492 也会报 390（W1 就是被这个假绿骗过）。这里量的是真视口。
_BROWSER = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, shot, width, height = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": width, "height": height}, is_mobile=False)
    page.goto(base + "/scans", wait_until="load")
    page.wait_for_selector(".list-row")
    page.screenshot(path=shot, full_page=True)

    # 用真键盘 Tab 走到第一行：:focus-visible 只认键盘焦点，el.focus() 不算。
    for _ in range(80):
        page.keyboard.press("Tab")
        if page.evaluate(
            "() => !!document.activeElement && document.activeElement.classList.contains('list-row')"
        ):
            break

    metrics = page.evaluate(
        """() => {
            const de = document.documentElement;
            const pageEl = document.querySelector('.workspace');
            const rows = [...document.querySelectorAll('.list-row')];
            const first = rows[0].getBoundingClientRect();
            const style = getComputedStyle(rows[0]);
            const focused = document.activeElement;
            const focusStyle = getComputedStyle(focused);
            const cards = [...document.querySelectorAll('.summary__item')];
            const value = document.querySelector('.summary__value');
            const label = document.querySelector('.summary__label');
            const tertiary = document.querySelector('.list-row__meta');
            return {
                scrollWidth: de.scrollWidth,
                clientWidth: de.clientWidth,
                innerWidth: window.innerWidth,
                contentWidth: Math.round(pageEl.getBoundingClientRect().width),
                rowWidth: Math.round(first.width),
                rowHeight: Math.round(first.height),
                rowBorderEnd: style.borderBlockStartWidth,
                thumbWidth: document.querySelector(".thumb").getBoundingClientRect().width,
                thumbHeight: document.querySelector(".thumb").getBoundingClientRect().height,
                rowBackground: style.backgroundColor,
                primaries: document.querySelectorAll('.btn--primary').length,
                summaryCells: cards.length,
                summaryText: document.querySelector('.summary').innerText.replace(/\s+/g, ' ').trim(),
                summaryCellTops: cards.map((el) => Math.round(el.getBoundingClientRect().top)),
                summaryCellLefts: cards.map((el) => Math.round(el.getBoundingClientRect().left)),
                summaryDividers: cards.map((el) => getComputedStyle(el).borderInlineStartWidth),
                summaryValueFontSize: getComputedStyle(value).fontSize,
                summaryValueWeight: getComputedStyle(value).fontWeight,
                summaryValueNumeric: getComputedStyle(value).fontVariantNumeric,
                summaryLabelFontSize: getComputedStyle(label).fontSize,
                summaryLabelFamily: getComputedStyle(label).fontFamily,
                summaryLabelColor: getComputedStyle(label).color,
                tertiaryColor: getComputedStyle(tertiary).color,
                focusTag: focused.tagName,
                focusIsRow: focused.classList.contains('list-row'),
                focusOutlineWidth: focusStyle.outlineWidth,
                focusOutlineStyle: focusStyle.outlineStyle,
                focusOutlineColor: focusStyle.outlineColor,
                focusOutlineOffset: focusStyle.outlineOffset,
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
    """浏览器只能走 HTTP，TestClient 不够用（与 test_pages_visual 同一套）。"""
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


def _measure(app, tmp_path: Path, shot: Path, width: int, height: int) -> dict:
    script = tmp_path / f"browse_{width}.py"
    script.write_text(_BROWSER, encoding="utf-8")
    with _live_server(app) as base:
        done = subprocess.run(
            [BROWSER_PY, str(script), base, str(shot), str(width), str(height)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    assert done.returncode == 0, f"浏览器脚本失败：\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_browser
def test_390_viewport_has_no_horizontal_overflow(app_for_scans, tmp_path):
    """AC2/AC3：固定 390 视口下 `scrollWidth == 390`，三格一行，并留一张截图。"""
    metrics = _measure(app_for_scans, tmp_path, SHOT_390, 390, 844)

    assert metrics["scrollWidth"] == 390, f"390 宽下有横向溢出：{metrics}"
    assert metrics["clientWidth"] == 390, metrics
    assert metrics["innerWidth"] == 390, metrics
    # 行式列表：1px 下边线、透明底（不是一张张卡片）
    assert metrics["rowBorderEnd"] == "1px", metrics
    assert metrics["thumbWidth"] == 52 and metrics["thumbHeight"] == 64, metrics
    assert metrics["rowBackground"] == "rgba(0, 0, 0, 0)", metrics
    assert metrics["primaries"] == 1, metrics
    assert SHOT_390.is_file(), f"截图没生成：{SHOT_390}"


@needs_browser
def test_summary_cards_stay_on_one_row_at_390(app_for_scans, tmp_path):
    """The three V3 summary cells stay in one row with readable values."""
    metrics = _measure(app_for_scans, tmp_path, SHOT_390, 390, 844)

    assert metrics["summaryCells"] == 3, metrics
    assert metrics["summaryText"] == "5 共 张 4 待处理 1 已收好", metrics
    # 一行：顶边相同、左边依次靠右
    assert len(set(metrics["summaryCellTops"])) == 1, f"390 下三格没排成一行：{metrics}"
    lefts = metrics["summaryCellLefts"]
    assert lefts == sorted(lefts) and len(set(lefts)) == 3, metrics
    # 格间 1px 竖线：第一格没有，后两格各一条
    assert metrics["summaryDividers"] == ["0px", "1px", "1px"], metrics
    # 数字 20/600 等宽数字；标签 11 等宽三级色
    assert metrics["summaryValueFontSize"] == "18px", metrics
    assert metrics["summaryValueWeight"] == "600", metrics
    assert "tabular-nums" in metrics["summaryValueNumeric"], metrics
    assert metrics["summaryLabelFontSize"] == "12px", metrics
    assert metrics["summaryLabelColor"] == metrics["tertiaryColor"], metrics


@needs_browser
def test_first_row_shows_a_focus_ring_when_tabbed_to(app_for_scans, tmp_path):
    """AC4：键盘 Tab 到第一行时，焦点环是 2px 实线、外扩 2px（outline，不是底色）。"""
    metrics = _measure(app_for_scans, tmp_path, SHOT_390, 390, 844)

    assert metrics["focusIsRow"] is True, f"Tab 之后焦点不在列表行上：{metrics}"
    assert metrics["focusOutlineWidth"] == "2px", metrics
    assert metrics["focusOutlineStyle"] == "solid", metrics
    assert metrics["focusOutlineOffset"] == "2px", metrics


@needs_browser
def test_1440_viewport_keeps_the_reading_column(app_for_scans, tmp_path):
    """At 1440px the workspace retains a bounded reading column."""
    metrics = _measure(app_for_scans, tmp_path, SHOT_1440, 1440, 900)

    assert 1000 <= metrics["contentWidth"] <= 1120, f"1440 下内容宽不对：{metrics}"
    assert metrics["scrollWidth"] == 1440, metrics
    assert SHOT_1440.is_file(), f"截图没生成：{SHOT_1440}"
