"""V2 · 「上传作业照片」页面级改造：只看页面这一层。

这一卡的重心是「样式换了、钩子和算法一个没动」，所以断言分两半：

- **页面骨架**（AC2/AC3/AC4）：外壳来自 `_base.html`，正文没有 `<table>`；
  六个钩子逐条在（id / name / action / enctype / accept / required）；
  全页 `.btn--primary` 恰好一个，且是 `#submit-btn`。
- **脚本资产**（AC5）：阈值常量与 `computeMetrics()` 的算式、两个监听逐字未变；
  输出从 `<table class="metrics">` 换成了 `.verdict` + `.facts` + `.list-row`。

AC6（390 无横滚）用真浏览器跑：选一张真图，断言 390 宽下 `scrollWidth == 390`，
并顺手确认三格数据真的被脚本画出来了（`.facts` 计数与格数）。
AC7 是人工截图取证（`/tmp/v2_upload_ok.png` / `/tmp/v2_upload_fail.png`），见 docs/EVIDENCE.md。

真图只用仓库里已有的 `docs/samples/hi-redbg.jpg`；小图在 tmp_path 里现造（200×200），
不往仓库里塞新样本。
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
from html.parser import HTMLParser
from pathlib import Path

import pytest

# 先 import uvicorn 再 import 应用，避免与 test_select_page_ui 的启动顺序互相影响
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image

from cairn.web import create_app

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "cairn" / "templates"
HI_REDBG = ROOT / "docs" / "samples" / "hi-redbg.jpg"


# ------------------------------------------------------------------ 钩子提取


class _Tags(HTMLParser):
    """把渲染结果里所有开始标签收成 [(tag, attrs), ...]；属性值 `None` 表示无值属性。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def _by_id(html: str, element_id: str) -> tuple[str, dict[str, str | None]]:
    parser = _Tags()
    parser.feed(html)
    found = [(tag, attrs) for tag, attrs in parser.tags if attrs.get("id") == element_id]
    assert len(found) == 1, f"#{element_id} 应该恰好出现一次，实际 {len(found)} 次"
    return found[0]


@pytest.fixture()
def upload_html(tmp_path, monkeypatch):
    """真文件库跑一遍 GET /upload：模板渲染结果就是页面本身。"""
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    app = create_app(f"sqlite:///{tmp_path / 'upload.db'}")
    with TestClient(app) as client:
        response = client.get("/upload")
    assert response.status_code == 200
    return response.text


# ------------------------------------------------------------ AC2 外壳 / 无表格


def test_the_page_comes_out_of_the_shell(upload_html):
    """G2-3 升级：骨架来自 v3 壳（viewport / 单 main / 引 v3.css / 不再挂旧样式）。"""
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in upload_html
    assert len(re.findall(r'<main class="main(?: [^"]+)?">', upload_html)) == 1
    assert re.search(r'<link rel="stylesheet" href="/static/v3\.css(?:\?[^" ]+)?">', upload_html)
    assert "tokens.css" not in upload_html
    assert "components.css" not in upload_html
    assert "<style" not in upload_html
    # v3 壳自带手机头部 + 底部四导航 + 桌面侧栏
    assert upload_html.count('class="tabbar"') == 1
    assert upload_html.count('class="shell__side"') == 1


def test_the_page_has_no_table_and_no_metrics_class(upload_html):
    """AC2：模板源码、渲染结果、脚本拼接串里都没有 <table>（旧版是 <table class="metrics">）。"""
    assert "<table" not in upload_html
    assert 'class="metrics"' not in upload_html
    assert "<tr" not in upload_html
    assert "<td" not in upload_html

    # 模板源码也查一遍：注释里写一句 `<table` 也会让 `grep -c '<table'` 不再为 0
    source = (TEMPLATES / "capture.html").read_text(encoding="utf-8")
    assert "<table" not in source


# ---------------------------------------------------------------- AC3 六个钩子


