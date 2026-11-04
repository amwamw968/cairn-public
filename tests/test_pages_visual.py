"""Check V3 page shells, navigation, layout and preserved page behavior."""

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

from cairn import scan_service
from cairn import web as web_module
from cairn.models import (
    REVIEW_STATUS_NEEDS_VARIANT,
    ErrorItem,
    Learner,
    Question,
    Scan,
    make_engine,
    make_session_factory,
)
from cairn.models.learner import utcnow
from cairn.models.scan import ScanQuestion
from cairn.ocr.cut import QuestionBlock
from cairn.web import create_app

_SRC = Path(web_module.__file__).parent
TEMPLATES = _SRC / "templates"
BASE_CSS = _SRC / "static" / "base.css"

# No templates need an inline style exception.
PENDING_STYLE_PAGES = set()

WIDTH, HEIGHT = 900, 1200
SHOT = "scans/2026/11/pages.jpg"

# 浏览器端 JSON.stringify 出来的质量指标：**一行、逗号后没有空格**。
# 这串是一个 200 多字符的不可断 token，正是它把老表格的最小内容宽顶到 900+。
QUALITY_JSON = (
    '{"brightness":214.6,"dark_ratio":0.081,"overexposure_ratio":0.012,'
    '"blur_var":132.44,"edge_density":0.183}'
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """真文件库 + 一张能解码的整页图；OCR 关掉，页面只走降级路径。"""
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    db = tmp_path / "pages.db"
    target = tmp_path / SHOT
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (WIDTH, HEIGHT), "white").save(target, format="JPEG", quality=95)
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as c:
        yield c, db


def _seed(db_path: Path) -> tuple[int, int]:
    """一张照片 + 两条切题行 + 一道错题：详情页不用现跑 OCR。"""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            scan = Scan(
                learner_id=learner.id,
                device_id="upload",
                device_name="浏览器上传",
                width=WIDTH,
                height=HEIGHT,
                captured_at=utcnow(),
                path=SHOT,
                source="upload",
                quality_json=QUALITY_JSON,
            )
            session.add(scan)
            session.commit()
            scan_service.store_questions(
                session,
                scan.id,
                [
                    QuestionBlock(
                        no=no,
                        bbox=(40, 40 + (no - 1) * 260, 860, 260 + (no - 1) * 260),
                        stem_text=f"{no}. 计算 {no} + {no} =",
                        line_indexes=[0],
                    )
                    for no in (1, 2)
                ],
                page_width=WIDTH,
                page_height=HEIGHT,
            )
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
            return scan.id, question.id
    finally:
        engine.dispose()


# ------------------------------------------------------------------ AC2 内联样式


def _style_counts() -> dict[str, int]:
    return {p.name: p.read_text(encoding="utf-8").count("<style") for p in TEMPLATES.glob("*.html")}


def test_no_template_keeps_an_inline_style_block():
    """Templates should not embed stylesheet blocks."""
    counts = _style_counts()

    offenders = {name: n for name, n in counts.items() if n and name not in PENDING_STYLE_PAGES}
    assert offenders == {}, f"这些模板还留着内联 <style>：{offenders}"

    # No remaining exception is expected.
    pending = {name: counts[name] for name in PENDING_STYLE_PAGES}
    assert pending == {}, pending


