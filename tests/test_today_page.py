"""卡 G2-1 + G2-2：v3 设计基座 + 全局壳 + 今天页（空态/常态/练完态）。

G2-1 只锁本卡交付面：

- AC1 `GET /` 直接 200 渲「今天」空态（不再 303 → /entry）；
- AC2 真浏览器：390（`is_mobile=False`）`scrollWidth == 390`；1160 视口侧栏
  出现同名四入口（今天/错题本/拍卷子/设置），手机底部 tab 不再重复；
- AC3 定稿文案逐字：Cairn 凯恩 / 您的学习小伙伴 / 空态卡两句 / 流程小字；
- AC4 v3 三件（v3.css、_v3.html、today.html）不引用 tokens.css / components.css；
- AC5 页面无「AI」字样；全页主按钮恰好一个（桌面主磁贴另计）。

G2-2 锁今天页两状态 + 桌面磁贴（数字全部后端真实计数、多处同源）：

- 常态「今天有 N 题要练」N == practice_today().remaining（与取题池同源）；
- 练完态用剧本 B4「今天的练习做完了 ✅」+ A9 计数句；
- 待处理 M == stage != ready 的卷子数；M=0 卡与磁贴都不出现，M≥1 桌面三枚磁贴
  且实心仅一枚；
- 近况一句话与 glance_service 同数（「最近怎么样」页将同源）；
- 侧栏徽标（今天/错题本）与今日卡数字同源（断言相等）；
- 位置：近况在手机次按钮上方、桌面磁贴下方；390（is_mobile=False）无横滚。

浏览器用例依赖本机 `/usr/bin/python3` 里装了 playwright；没有就 skip，接口侧仍有
全量断言。尺寸口径：**`is_mobile=False`**（移动仿真开着会把视口一起放大，假绿过一次）。
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn import glance_service, scan_service, stats_service
from cairn import web as web_module
from cairn.models import ErrorItem, Learner, Question, Review, Scan
from cairn.models.learner import utcnow
from cairn.web import create_app

_SRC = Path(web_module.__file__).parent
V3_FILES = (
    _SRC / "static" / "v3.css",
    _SRC / "templates" / "_v3.html",
    _SRC / "templates" / "today.html",
)

COPY = (
    "Cairn 凯恩",
    "您的学习小伙伴",
    "今天还没有要复习的题",
    "拍一张孩子的卷子，圈出做错的题，剩下的交给我们。",
    "拍一张卷子",
    "拍进来 → 圈出错的 → 明天开始练",
)

NAV_LABELS = ("今天", "错题本", "拍卷子", "设置")
NAV_HREFS = ("/", "/questions", "/upload", "/settings")


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'today.db'}")
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------- AC1 / AC3 / AC5


def test_root_answers_200_without_redirect(client):
    """`/` 直接 200 渲今天页：不带 Location、不是跳转。"""
    c = client
    resp = c.get("/", follow_redirects=False)
    assert resp.status_code == 200
    assert "location" not in {k.lower() for k in resp.headers}
    assert c.get("/", follow_redirects=True).status_code == 200


def test_root_renders_the_finalized_copy(client):
    """定稿文案逐字在场；空态小字整句在；页面无「AI」。"""
    c = client
    html = c.get("/").text
    missing = [text for text in COPY if text not in html]
    assert missing == [], f"定稿文案缺了：{missing}"
    assert "AI" not in html, "界面禁「AI」字样（说机器读题/自动读题）"


def test_root_has_exactly_one_primary_button(client):
    """红线：每页主按钮恰好一个。桌面主磁贴是 `tile--primary`，单独计一枚。"""
    c = client
    html = c.get("/").text
    assert html.count('class="btn btn--primary"') == 1
    assert html.count('class="tile tile--primary"') == 1


def test_root_shows_real_date_line(client):
    """头部/内容列日期是真日期：逐字等于 UTC+8 的今天（N月N日 周X），不是占位符。"""
    from datetime import datetime, timedelta, timezone

    c = client
    html = c.get("/").text
    now = datetime.now(timezone(timedelta(hours=8)))
    expected = f"{now.month}月{now.day}日 周{'一二三四五六日'[now.weekday()]}"
    assert expected in html, f"日期行不是今天：想找 {expected!r}"


# --------------------------------------------------------------- 壳：四入口


def test_shell_nav_has_the_four_entries(client):
    """手机底 tab 与桌面侧栏同名同序同去向（今天/错题本/拍卷子/设置）。"""
    c = client
    html = c.get("/").text

    for nav_class, item_class in (("tabbar", "tabbar__item"), ("side-nav", "side-nav__item")):
        block = re.search(
            rf'<nav class="{nav_class}"[^>]*>(.*?)</nav>', html, re.S
        )
        assert block is not None, f"没有 {nav_class}"
        items = re.findall(
            rf'<a class="{item_class}[^"]*" href="([^"]*)"[^>]*>(.*?)</a>',
            block.group(1),
            re.S,
        )
        labels = [re.sub(r"<[^>]+>", "", inner).strip() for _, inner in items]
        hrefs = [href for href, _ in items]
        assert labels == list(NAV_LABELS), f"{nav_class} 文案/顺序不对：{labels}"
        assert hrefs == list(NAV_HREFS), f"{nav_class} 去向不对：{hrefs}"

    # 当前页是「今天」；空态下「错题本」置灰但可见（蓝图 §3.3）
    assert 'class="tabbar__item is-on" href="/"' in html
    assert 'class="tabbar__item is-off" href="/questions"' in html
    assert 'class="side-nav__item is-on" href="/"' in html
    assert 'class="side-nav__item is-off" href="/questions"' in html


# --------------------------------------------------------------- AC4


def test_v3_files_never_reference_the_old_stylesheets():
    """v3 页面禁 import 旧两份 css（旧 tokens/components 只服务批外旧页）。"""
    for path in V3_FILES:
        source = path.read_text(encoding="utf-8")
        for banned in ("tokens.css", "components.css"):
            assert banned not in source, f"{path.name} 引了 {banned}"


# --------------------------------------------------------------- AC2 真浏览器


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

# **is_mobile=False**：移动仿真开着时浏览器会把视口一起放大，溢出也会报 390（假绿）。
_BROWSER_390 = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, shot = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=False)
    page.goto(base + "/", wait_until="load")
    page.screenshot(path=shot, full_page=True)
    out = page.evaluate(
        """() => [document.documentElement.scrollWidth,
                  document.documentElement.clientWidth,
                  window.innerWidth]"""
    )
    browser.close()
print(json.dumps(out))
'''

_BROWSER_1160 = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, shot = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1160, "height": 800})
    page.goto(base + "/", wait_until="load")
    page.screenshot(path=shot, full_page=True)
    out = page.evaluate(
        """() => {
            const side = document.querySelector('.shell__side');
            const tab = document.querySelector('.tabbar');
            const items = [...document.querySelectorAll('.side-nav__item')]
                .map((a) => a.textContent.trim());
            const box = side ? side.getBoundingClientRect() : null;
            return {
                sideVisible: !!side && getComputedStyle(side).display !== 'none',
                tabHidden: !tab || getComputedStyle(tab).display === 'none',
                sideWidth: box ? Math.round(box.width) : 0,
                items,
            };
        }"""
    )
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
def test_browser_390_today_has_no_overflow(tmp_path):
    """390 真视口：`scrollWidth == clientWidth == innerWidth == 390`（手机不横滚）。"""
    app = create_app(f"sqlite:///{tmp_path / 'browser390.db'}")
    with TestClient(app):
        pass  # 先把库建出来
    shot = "/tmp/cairn-g2-1-390.png"
    script = tmp_path / "browse390.py"
    script.write_text(_BROWSER_390, encoding="utf-8")
    with _live_server(app) as base:
        done = subprocess.run(
            [BROWSER_PY, str(script), base, shot], capture_output=True, text=True, timeout=300
        )
    assert done.returncode == 0, f"浏览器脚本失败：\n{done.stdout}\n{done.stderr}"
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen == [390, 390, 390], f"390 宽下有横向溢出：{seen}"
    print(f"390×844 截图：{shot} scrollWidth={seen[0]}")


@needs_browser
def test_browser_1160_sidebar_shows_four_entries(tmp_path):
    """1160 视口：侧栏同名四入口在场（250px 宽），手机底部 tab 不再重复。"""
    app = create_app(f"sqlite:///{tmp_path / 'browser1160.db'}")
    with TestClient(app):
        pass
    shot = "/tmp/cairn-g2-1-1160.png"
    script = tmp_path / "browse1160.py"
    script.write_text(_BROWSER_1160, encoding="utf-8")
    with _live_server(app) as base:
        done = subprocess.run(
            [BROWSER_PY, str(script), base, shot], capture_output=True, text=True, timeout=300
        )
    assert done.returncode == 0, f"浏览器脚本失败：\n{done.stdout}\n{done.stderr}"
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen["sideVisible"] is True, "1160 下侧栏没显示"
    assert seen["tabHidden"] is True, "1160 下手机底部 tab 还在（应由侧栏承担）"
    assert seen["sideWidth"] == 250, f"侧栏宽度不对：{seen['sideWidth']}"
    assert seen["items"] == list(NAV_LABELS), f"侧栏四入口不对：{seen['items']}"
    print(f"1160×800 截图：{shot}")


# ====================================================================== G2-2
# 今天页两状态 + 桌面磁贴：真实计数、待处理卡、近况、徽标。


def _learner_id(session) -> int:
    return session.scalars(select(Learner).order_by(Learner.id.asc())).first().id


def _add_items(app, *, due=0, done_today=0, due_tomorrow=0, wrong_count=1):
    """播错题：due=今天到期（要练）/ done_today=今天已练完 / due_tomorrow=明天到期。

    走 app 的 session_factory（和路由同一个库）。返回到期题 id 列表。
    """
    now = utcnow()
    with app.state.session_factory() as s:
        learner = _learner_id(s)
        due_ids = []
        for i in range(due + done_today + due_tomorrow):
            q = Question(
                learner_id=learner,
                subject="数学",
                grade_level="primary-3",
                question_type="计算",
                content=f"{i} + 1 = ?",
                answer=str(i + 1),
                analysis="",
            )
            s.add(q)
            s.flush()
            if i < due:
                item = ErrorItem(
                    learner_id=learner,
                    question_id=q.id,
                    review_status="reviewing",
                    next_due_at=now - timedelta(hours=1 + i),
                    last_reviewed_at=now - timedelta(days=1),
                    wrong_count=wrong_count,
                )
                s.add(item)
                s.flush()
                due_ids.append(item.id)
            elif i < due + done_today:
                item = ErrorItem(
                    learner_id=learner,
                    question_id=q.id,
                    review_status="reviewing",
                    next_due_at=now + timedelta(days=3),
                    last_reviewed_at=now,
                    wrong_count=wrong_count,
                )
                s.add(item)
                s.flush()
                s.add(Review(error_item_id=item.id, rating="good", reviewed_at=now))
            else:
                s.add(
                    ErrorItem(
                        learner_id=learner,
                        question_id=q.id,
                        review_status="reviewing",
                        next_due_at=now + timedelta(days=1),
                        last_reviewed_at=now - timedelta(days=2),
                        wrong_count=wrong_count,
                    )
                )
        s.commit()
        return due_ids


def _add_pending_scan(app) -> int:
    """一张还没圈的卷子：无旁挂裁切记录 → stage=crop（断点续走进裁边页）。"""
    with app.state.session_factory() as s:
        scan = Scan(
            learner_id=_learner_id(s),
            device_id="test-device",
            device_name="测试相机",
            width=3024,
            height=4032,
            captured_at=utcnow(),
            path="scans/2026/11/pending.jpg",
            source="upload",
        )
        s.add(scan)
        s.commit()
        return scan.id


def _today_stats(app):
    """路由同款口径再算一遍（测试里断言「渲染的 N == 后端 due 计数」用）。"""
    with app.state.session_factory() as s:
        learner = _learner_id(s)
        tz = timezone(timedelta(hours=8))
        stat = stats_service.practice_today(s, learner, tz=tz)
        glance = glance_service.build_glance(s, learner, tz=tz)
        scans = scan_service.list_scans(s, learner)
        pending = [
            x
            for x in scans
            if scan_service.stage_for(s, x, app.state.db_url) != scan_service.STAGE_READY
        ]
        return stat, glance, len(pending)


def test_todo_state_due_count_matches_backend(client):
    """常态：「今天有 N 题要练」的 N == 后端 due 计数（测试内断言相等）。"""
    c = client
    _add_items(c.app, due=3)
    html = c.get("/").text
    shown = re.search(r"今天有 (\d+) 题要练", html)
    assert shown, "常态大字「今天有 N 题要练」不在"
    stat, _, _ = _today_stats(c.app)
    assert int(shown.group(1)) == stat.remaining == 3
    assert "开始今天的练习" in html
    assert "今天还没有要复习的题" not in html
    assert "今天的练习做完了" not in html


def test_empty_state_absent_once_there_is_work(client):
    """有题要练时空态整段不出现（三态互斥）。"""
    c = client
    _add_items(c.app, due=1)
    html = c.get("/").text
    assert "今天还没有要复习的题" not in html
    assert "拍一张孩子的卷子，圈出做错的题，剩下的交给我们。" not in html


def test_done_state_uses_script_b4_copy(client):
    """练完态：剧本 B4「今天的练习做完了 ✅」+ A9 计数句（明天见是真实数）。"""
    c = client
    _add_items(c.app, done_today=2, due_tomorrow=1)
    html = c.get("/").text
    assert "今天的练习做完了 ✅" in html
    assert "今天已完成 2 题练习" in html
    assert "还有 1 道题明天见" in html
    assert "开始今天的练习" not in html
    assert "今天还没有要复习的题" not in html


def test_mid_state_says_half_done_with_real_numbers(client):
    """G2-5 移交（B.6）：练到一半主按钮「练到一半（x/N），继续」，数字真实。

    x = 今天已练、N = 已练 + 还剩（与取题池同源）；一条没练时不出现这句。
    （V3.1：主按钮去播放三角与尾部箭头，文案与数字断言不变。）
    """
    c = client
    _add_items(c.app, due=2, done_today=1)
    html = c.get("/").text
    assert "练到一半（1/3），继续" in html
    assert "btn__play" not in html, "V3.1 起主按钮不再带播放三角"
    assert "开始今天的练习" not in html, "已开练后不该再喊开始"
    stat, _, _ = _today_stats(c.app)
    assert (stat.completed, stat.remaining) == (1, 2)


def test_pending_card_hidden_without_scans(client):
    """AC3 M=0：手机不显示待处理卡、桌面不显示待处理磁贴。"""
    c = client
    _add_items(c.app, due=1)
    html = c.get("/").text
    assert "还没圈" not in html
    assert "待处理的卷子" not in html


def test_pending_scan_card_tiles_and_link(client):
    """AC3 M≥1：桌面三枚磁贴且实心仅一枚；卡片/磁贴的 M == 后端 stage 计数。"""
    c = client
    _add_items(c.app, due=2)
    scan_id = _add_pending_scan(c.app)
    html = c.get("/").text
    _, _, pending = _today_stats(c.app)
    assert pending == 1
    assert "还没圈（1 张）" in html
    assert f'href="/scans/{scan_id}/crop"' in html, "断点续走应进裁边页"
    assert "待处理的卷子" in html
    assert html.count('class="tile tile--primary"') == 1, "实心磁贴必须恰好一枚"
    assert html.count('class="tile tile--secondary"') == 2, "其余描边"


def test_glance_numbers_match_glance_service(client):
    """近况一句话与 glance_service 同数（「最近怎么样」页将同源）。"""
    c = client
    ids = _add_items(c.app, due=1, wrong_count=2)
    with c.app.state.session_factory() as s:
        s.add(Review(error_item_id=ids[0], rating="again", reviewed_at=utcnow()))
        s.commit()
    html = c.get("/").text
    _, glance, _ = _today_stats(c.app)
    assert glance.practiced_this_week == 1 and glance.repeated_wrong == 1
    assert f"这周练了 {glance.practiced_this_week} 题" in html
    assert f"{glance.repeated_wrong} 题还在反复错" in html


def test_glance_line_hides_when_both_zero(client):
    """两个数都为 0 时近况整行不出现（0 不上屏）。"""
    c = client
    _add_items(c.app, due=1)
    html = c.get("/").text
    assert "这周练了" not in html
    assert "还在反复错" not in html
    assert 'glance-card' not in html


def test_sidebar_badges_match_today_card(client):
    """AC5 侧栏徽标与今日卡数字同源：今天徽标 == 大字 N；错题本徽标 == 题库总数。"""
    c = client
    _add_items(c.app, due=2, done_today=1)
    html = c.get("/").text
    headline = re.search(r"今天有 (\d+) 题要练", html).group(1)
    badges = re.findall(r'side-nav__badge[^"]*">(\d+)</span>', html)
    assert badges[0] == headline, f"今天徽标 {badges[0]} != 今日卡 {headline}"
    stat, _, _ = _today_stats(c.app)
    assert int(headline) == stat.remaining == 2
    assert int(badges[1]) == stat.total_items == 3
    # 文案红线照旧
    assert "AI" not in html


_BROWSER_390_DATA = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, shot = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=False)
    page.goto(base + "/", wait_until="load")
    page.screenshot(path=shot, full_page=True)
    out = page.evaluate(
        """() => {
            const rect = (sel) => {
                const el = document.querySelector(sel);
                // getClientRects 为空 = 自己或祖先 display:none（只看自身 display 会被
                // .only-desktop 父级骗过去）
                return el && el.getClientRects().length ? el.getBoundingClientRect() : null;
            };
            const glance = rect('.glance-card');
            const secondary = rect('.btn--secondary');
            const pending = rect('.pending-card');
            const tiles = rect('.tile-row');
            return {
                widths: [document.documentElement.scrollWidth,
                         document.documentElement.clientWidth,
                         window.innerWidth],
                glanceY: glance ? Math.round(glance.top) : null,
                secondaryY: secondary ? Math.round(secondary.top) : null,
                pendingY: pending ? Math.round(pending.top) : null,
                tilesVisible: !!tiles,
            };
        }"""
    )
    browser.close()
print(json.dumps(out))
'''

_BROWSER_1160_DATA = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, shot = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1160, "height": 800})
    page.goto(base + "/", wait_until="load")
    page.screenshot(path=shot, full_page=True)
    out = page.evaluate(
        """() => {
            const row = document.querySelector('.tile-row');
            const glance = document.querySelector('.glance-card');
            const rowOn = !!row && row.getClientRects().length > 0;
            const glanceOn = !!glance && glance.getClientRects().length > 0;
            const r = rowOn ? row.getBoundingClientRect() : null;
            const g = glanceOn ? glance.getBoundingClientRect() : null;
            return {
                scrollWidth: document.documentElement.scrollWidth,
                tileCount: rowOn ? row.querySelectorAll('.tile').length : 0,
                solidCount: rowOn ? row.querySelectorAll('.tile--primary').length : 0,
                rowBottom: r ? Math.round(r.bottom) : null,
                glanceTop: g ? Math.round(g.top) : null,
            };
        }"""
    )
    browser.close()
print(json.dumps(out, ensure_ascii=False))
'''


@needs_browser
def test_browser_positions_and_overflow_with_data(tmp_path):
    """AC4（带数据实测）：

    - 390（is_mobile=False）`scrollWidth == 390` 且块序 = 待处理卡 → 近况 → 次按钮
      （近况在手机次按钮上方）；
    - 1160 磁贴行三枚、实心恰好一枚，近况在磁贴行下方，页面不横滚。
    """
    app = create_app(f"sqlite:///{tmp_path / 'browser-data.db'}")
    with TestClient(app):
        pass
    due_ids = _add_items(app, due=2, wrong_count=2)
    _add_pending_scan(app)
    with app.state.session_factory() as s:
        s.add(Review(error_item_id=due_ids[0], rating="again", reviewed_at=utcnow()))
        s.commit()

    script390 = tmp_path / "browse390data.py"
    script390.write_text(_BROWSER_390_DATA, encoding="utf-8")
    script1160 = tmp_path / "browse1160data.py"
    script1160.write_text(_BROWSER_1160_DATA, encoding="utf-8")
    with _live_server(app) as base:
        run390 = subprocess.run(
            [BROWSER_PY, str(script390), base, "/tmp/cairn-g2-2-390.png"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        run1160 = subprocess.run(
            [BROWSER_PY, str(script1160), base, "/tmp/cairn-g2-2-1160.png"],
            capture_output=True,
            text=True,
            timeout=300,
        )
    assert run390.returncode == 0, f"390 脚本失败：\n{run390.stdout}\n{run390.stderr}"
    assert run1160.returncode == 0, f"1160 脚本失败：\n{run1160.stdout}\n{run1160.stderr}"

    seen390 = json.loads(run390.stdout.strip().splitlines()[-1])
    assert seen390["widths"] == [390, 390, 390], f"390 带数据横滚：{seen390['widths']}"
    assert seen390["tilesVisible"] is False, "390 下不该出现磁贴行"
    assert seen390["pendingY"] < seen390["glanceY"] < seen390["secondaryY"], (
        f"手机块序不对（应为 待处理卡 → 近况 → 次按钮）：{seen390}"
    )

    seen1160 = json.loads(run1160.stdout.strip().splitlines()[-1])
    assert seen1160["scrollWidth"] == 1160, f"1160 横滚：{seen1160['scrollWidth']}"
    assert seen1160["tileCount"] == 3, f"磁贴不是三枚：{seen1160['tileCount']}"
    assert seen1160["solidCount"] == 1, f"实心磁贴不是恰好一枚：{seen1160['solidCount']}"
    assert seen1160["rowBottom"] <= seen1160["glanceTop"], (
        f"近况没在磁贴行下方：{seen1160}"
    )
    print("390 截图：/tmp/cairn-g2-2-390.png  1160 截图：/tmp/cairn-g2-2-1160.png")