def test_the_six_hooks_are_intact(upload_html):
    """AC3：六个钩子逐条断言（后端与内联 JS 都按这些 id / name 取元素）。"""
    tag, form = _by_id(upload_html, "upload-form")
    assert tag == "form"
    assert form.get("action") == "/scans/upload"
    assert form.get("method") == "post"
    assert form.get("enctype") == "multipart/form-data"

    tag, file_input = _by_id(upload_html, "file-input")
    assert tag == "input"
    assert file_input.get("type") == "file"
    assert file_input.get("name") == "file"
    assert file_input.get("accept") == "image/*", "file input 少了 accept 属性"
    assert "required" in file_input, "file input 少了 required 属性"

    tag, quality = _by_id(upload_html, "quality-input")
    assert tag == "input"
    assert quality.get("type") == "hidden"
    assert quality.get("name") == "quality"

    assert _by_id(upload_html, "preview")[0] == "img"

    assert _by_id(upload_html, "verdict")[0] == "div"

    tag, submit = _by_id(upload_html, "submit-btn")
    assert tag == "button"
    assert submit.get("type") == "submit"


def test_the_form_actually_wraps_both_inputs_and_the_button(upload_html):
    """AC3 附带：三个提交要素必须在同一个 <form> 里（否则按钮提交不到那两个字段）。"""
    body = upload_html.split('id="upload-form"', 1)[1].split("</form>", 1)[0]

    for hook in ('id="file-input"', 'id="quality-input"', 'id="submit-btn"'):
        assert hook in body, f"{hook} 跑到表单外面了"


# ------------------------------------------------------- AC4 全页只有一个主按钮


def test_only_one_primary_button_and_it_is_the_pick_label(upload_html):
    """G2-3 升级：全页唯一主按钮是「拍照」（触发选图）；#submit-btn 是次按钮且初始置灰。

    旧断言锁的是「主按钮=提交键」；v3 蓝图 E.2 定稿主按钮为「拍照」，提交键降为
    续步（选好图后由脚本解禁）。唯一主行动这条红线不变，还多了「初始禁用」。
    """
    assert upload_html.count('class="btn btn--primary"') == 1

    tag, pick = _by_id(upload_html, "file-input")
    assert tag == "input"  # 钩子没动
    assert '<label class="btn btn--primary" for="file-input">' in upload_html
    assert "拍照" in upload_html

    _, submit = _by_id(upload_html, "submit-btn")
    assert submit.get("class") == "btn btn--secondary"
    assert "disabled" in submit, "没选图前续步必须置灰"

    # 唯一主按钮就在选图处；「已拍的卷子」是文字链、不占主按钮位
    assert 'class="btn--ghost" href="/scans"' in upload_html or 'href="/scans">已拍的卷子' in upload_html
    assert upload_html.index("拍照") < upload_html.index("已拍的卷子")


# ------------------------------------------------------------ 页头 / 区块 / 页脚


def test_the_page_head_explains_how_to_photograph(upload_html):
    """The page explains how to capture the paper clearly."""
    assert '<h1 class="page-title">拍一张卷子</h1>' in upload_html
    assert '<p class="hint">页面铺平，光线均匀，四角都拍进来</p>' in upload_html
    assert "上传作业照片" not in upload_html, "旧页头文案还魂"


def test_the_blocks_and_the_footer_are_there(upload_html):
    """G2-3 升级：区块骨架 + 蓝图三件（拍照/从相册选/手动输入一道题）+ 页脚小字。"""
    for title in ("量出来的结果",):
        assert f'<span class="section__title">{title}</span>' in upload_html, title

    assert "电脑上也可以把截图粘贴到这页。" in upload_html
    assert "选中的这张，未做任何处理" in upload_html
    assert '<div class="select-actions" id="submit-row" hidden>' in upload_html  # V3.1b：选图后才展开
    # 「从相册选」永远在场（蓝图 B.1 相册是相机被拒时的退路）
    assert ' <label class="btn--ghost" for="file-input">从相册选</label>' in upload_html
    assert '<label class="btn--ghost" for="file-input">重新选一张</label>' in upload_html
    # 手动录入入口 = 文字链（PLAN G2-3/G2-6 默认方案）
    assert 'href="/entry">手动输入一道题' in upload_html
    assert "照片质量先在这台设备上检查；确认后照片会上传到 Cairn。" in upload_html