def test_no_template_hardcodes_a_color():
    """AC5：颜色只能来自 tokens.css；模板里不许出现 #rgb / #rrggbb。"""
    hex_color = re.compile(r"#[0-9a-fA-F]{3,6}\b")

    offenders = sorted(
        p.name
        for p in TEMPLATES.glob("*.html")
        if p.name not in PENDING_STYLE_PAGES and hex_color.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == []


# ------------------------------------------------------- AC2 另一半：改引样式

HTML_PAGES = [
    "/entry",
    "/practice",
    "/books",
    "/upload",
    "/tools",
    "/tutor",
    "/review-queue",
    "/stats",
    "/exam",
    "/import",
    "/devices",
    "/scans",
    "/settings",
]

# 这两条不是页面、是 JSON 响应（/strategies 列策略、/export 下备份），AC3 只要求 200
JSON_ENDPOINTS = ["/strategies", "/export"]

# G2 起逐步换 v3 壳的页面：引 v3.css、不再引 tokens/base（每换一页挪一个进来）
V3_PAGES = HTML_PAGES


def _has_stylesheet(html: str, name: str) -> bool:
    return bool(re.search(r'<link rel="stylesheet" href="/static/' + re.escape(name) + r'(?:\?[^" ]+)?">', html))


@pytest.mark.parametrize("path", HTML_PAGES)
def test_every_plain_page_links_its_shell_stylesheets(client, path):
    """AC2 的另一半：模板不是"把 <style> 删了"就算完，得真的改引对应的壳样式。

    旧壳页锁 tokens+base 两份；v3 壳页锁 v3.css 一份且不得再引旧样式
    （G2-3 起 /upload 走 v3，后续页面逐个挪进 V3_PAGES）。
    """
    c, _ = client
    html = c.get(path, follow_redirects=True).text

    if path in V3_PAGES:
        assert _has_stylesheet(html, "v3.css"), path
        assert "tokens.css" not in html and "base.css" not in html, path
    else:
        assert _has_stylesheet(html, "tokens.css"), path
        assert _has_stylesheet(html, "base.css"), path


def test_detail_pages_link_their_shell_stylesheets(client):
    """详情页同样锁壳样式：旧壳引两份，v3 壳引一份（/scans/{id}/ocr 已换 v3）。"""
    c, db = client
    scan_id, question_id = _seed(db)

    for path in (
        f"/scans/{scan_id}",
        f"/scans/{scan_id}/questions",
        f"/scans/{scan_id}/frame",
    ):
        html = c.get(path, follow_redirects=True).text
        assert _has_stylesheet(html, "v3.css"), path
        assert "tokens.css" not in html and "base.css" not in html, path

    # G2-4/G2-6 起 ocr 与错题详情走 v3 壳
    for path in (f"/scans/{scan_id}/ocr", f"/questions/{question_id}"):
        html = c.get(path, follow_redirects=True).text
        assert _has_stylesheet(html, "v3.css"), path
        assert "tokens.css" not in html and "base.css" not in html, path


# ------------------------------------------------------------------ AC3 页面 200


@pytest.mark.parametrize("path", HTML_PAGES + JSON_ENDPOINTS)
def test_every_plain_page_answers_200(client, path):
    """AC3：不带 id 的页面一律 200（/strategies 与 /export 是 JSON，同样 200）。"""
    c, _ = client
    assert c.get(path, follow_redirects=True).status_code == 200


def test_root_renders_today_page(client):
    """G2-1 升级（断言只强不弱）：`/` 不再 303 → /entry，单次 GET 就是 200 的 v3 今天页。

    旧断言锁的是 303 + location + 跟随后 200；行为被新页面顶掉后升级为：不重定向、
    直接 200、渲染的是 v3 壳（引 v3.css、四入口在场）。跟随后 200 这条保留。
    """
    c, _ = client

    root = c.get("/", follow_redirects=False)
    assert root.status_code == 200
    assert "location" not in {k.lower() for k in root.headers}
    assert c.get("/", follow_redirects=True).status_code == 200
    html = root.text
    assert _has_stylesheet(html, "v3.css")
    for label in ("今天", "错题本", "拍卷子", "设置"):
        assert label in html


def test_detail_pages_answer_200(client):
    """AC3：带 id 的详情页各一条（照片、整页识别、切题结果、框选、错题详情）。"""
    c, db = client
    scan_id, question_id = _seed(db)

    for path in (
        f"/scans/{scan_id}",
        f"/scans/{scan_id}/ocr",
        f"/scans/{scan_id}/questions",
        f"/scans/{scan_id}/frame",
        f"/questions/{question_id}",
    ):
        assert c.get(path, follow_redirects=True).status_code == 200, path


# ------------------------------------------------------------------ AC4 文案不变


COPY = {
    # G2-6：录入页换 v3（手动输入一道题），红线词（题型/语种/讲解）清零
    "/entry": (
        "手动输入一道题",
        "整段粘贴",
        "一道一道填",
        "行首编号和 Unit/Lesson 会自动去掉；说明性长句会被丢弃；同一批重复词只留一条。",
    ),
    # G2-5：练习页换 v3（蓝图 E.8/E.9）。空态快照锁三段式；带题文案由
    # test_practice_page.py 逐条锁。
    "/practice": (
        "练一题",
        "今天没有要练的题了",
        "回今天看看",
    ),
    "/books": (
        "<h1>教材库</h1>",
        "<h2>书</h2>",
        "按词搜（跨单元）：输入中文、英文或拼音",
        "搜索",
    ),
    # G2-3：拍卷子页换 v3 文案（蓝图 E.2 逐字），旧页头四件不再出现。
    "/upload": (
        "拍一张卷子",
        "页面铺平，光线均匀，四角都拍进来",
        "拍照",
        "从相册选",
    ),
    "/tools": (
        "<h1>工具开关</h1>",
        "八样能力",
        "当前配方",
        "怎么关",
    ),
    "/tutor": (
        "<h1>讲解（先想后看）</h1>",
        "从练习页的错题进入讲解；或带上 <code>?question_id=编号</code> 打开本页。",
        'href="/practice"',
    ),
    # G2-4：待确认屏换 v3（一屏一题，蓝图 E.6）。本快照锁空态三段式
    # （带题时的逐字文案由 test_review_queue_page.py 逐条锁，比这里更全）。
    "/review-queue": (
        "现在没有要确认的题",
        "机器读完题，拿不准的会排到这里，等你看一眼。",
        "去练一题",
    ),
    # G2-7：最近怎么样换 v3（一句话+简表）。本快照锁空态三段式。
    "/stats": (
        "最近怎么样",
        "还没有练习记录",
        "孩子练过的题会在这里汇成一句话。",
        "先去完成今天的练习",
    ),
    # G2-7：设置页（新增页，直接 v3）
    "/settings": (
        "设置",
        "孩子年级（选填）",
        "每天练几题",
        "数据导出 / 备份",
    ),
    "/exam": (
        "<h1>考试</h1>",
        "保存考试",
        "考试列表",
        "还没有考试。填一个日期，系统才知道什么时候该冲刺。",
    ),
    "/import": (
        "<h1>导入备份</h1>",
        "只用于「新机器恢复」或「本地库被删后恢复」：<strong>目标库必须是空库</strong>。",
        "导入是一次事务：中途任何一步失败，整批回滚，库里不会留下导了一半的数据。",
        ">导入</button>",
    ),
    "/devices": (
        "<h1>拍摄</h1>",
        "高拍仪在 macOS 上就是 UVC 摄像头，这里只读它的能力、不写曝光/对焦。抓一张会存到库目录下的 ",
        # 旧断言 '<a href="/scans">已拍</a>' 已随导航重构下线，改由下面的
        # test_shell_pages_get_the_five_item_nav 逐项断言（五项、href、aria-current），
        # 比原来"页面上存在一条指向 /scans 的链接"更强。
    ),
    "/scans": (
        "<h1>已拍的卷子</h1>",
        "按拍摄时间从新到旧排；每行点进去是那张照片的详情。",
        "还没有拍过一张纸。",
        "拍第一张",
    ),
}


@pytest.mark.parametrize("path", sorted(COPY))
def test_page_keeps_its_visible_copy(client, path):
    """AC4：每页 3–5 条关键中文逐字出现——换皮不许改一个字。"""
    c, _ = client
    html = c.get(path, follow_redirects=True).text

    missing = [text for text in COPY[path] if text not in html]
    assert missing == [], f"{path} 少了这些文案：{missing}"


def _seed_needs_variant(db_path: Path) -> int:
    """一条 needs_variant 错题：进「缺同类的题卡住」那一段，不进待确认（已复习过）。

    `last_reviewed_at` 是必须的：needs_variant 的本义是「已经复习过几轮」，
    待确认队列只收没复习过的题，不写这一笔同一道题会在两段里各出现一次。
    """
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            question = Question(
                learner_id=learner.id,
                subject="数学",
                grade_level="primary-3",
                question_type="计算",
                content="7. 计算 7 + 7 =",
                answer="14",
            )
            session.add(question)
            session.commit()
            item = ErrorItem(
                learner_id=learner.id,
                question_id=question.id,
                review_status=REVIEW_STATUS_NEEDS_VARIANT,
                last_reviewed_at=utcnow(),
            )
            session.add(item)
            session.commit()
            return item.id
    finally:
        engine.dispose()


def _add_two_pending(db_path: Path) -> None:
    """两条低置信度、没复习过的待确认错题（进「看看读得对不对」的队列）。"""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            for index in range(2):
                question = Question(
                    learner_id=learner.id,
                    subject="数学",
                    grade_level="primary-3",
                    question_type="计算",
                    content=f"待确认题-{index + 1}",
                    answer="1",
                )
                session.add(question)
                session.commit()
                session.add(
                    ErrorItem(
                        learner_id=learner.id,
                        question_id=question.id,
                        confidence=0.5,
                    )
                )
            session.commit()
    finally:
        engine.dispose()


def test_review_queue_counts_keep_their_two_labels(client):
    """G2-4 升级：待确认屏的进度计数换 v3 形态——标题「第 1/N 道」+「后面还有 N-1 道」。

    旧断言锁的是计数条「待确认 / 缺同类的题」两段；一屏一题后计数进标题与小节，
    数字仍然真实（这里造 1 条 needs_variant + 0 条待确认 → 空态；再造 2 条待确认
    看「第 1/2 道」）。
    """
    c, db = client
    _seed_needs_variant(db)
    html = c.get("/review-queue", follow_redirects=True).text
    assert "现在没有要确认的题" in html, "0 条待确认时应是空态"
    assert "还在等的 1 道" in html, "needs_variant 那批照旧列出来"

    _add_two_pending(db)
    html = c.get("/review-queue", follow_redirects=True).text
    assert "看看读得对不对（第 1/2 道）" in html
    assert "后面还有 1 道" in html


def test_detail_pages_keep_their_visible_copy(client):
    """AC4：详情页那一组同样逐字锁（改样式不动文案）。"""
    c, db = client
    scan_id, question_id = _seed(db)

    cases = {
        f"/scans/{scan_id}": (
            f"<h1>照片 #{scan_id}</h1>",
            "人工确认收好",
            ">确认收进去</button>",
            "拍到/传上来只是第一步；读题与切题以后交流水线（ADR-007），先由家长人工确认收好。",
        ),
        f"/scans/{scan_id}/ocr": (
            "正在读题",
            "通常 10-30 秒",
            "有点慢，点这里重试这道题",
            "这道题先跳过，稍后在『待你确认』里找它",
            "去确认读得对不对",
        ),
        f"/scans/{scan_id}/questions": (
            f"<h1>标错收进去 · 照片 #{scan_id}</h1>",
            "共 2 题",
            "切得不对？我自己框",
            "把勾选的题收进错题本",
            "系统不判对错，也不识别手写答案：勾哪道，哪道就是错的（见 docs/WRONG-QUESTION-ENTRY.md）。",
        ),
        f"/scans/{scan_id}/frame": (
            f"<h1>我自己框题 · 照片 #{scan_id}</h1>",
            "框题",
            "移动画面",
            "把这些题收进错题本",
        ),
        # 这块的「题目 / 答案 / 孩子当时那道题 / 保存答案」随 P4-a 换语法一起没了：
        # 那四条是在锁一页两个表单的老结构。现在的判据在下面三条，比文案更强。
        f"/questions/{question_id}": ("暂无原图",),
    }

    problems = {}
    for path, texts in cases.items():
        html = c.get(path, follow_redirects=True).text
        missing = [text for text in texts if text not in html]
        if missing:
            problems[path] = missing

    assert problems == {}

    # P4-a-fix：题干页从「页面上有这句话」升级成「整页只有一条保存路径」。
    # P4-a 删掉了下面那个独立的「保存答案」表单，编辑表单成了唯一入口。
    html = c.get(f"/questions/{question_id}", follow_redirects=True).text
    assert html.count(f'action="/questions/{question_id}/edit"') == 1, "编辑表单应当只有一个"
    assert f'action="/questions/{question_id}/answer"' not in html, "重复的 answer 表单又回来了"
    assert "改这道题" in html
    assert 'class="facts"' in html


# ------------------------------------------------- 导航与外壳（卡 P1-a）

SHELL_TEMPLATES = ("tools.html", "devices.html", "import.html")
SHELL_PATHS = ("/tools", "/devices", "/import")

# 前四项是空的 <a>；第五项「更多」是 <details>/<summary> 的面板开关（卡 P1-a-fix 改成真面板，
# 不再是 href="#more" 的死链）。侧栏与底栏共用同一份数据：顺序、文字、href 都不许各自漂移。
NAV_ITEMS = (
    ("今天", "/"),
    ("卷子", "/scans"),
    ("拍", "/upload"),
    ("复习", "/practice"),
)

# Shared navigation groups must render identically on desktop and mobile.
MORE_GROUPS = (
    ("学习", (("错题本", "/questions"), ("教材库", "/books"), ("考试", "/exam"), ("统计", "/stats"), ("待复习", "/review-queue"), ("讲解", "/tutor"))),
    ("数据", (("手动输入", "/entry"), ("导入备份", "/import"), ("导出 Anki", "/export"))),
    ("偏好", (("设置", "/settings"),)),
    ("卷子", (("拍摄设备", "/devices"),)),
    ("诊断", (("工具开关", "/tools"),)),
)

# 换外壳不许弄丢任何一个去处：顶层 4 项里的 3 条 + 面板 9 条 = 12 条（"/" 不算，它是首页本身）。
ALL_DESTINATIONS = (
    "/books", "/exam", "/stats", "/entry", "/import", "/export", "/devices",
    "/review-queue", "/tools", "/scans", "/practice", "/upload",
)

_SIDEBAR = re.compile(r'<nav class="sidebar"[^>]*>(.*?)</nav>', re.S)
_TABBAR = re.compile(r'<nav class="tabbar"[^>]*>(.*?)</nav>', re.S)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S)
_HREF = re.compile(r'href="([^"]*)"')
_TAGS = re.compile(r"<[^>]+>")
_MORE = re.compile(r"<details\b[^>]*>(.*?)</details>", re.S)
_SUMMARY = re.compile(r"<summary\b([^>]*)>(.*?)</summary>", re.S)
_GROUP = re.compile(r'<p class="more__title">(.*?)</p>\s*<ul class="more__links">(.*?)</ul>', re.S)


def _nav_block(html: str, nav: re.Pattern[str]) -> str:
    block = nav.search(html)
    assert block is not None, f"这段导航没渲染出来：{nav.pattern[:32]}"
    return block.group(1)


def _nav_entries(html: str, nav: re.Pattern[str]) -> list[tuple[str, str, bool]]:
    """把一段导航顶层的四项拆成 [(href, 纯文字, 带不带 aria-current), ...]。

    面板链接在 `<details>` 里，不算顶层项，先切掉再看。
    """
    head = _nav_block(html, nav).split("<details", 1)[0]

    entries = []
    for attrs, inner in _ANCHOR.findall(head):
        href = _HREF.search(attrs)
        assert href is not None, f"导航项没有 href：{attrs}"
        entries.append((href.group(1), _TAGS.sub("", inner).strip(), 'aria-current="page"' in attrs))
    return entries


def _more_panels(html: str) -> list[str]:
    """侧栏与底栏各一个「更多」面板，返回两段 <details> 的内文（必须同源）。

    只在两段导航里找：页面正文自己也用 `<details>`（`/devices` 就有），不能混进来。
    """
    panels = []
    for nav in (_SIDEBAR, _TABBAR):
        found = _MORE.findall(_nav_block(html, nav))
        assert len(found) == 1, f"{nav.pattern[:32]} 里的「更多」面板数量是 {len(found)}"
        panels.append(found[0])
    return panels