def test_capture_guide_has_a_labeled_heading(upload_html):
    """The guide has a heading and its aside points to that heading."""
    assert '<aside class="capture-guide" aria-labelledby="capture-guide-title">' in upload_html
    assert '<h2 class="capture-guide__title" id="capture-guide-title">' in upload_html

    static = re.sub(r"<script\b.*?</script>", "", upload_html, flags=re.S)
    assert static.count('<span class="section__title">') == 1  # 第 2 区块（第 1 区块 V3.1b 起不设标题）
    # 第 3 区块的标题在脚本里拼，同样是 span（合格时根本不调用）
    assert '\'<span class="section__title">哪一项不合格</span>\'' in upload_html


def test_the_figure_caption_does_not_occupy_space_before_a_photo_is_picked(upload_html):
    """V2-fix 5：未选图时 `.figure__cap` 带 `hidden`，选中后由脚本展开。"""
    assert '<p class="figure__cap" hidden>选中的这张，未做任何处理</p>' in upload_html
    assert "figureCap.hidden = false;" in upload_html


def test_the_five_copy_fixes_are_in_place(upload_html):
    """G2-3 升级：质量门三词保留；续步按钮与页脚换 v3 文案；旧文案彻底消失。

    `质量合格` / `暗部占比` / `过曝占比` 都只出现在脚本拼出的字符串里，所以查的是
    渲染后的整页（脚本原样进页面）。
    """
    for wanted in (
        "质量合格",
        "暗部占比",
        "过曝占比",
        "照片质量先在这台设备上检查；确认后照片会上传到 Cairn。",
        "用这张",
        "仍然用这张",
    ):
        assert wanted in upload_html, f"少了新文案：{wanted}"
    for gone in ("可以收进去", "不引第三方库", "上传作业照片"):
        assert gone not in upload_html, f"旧文案还在：{gone}"

    # 三格的标签顺序：清晰度 / 暗部占比 / 过曝占比
    captions = re.findall(r'facts__caption">([^<·]+) · ', upload_html)
    assert captions == ["清晰度", "暗部占比", "过曝占比"], captions


# ------------------------------------------------ AC5 常量 / 算法 / 监听逐字未变


def test_the_thresholds_and_the_algorithm_are_untouched(upload_html):
    """AC5：阈值常量与 computeMetrics() 的算式逐字存在（换的是输出，不是判定）。"""
    unchanged = (
        "const THRESH = {",
        "blur_var: 60,",
        "dark_ratio: 0.35,",
        "overexposure_ratio: 0.25,",
        "edge_density: 0.02,",
        "const GRAY_DARK = 60;",
        "const GRAY_BRIGHT = 245;",
        "const SHORT_SIDE = 512;",
        "const EDGE_GRAD = 100;",
        # 灰度加权、3×3 拉普拉斯、Sobel 三条算式各留一条指纹
        "0.299 * px[p] + 0.587 * px[p + 1] + 0.114 * px[p + 2]",
        "4 * gray[i] - gray[i - 1] - gray[i + 1] - gray[i - w] - gray[i + w]",
        "if (Math.sqrt(gx * gx + gy * gy) > EDGE_GRAD) edges++;",
    )
    missing = [fragment for fragment in unchanged if fragment not in upload_html]
    assert missing == [], f"这些脚本片段被改动了：{missing}"

    # MIN_SIDE 仍是模板变量下发的（前端提示与服务端硬拒同一条线）
    assert re.search(r"const MIN_SIDE = \d+;", upload_html), "MIN_SIDE 不是服务端下发的一个数"


def test_the_two_listeners_are_untouched(upload_html):
    """AC5：change 与 paste 两个监听逐字未变（本页没有 submit 监听，也没有新增）。"""
    assert "fileInput.addEventListener('change', () => {" in upload_html
    assert "document.addEventListener('paste', (event) => {" in upload_html
    assert "addEventListener('submit'" not in upload_html
    assert "fileInput.files = dt.files;" in upload_html, "粘贴那段 DataTransfer 被改过"