@pytest.mark.parametrize("path", SHELL_PATHS)
def test_shell_pages_get_v3_navigation_and_tools(client, path):
    """Both navigation bars expose the same four destinations and a tools menu."""
    html = client[0].get(path, follow_redirects=True).text

    expected = [("今天", "/"), ("错题本", "/questions"), ("拍卷子", "/upload"), ("设置", "/settings")]
    for selector in (r'<nav class="side-nav"[^>]*>(.*?)</nav>', r'<nav class="tabbar"[^>]*>(.*?)</nav>'):
        entries = _nav_entries(html, re.compile(selector, re.S))
        assert [(label, href) for href, label, _ in entries] == expected
        assert [href for href, _, active in entries if active] == (["/upload"] if path == "/devices" else ["/settings"])
    assert '<details class="workspace-links">' in html
    assert '<summary>更多功能</summary>' in html


@pytest.mark.parametrize("path", SHELL_PATHS)
def test_no_destination_is_lost(client, path):
    """AC3：换外壳前有 12 个去处，换完一个不少——全都在渲染出来的 `<a href>` 里。

    这条是评审抓出来的缺口：五项导航把 8 个旧去处挤掉了，`#more` 又只是死链，
    于是 12 个去处里有 9 个在页面上根本点不到。少一个就红。
    """
    html = client[0].get(path, follow_redirects=True).text

    linked = set(_HREF.findall(html))
    missing = [href for href in ALL_DESTINATIONS if href not in linked]
    assert missing == [], f"{path} 少了这些去处：{missing}"
    assert len(ALL_DESTINATIONS) == 12, f"清单写错了：{len(ALL_DESTINATIONS)} 条"