def test_the_result_is_written_as_verdict_and_facts(upload_html):
    """AC5 / 结构：脚本拼出来的是 .verdict + .facts 三格 + .list-row，不再是 table。"""
    for fragment in (
        'class="verdict',
        'class="verdict__label"',
        'class="verdict__meta"',
        # 两种坏结论各自的修饰类：tone 拼成 `verdict--warn` / `verdict--fail`
        "' verdict--' + tone",
        "blocked ? 'fail' : 'warn'",
        '<div class="facts">',
        'class="facts__cell"',
        'class="facts__value"',
        'class="facts__caption"',
        'class="facts__meta"',
        'class="list-row"',
        'class="list-row__main"',
        'class="list-row__meta"',
        "哪一项不合格",
    ):
        assert fragment in upload_html, f"脚本里少了 {fragment}"


def test_an_all_good_photo_leaves_no_reason_block(upload_html):
    """结构：第 3 区块由脚本条件渲染——静态骨架里没有「哪一项不合格」这一块。

    标题只出现在脚本的字符串里（合格时 `reasonsBlock()` 根本不调用），所以刚打开
    的空页面上不会有一个空的原因区块。把 `<script>` 整段摘掉再查，才不会把脚本
    里的字符串当成"页面上已经有这一块"。
    """
    static = re.sub(r"<script\b.*?</script>", "", upload_html, flags=re.S)
    assert "哪一项不合格" not in static
    assert "哪一项不合格" in upload_html, "脚本里连这条文案都没有"


# --------------------------------------------- AC6 真浏览器 390 宽 + 脚本接线


def _browser_python() -> str | None:
    """找一个装了 playwright 的解释器（与 test_select_page_ui 同一条口径）。"""
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

_BROWSER = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, big, small = sys.argv[1], sys.argv[2], sys.argv[3]
out = {}


def snapshot(page):
    return {
        "scroll": page.evaluate("document.documentElement.scrollWidth"),
        "facts": page.locator(".facts").count(),
        "cells": page.locator(".facts__cell").count(),
        "label": page.inner_text(".verdict__label"),
        "verdict_class": page.get_attribute(".verdict", "class"),
        "meta": page.inner_text(".verdict__meta"),
        "rows": page.locator(".list-row").count(),
        "row_main": page.locator(".list-row__main").first.inner_text()
        if page.locator(".list-row").count() else "",
        "button": page.inner_text("#submit-btn"),
        "disabled": page.eval_on_selector("#submit-btn", "el => el.disabled"),
        "cap_hidden": page.eval_on_selector(".figure__cap", "el => el.hidden"),
        "preview_h": page.evaluate(
            "document.getElementById('preview').getBoundingClientRect().height"
        ),
        "quality": page.evaluate("document.getElementById('quality-input').value"),
    }


with sync_playwright() as p:
    browser = p.chromium.launch()

    def run(sample):
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(base + "/upload", wait_until="load")
        empty = {
            "scroll": page.evaluate("document.documentElement.scrollWidth"),
            "facts": page.locator(".facts").count(),
            "label": page.locator(".verdict").count(),
            "cap_visible": page.is_visible(".figure__cap"),
        }
        page.set_input_files("#file-input", sample)
        page.wait_for_selector(".verdict", timeout=30000)
        shown = snapshot(page)
        page.close()
        return {"empty": empty, "shown": shown}

    # AC3 的第三种状态：同一页里 好图 → 小图 → 再选回好图，按钮的 disabled 要跟着回 false。
    def run_cycle():
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(base + "/upload", wait_until="load")

        page.set_input_files("#file-input", big)
        page.wait_for_function(
            "document.querySelector('.verdict__label')"
            " && document.querySelector('.verdict__label').textContent === '质量合格'",
            timeout=30000,
        )
        first = snapshot(page)

        page.set_input_files("#file-input", small)
        page.wait_for_function(
            "document.querySelector('.verdict--fail') !== null", timeout=30000
        )
        second = snapshot(page)

        page.set_input_files("#file-input", big)
        page.wait_for_function(
            "document.querySelector('.verdict__label')"
            " && document.querySelector('.verdict__label').textContent === '质量合格'",
            timeout=30000,
        )
        third = snapshot(page)
        page.close()
        return {"good": first, "tiny": second, "good_again": third}

    out["big"] = run(big)
    out["small"] = run(small)
    out["cycle"] = run_cycle()
    browser.close()