@pytest.mark.parametrize("path", SHELL_PATHS)
def test_v3_css_loaded_in_head(client, path):
    """The shared V3 stylesheet is loaded exactly once in the document head."""
    html = client[0].get(path, follow_redirects=True).text
    links = list(re.finditer(r'<link rel="stylesheet" href="/static/v3\.css(?:\?[^" ]+)?">', html))
    assert len(links) == 1, f"{path} 里 v3.css 的 link 出现了 {len(links)} 次"
    assert links[0].start() < html.index("<body"), f"{path} 的 v3.css 落在 <body> 里"

    nav_source = (TEMPLATES / "_nav.html").read_text(encoding="utf-8")
    assert "<link" not in nav_source, "_nav.html 里不该再有 <link>"


def test_shell_templates_drop_their_own_skeleton():
    """AC2：三页不再自带 <head> / <nav> / <style> / DOCTYPE，骨架只剩 _base.html 一份。"""
    for name in SHELL_TEMPLATES:
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert source.startswith('{% extends "_base.html" %}'), name
        for tag in ("<!DOCTYPE", "<head", "<nav", "<style"):
            assert tag not in source, f"{name} 还留着自己的骨架：{tag}"


@pytest.mark.parametrize("path", SHELL_PATHS)
def test_shell_pages_come_out_of_the_shell(client, path):
    """AC2 / AC3：渲染结果只有外壳给的骨架——单 main、两条 nav（侧栏 + 底栏）、无内联 style、有 viewport。"""
    html = client[0].get(path, follow_redirects=True).text

    assert html.count('<main class="main main--wide">') == 1, path
    assert html.count('<nav') == 3, path
    assert "<style" not in html, path
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html, path


# ------------------------------------------------------------ AC6 移动优先的尺寸


def _declarations(css: str, selector: str) -> str:
    """取 *selector* 所在规则的声明块（支持 `.a, .b { … }` 这种并列选择器）。"""
    cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", cleaned):
        selectors = [part.strip() for part in match.group(1).split(",")]
        if selector in selectors:
            return match.group(2)
    raise AssertionError(f"base.css 里没有这条规则：{selector}")


def test_mobile_first_sizes_stay_in_base_css():
    """AC6：`.btn/.input/.textarea` 仍是 44px 起、16px 起，宽表仍能横滚。"""
    css = BASE_CSS.read_text(encoding="utf-8")

    for selector in (".btn", ".input", ".textarea"):
        declarations = _declarations(css, selector)
        assert "min-height: var(--tap-min)" in declarations, selector
        assert "font-size: var(--text-base)" in declarations, selector

    scroll = _declarations(css, ".table--scroll")
    assert "overflow-x: auto" in scroll


def test_wide_tables_opt_into_the_scroll_wrapper():
    """宽表必须带 `.table--scroll`：窄屏横向滚动，不把六列挤成一列字。

    `scans.html` 已在 V1 改成行式列表（`.list-row`），不再有表格，故不在名单里。
    G2-4/G2-7 起 scan_ocr.html 与 stats.html 也不再有表格（换 v3 读题屏 / 一句话+简表），
    名单换成还在用宽表的模板。
    """
    for name in ("books.html", "devices.html", "exam.html", "entry.html"):
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'class="table table--scroll"' in html, name


# ------------------------------------------- 卡 W1：详情页按状态出一个主按钮

SHOT_STAGES = {
    "crop": "scans/2026/11/stage-crop.jpg",
    "select": "scans/2026/11/stage-select.jpg",
    "ready": "scans/2026/11/stage-ready.jpg",
}

STAGE_BUTTONS = {
    "crop": ("确认裁边", "/scans/{id}/crop"),
    "select": ("选出要收的题", "/scans/{id}/select"),
    "ready": ("开始复习", "/practice"),
}