print(json.dumps(out, ensure_ascii=False))
'''


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _live_server(app):
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
def test_browser_390_selects_a_photo_and_draws_verdict_and_three_cells(tmp_path):
    """AC6（V2）/ AC3（V2-fix）：390 宽下 `scrollWidth == 390`；三格 + 结论条真的出现在页面上。

    三段真浏览器取证：
    - `hi-redbg.jpg`（1098×2560，短边够）→ 结论条「质量合格」+ 三格 + 机器值行，按钮可点；
    - 现造的 200×200 → `verdict--fail` + 一行原因 + 按钮变「换一张原图」且 `disabled`；
    - 同一页里再选回大图 → 结论条回「质量合格」，按钮 `disabled` 恢复 `false`。
    """
    small = tmp_path / "tiny.png"
    Image.new("RGB", (200, 200), "white").save(small, format="PNG")

    app = create_app(f"sqlite:///{tmp_path / 'browser.db'}")
    with TestClient(app):
        pass  # 让 lifespan 把表建出来

    script = tmp_path / "browse_upload.py"
    script.write_text(_BROWSER, encoding="utf-8")
    with _live_server(app) as base:
        done = subprocess.run(
            [BROWSER_PY, str(script), base, str(HI_REDBG), str(small)],
            capture_output=True,
            text=True,
            timeout=180,
        )
    assert done.returncode == 0, f"浏览器脚本失败：\n{done.stdout}\n{done.stderr}"
    seen = json.loads(done.stdout.strip().splitlines()[-1])

    big = seen["big"]
    assert big["empty"]["scroll"] == 390
    assert big["empty"]["facts"] == 0, "未选图时页面就画了结果"
    assert big["empty"]["label"] == 0
    assert big["empty"]["cap_visible"] is False, "未选图时图注占了位"
    assert big["shown"]["scroll"] == 390, f"390 宽下有横向溢出：{big['shown']['scroll']}"
    assert big["shown"]["facts"] == 1
    assert big["shown"]["cells"] == 3, "三格数据没画全"
    assert big["shown"]["label"] == "质量合格", big["shown"]["label"]
    assert big["shown"]["meta"].startswith("本机 · 短边 1098"), big["shown"]["meta"]
    assert big["shown"]["preview_h"] <= 320, big["shown"]["preview_h"]
    assert big["shown"]["cap_hidden"] is False, "选中后图注没展开"
    assert big["shown"]["disabled"] is False, "合格图不该把提交按钮置灰"
    assert '"blur_var"' in big["shown"]["quality"], "quality 字段没被脚本填上"

    short = seen["small"]
    assert short["shown"]["scroll"] == 390
    assert short["shown"]["cells"] == 3
    assert short["shown"]["label"] == "不合格，不能用"
    assert "verdict--fail" in short["shown"]["verdict_class"]
    assert short["shown"]["rows"] == 1, "短边不足应该只给一行原因"
    assert short["shown"]["row_main"] == "这张图太小，切题会漏"
    assert short["shown"]["button"] == "换一张"
    assert short["shown"]["disabled"] is True, "短边不足时提交按钮必须置灰"

    cycle = seen["cycle"]
    assert (cycle["good"]["label"], cycle["good"]["disabled"]) == ("质量合格", False)
    assert (cycle["good"]["button"],) == ("用这张",)
    assert (cycle["tiny"]["label"], cycle["tiny"]["disabled"]) == ("不合格，不能用", True)
    assert (cycle["good_again"]["label"], cycle["good_again"]["disabled"]) == (
        "质量合格",
        False,
    ), "重新选到合格图后按钮没恢复"
    assert cycle["good_again"]["facts"] == 1
    assert cycle["good_again"]["rows"] == 0, "换回合格图后原因行没被清掉"

    # AC3 要的三次状态原样打出来（pytest -s 可见，EVIDENCE 贴的就是这一段）
    print(
        "AC3 三次状态：",
        json.dumps(
            {
                name: {k: cycle[name][k] for k in ("label", "disabled", "button")}
                for name in ("good", "tiny", "good_again")
            },
            ensure_ascii=False,
        ),
    )