def _seed_three_stages(db_path: Path) -> dict[str, int]:
    """三张卷子各停在一个状态：没裁过 / 裁好了没收进去 / 收进去 3 道。

    直接写库与 sidecar，不跑裁边——这一组只验"页面读状态"，不是验检测器。
    """
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            ids: dict[str, int] = {}
            for index, (stage, rel) in enumerate(SHOT_STAGES.items()):
                target = db_path.parent / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (WIDTH, HEIGHT), "white").save(
                    target, format="JPEG", quality=90
                )
                scan = Scan(
                    learner_id=learner.id,
                    device_id="browser-upload-9f3c2a71",
                    device_name="浏览器上传",
                    width=WIDTH,
                    height=HEIGHT,
                    # 新的在前：列表页三行的顺序才稳定
                    captured_at=utcnow().replace(microsecond=index),
                    path=rel,
                    source="upload",
                    quality_json=QUALITY_JSON,
                )
                session.add(scan)
                session.commit()
                ids[stage] = scan.id
                if stage == "crop":
                    continue  # 老照片：图旁边没有 sidecar
                record = target.with_suffix(".crop.json")
                record.write_text(
                    '{"reason": "ok", "area_ratio": 0.44, "note": "完成"}',
                    encoding="utf-8",
                )
                if stage != "ready":
                    continue
                for no in (1, 2, 3):
                    question = Question(
                        learner_id=learner.id,
                        subject="数学",
                        grade_level="primary-3",
                        question_type="计算",
                        content=f"{no}. 计算 {no} + {no} =",
                        answer=str(no * 2),
                    )
                    session.add(question)
                    session.commit()
                    session.add(
                        ScanQuestion(
                            scan_id=scan.id,
                            no=no,
                            bbox_json='{"bbox": [0, 0, 10, 10], "page": [900, 1200], "angle": 0}',
                            stem_text=f"{no}. 计算 {no} + {no} =",
                            is_wrong=True,
                            question_id=question.id,
                        )
                    )
                session.commit()
            return ids
    finally:
        engine.dispose()


def _foot(html: str) -> str:
    """主按钮下面那行等宽小字（`.select-foot`）。"""
    found = re.search(r'<p class="select-foot">(.*?)</p>', html, re.S)
    assert found is not None, "详情页没有主按钮下面那行依据"
    return found.group(1).strip()


@pytest.mark.parametrize("stage", sorted(SHOT_STAGES))
def test_scan_detail_offers_exactly_one_next_step(client, stage):
    """AC2：三种状态各一个按钮，另外两个文案不出现；依据那行跟着状态走。"""
    c, db = client
    scan_ids = _seed_three_stages(db)
    scan_id = scan_ids[stage]
    label, href = STAGE_BUTTONS[stage]
    href = href.format(id=scan_id)

    html = c.get(f"/scans/{scan_id}").text

    assert f'href="{href}">{label}</a>' in html, f"{stage} 的主按钮不对"
    assert 'class="btn btn--primary"' in html
    for other, _ in STAGE_BUTTONS.values():
        if other != label:
            assert other not in html, f"{stage} 的页面上不该出现「{other}」"

    # 次按钮始终在：原图 / 再拍一张
    assert f'href="/scans/{scan_id}?source=original">原图</a>' in html
    assert 'href="/devices">再拍一张</a>' in html

    hints = {"crop": "还没有裁切记录", "select": "自动裁切完成 · 纸占 44%", "ready": "已收好 3 道"}
    assert _foot(html) == hints[stage], f"{stage} 的依据那行不对：{_foot(html)}"


def test_missing_scan_still_answers_404(client):
    """AC5：卷子不存在时详情页仍是 404（`stage` 不拿 "crop" 兜底）。"""
    c, _ = client

    assert c.get("/scans/999999").status_code == 404


def test_scans_list_shows_the_next_step_without_buttons(client):
    """AC4：列表页三行各显示一种状态；行里没有 button。

    V1 起列表是行式（`<a class="list-row">`），不再是 `<tr>`：整行一个出口，
    状态落在 `.list-row__step`。所以这里按行取值，不再找 `<td class="next">`。
    """
    c, db = client
    _seed_three_stages(db)

    html = c.get("/scans").text
    rows = re.findall(r'<a class="list-row" href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    assert len(rows) == 3, f"应该三行，实际 {len(rows)}"

    steps = []
    for href, row in rows:
        cell = re.search(r'<span class="list-row__step">(.*?)</span>', row, re.S)
        assert cell is not None, f"这一行没有步骤列：{row[:80]}"
        steps.append(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell.group(1))).strip())
        assert "<button" not in row, "列表页不放按钮，整行只是指路"
        assert re.fullmatch(r"/scans/\d+", href), f"整行没有链到详情：{href}"

    assert sorted(steps) == sorted(["待裁边", "待选题", "已收好 3"]), steps

    # V1.5 起计数条换成 .summary 三格（数字 / 标签各自成行），口径不变：共 3 / 待处理 2 / 已收好 1
    assert '<div class="summary">' in html, "页面上没有汇总格卡"
    cells = re.findall(
        r'<span class="summary__value">([^<]*)</span>\s*'
        r'<span class="summary__label">([^<]*)</span>',
        html,
    )
    assert cells == [("3", "共 张"), ("2", "待处理"), ("1", "已收好")], cells


def test_scan_detail_has_no_table(client):
    """W1-fix：卷子详情页是行式结构，整页不许再出现 table / tr / td。

    老那张 `<table class="table">` 的宽度是 100%，但表格的最小内容宽由格子里
    不可断的 `<code>` 决定：压缩过的质量指标 JSON 把它顶到 924，390 的真视口
    被浏览器撑成 492（`innerWidth` 也跟着变）。所以这里造的是**带真实质量指标**
    的卷子——指标为空的话，这条测试对那次的回归是瞎的。
    列表页 `scans.html` 仍用表格，不在这条里。
    """
    c, db = client
    scan_id, _ = _seed(db)

    html = c.get(f"/scans/{scan_id}").text.lower()

    for tag in ("<table", "<tr", "<td"):
        assert tag not in html, f"卷子详情页还有 {tag}"


def test_scan_detail_figure_image_is_capped(client):
    """W1-fix：图在 `.figure` 里，且 `.figure img` 限宽 100%。

    原图是 1000px 起的；`max-width: 100%` 一丢，390 的屏就会被图片撑开。
    两半都查：CSS 规则（真限宽）+ 模板结构（规则能命中这张图）。
    """
    c, db = client
    scan_id, _ = _seed(db)
    html = c.get(f"/scans/{scan_id}").text

    declarations = _declarations(
        (_SRC / "static" / "components.css").read_text(encoding="utf-8"), ".figure img"
    )
    assert "max-width: 100%" in declarations, f".figure img 没有限宽：{declarations.strip()}"

    figure = re.search(r'<div class="figure">(.*?)</div>', html, re.S)
    assert figure is not None, "渲染出来的详情页没有 .figure"
    assert "<img" in figure.group(1), "图片不在 .figure 里，限宽规则管不到它"


# --------------------------------------------- AC6 真浏览器 390 宽实测（三种状态）


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

base, outdir = sys.argv[1], sys.argv[2]
pages = json.loads(sys.argv[3])
out = {}
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
    )
    for name, path in pages.items():
        page.goto(base + path, wait_until="load")
        page.wait_for_selector(".select-actions")
        page.screenshot(path=outdir + "/" + name + ".png", full_page=True)
        size = page.evaluate(
            "() => [document.documentElement.scrollWidth,"
            " document.documentElement.clientWidth,"
            " window.innerWidth]"
        )
        photo = page.locator('.workspace .figure img')
        out[name] = {"size": size, "visible": photo.is_visible(),
                     "loaded": photo.evaluate('el => el.naturalWidth > 0'),
                     "width": photo.bounding_box()['width']}
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
def test_browser_390_has_no_overflow_in_any_stage(tmp_path):
    """AC6：390 宽下三种状态各截一张，`innerWidth` 与 `scrollWidth` 都必须是 390。

    只看 `scrollWidth` 不够：`is_mobile` 下溢出时浏览器会把内外两个宽度一起放大
    （W1 就是这么把 492 报成"无溢出"的），所以三个数逐个比 390。
    """
    db = tmp_path / "stages.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app):
        pass  # 先让库把表建出来
    scan_ids = _seed_three_stages(db)
    outdir = Path("/tmp/cairn-w1-390")
    outdir.mkdir(parents=True, exist_ok=True)

    pages = {stage: f"/scans/{scan_id}" for stage, scan_id in scan_ids.items()}
    script = tmp_path / "browse_stages.py"
    script.write_text(_BROWSER, encoding="utf-8")
    with _live_server(app) as base:
        done = subprocess.run(
            [BROWSER_PY, str(script), base, str(outdir), json.dumps(pages)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    assert done.returncode == 0, f"浏览器脚本失败：\n{done.stdout}\n{done.stderr}"
    seen = json.loads(done.stdout.strip().splitlines()[-1])

    for stage in pages:
        assert seen[stage]["size"] == [390, 390, 390], f"{stage} 在 390 宽下有横向溢出：{seen[stage]}"
        assert seen[stage]["visible"] and seen[stage]["loaded"]
        assert 0 < seen[stage]["width"] <= 390
    print(
        "390×844 截图："
        + " | ".join(f"{stage} {outdir / (stage + '.png')} {seen[stage]['size'][0]}" for stage in pages)
    )


# --------------------------------------- 卡 V7：导航的"可点"长相（1440 / 390）
# 这两条锁的是"看得见的反馈"：侧栏行线 + hover + 当前项墨条，底栏格线 +
# 当前项横条 + 五格同一条中心线。样式全在 components.css，模板不动。
# 落点用 /scans：`/` 是不带外壳的落地页，里面没有这两条 nav。

_NAV_1440_BROWSER = r'''
import json, sys
from playwright.sync_api import sync_playwright
base, shot = sys.argv[1:]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(base + "/scans", wait_until="load")
    nav = page.locator(".side-nav")
    items = nav.locator("a")
    before = items.nth(3).evaluate("e => getComputedStyle(e).backgroundColor")
    items.nth(3).hover()
    page.wait_for_timeout(300)
    after = items.nth(3).evaluate("e => getComputedStyle(e).backgroundColor")
    result = {"count": items.count(), "current": nav.locator('[aria-current=page]').count(), "hover_changed": before != after, "visible": nav.is_visible()}
    nav.screenshot(path=shot)
    browser.close()
print(json.dumps(result))
'''

_NAV_390_BROWSER = r'''
import json, sys
from playwright.sync_api import sync_playwright
base, shot = sys.argv[1:]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page.goto(base + "/scans", wait_until="load")
    cells = page.locator(".tabbar > a")
    measure = "es => es.map(e => { const b=e.getBoundingClientRect(); const c=getComputedStyle(e); const p=[...e.children].map(x => x.getBoundingClientRect()); return {x:b.x,w:b.width,h:b.height,content:(Math.min(...p.map(x=>x.left))+Math.max(...p.map(x=>x.right)))/2,current:e.hasAttribute('aria-current'),color:c.color,weight:c.fontWeight}; })"
    closed = cells.evaluate_all(measure)
    page.locator('.workspace-links summary').click()
    opened = cells.evaluate_all(measure)
    result = {"closed": closed, "opened": opened, "visible":page.locator('.tabbar').is_visible(), "menu":page.locator('.workspace-links a:visible').count()}
    page.locator(".tabbar").screenshot(path=shot)
    browser.close()
print(json.dumps(result))
'''


def _run_nav_browser(tmp_path, name: str, script: str, shot: str) -> dict:
    """起一次真服务，跑给定的浏览器脚本，回收它打印的 JSON。"""
    db = tmp_path / f"{name}.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app):
        pass  # 先让库把表建出来，/ 才不会 500
    path = tmp_path / f"{name}.py"
    path.write_text(script, encoding="utf-8")
    with _live_server(app) as base:
        done = subprocess.run(
            [BROWSER_PY, str(path), base, shot],
            capture_output=True,
            text=True,
            timeout=300,
        )
    assert done.returncode == 0, f"浏览器脚本失败：\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_browser
def test_browser_1440_sidebar_rows_have_rules_and_hover(tmp_path):
    """The desktop navigation shows an active item and hover feedback."""
    shot = "/tmp/v7_nav_1440.png"
    seen = _run_nav_browser(tmp_path, "nav1440", _NAV_1440_BROWSER, shot)

    assert seen["visible"]
    assert seen["count"] == 4
    assert seen["current"] == 1
    assert seen["hover_changed"]


@needs_browser
def test_browser_390_tabbar_cells_share_one_centre_line(tmp_path):
    """The four mobile items stay centered when the tools menu opens."""
    shot = "/tmp/v7_nav_390.png"
    seen = _run_nav_browser(tmp_path, "nav390", _NAV_390_BROWSER, shot)

    assert seen["visible"] and seen["menu"] == 12
    for state in ("closed", "opened"):
        cells = seen[state]
        assert len(cells) == 4
        assert max(c["w"] for c in cells) - min(c["w"] for c in cells) <= 1
        assert all(c["h"] >= 44 for c in cells)
        current = [c for c in cells if c["current"]]
        assert len(current) == 1 and current[0]["weight"] == "600"
        assert current[0]["color"] == "rgb(15, 110, 86)"
        centres = [c["x"] + c["w"] / 2 for c in cells]
        assert all(abs(c["content"] - x) <= 1 for c, x in zip(cells, centres))
        gaps = [b - a for a, b in zip(centres, centres[1:])]
        assert max(gaps) - min(gaps) <= 1




# ------------------------------- 卡 V8：主按钮的底必须是"填色"，不是链接色
# `--color-accent` 在亮色是 #8A5A12（深棕），配墨字整块发暗；tokens.css 里
# `--accent-fill` 一直注明"主按钮填色"却没人引用。这条用真浏览器量 computed 值：
# 亮色 #E9B44C、暗色 #D08C3A，字色两套都是 #1C1A17。

_V8_BROWSER = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, shot = sys.argv[1], sys.argv[2]

READ = """() => {
  const el = document.querySelector('.btn--primary');
  if (!el) return null;
  const c = getComputedStyle(el);
  return {bg: c.backgroundColor, color: c.color, opacity: c.opacity};
}"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    out = {}
    for name in ("light", "dark"):
        page = browser.new_page(
            viewport={"width": 1024, "height": 800}, color_scheme=name
        )
        page.goto(base + "/review-queue", wait_until="load")
        out[name] = page.evaluate(READ)
        if name == "light":
            page.locator(".btn--primary").screenshot(path=shot)
        page.close()
    browser.close()
print(json.dumps(out, ensure_ascii=False))
'''


@needs_browser
def test_browser_primary_button_is_v3_green_in_both_themes(tmp_path):
    """G2-4 升级：v3 主按钮实测——底色青绿 #0F6E56（rgb(15,110,86)）、字色白。

    `/review-queue` 空态只有一个主按钮（「去练一题」），querySelector 拿到的就是它。
    旧断言锁的是旧体系琥珀色（#E9B44C/#D08C3A+墨字）；v3 色板定稿后换锁 v3 值，
    亮/暗两个 color_scheme 都量一遍（v3 单主题，两边必须一致）。
    """
    shot = "/tmp/v8_btn.png"
    seen = _run_nav_browser(tmp_path, "v8btn", _V8_BROWSER, shot)

    assert seen["light"] is not None, "亮色没找到 .btn--primary"
    assert seen["dark"] is not None, "暗色没找到 .btn--primary"

    for scheme in ("light", "dark"):
        assert seen[scheme]["bg"] == "rgb(15, 110, 86)", (
            f"{scheme} 主按钮底色不是 #0F6E56：{seen[scheme]['bg']}"
        )
        assert seen[scheme]["color"] == "rgb(255, 255, 255)", (
            f"{scheme} 主按钮字色不是白色：{seen[scheme]['color']}"
        )
    assert Path(shot).exists(), f"截图没生成：{shot}"
    print(f"主按钮截图：{shot} | {json.dumps(seen, ensure_ascii=False)}")
