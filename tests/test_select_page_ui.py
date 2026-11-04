"""前端 Phase 3-a：圈选入库页（`GET /scans/{id}/select`）——只看页面这一层。

这张卡钉六件事：

1. **AC1/AC2**：`GET /scans/{id}/select` 对三张真图各 200，页面上画出来的自动框数
   恰好等于该卷子 `scan_question` 的行数（一框一行，不多不少）。
2. **AC3**：渲染结果零内联样式——没有 `<style>`、没有 `style="`、没有内联
   `<script>`；唯一那个 `<script>` 是外链 `/static/select-questions.js`。
3. **AC4**：真浏览器（系统 python3 的 playwright）走一遍——点 2 个自动框、在空白
   处画 1 个人工框、点「确认入库」，库里 `question` 恰好 +3，人工框的 bbox 落在
   按 rect 换算出的一页坐标上（坐标不换算就会偏，见 AC7 变异 ②）。
4. **AC5**：一张都没切出来的卷子 → 200 且显示「这张卷子没切出题，请自己圈」。
5. **AC6**：390 宽下 `scrollWidth == clientWidth == 390`（手机不横滚）。
6. 版式守卫：五块顺序（h1 → .counts → .select-stage → .select-actions → .select-foot）、
   计数条是两线夹一行、框虚线/选中实线、× 是 44px 热区 + 12px 视觉。

Phase 3-b（本文件下半段）：提交时把这一页的四个动作写进 `cut_feedback`——

- **AC2**：18 个自动框勾 2 / 不要 16 / 自己画 1 → 表里 19 行，`accept=2 delete=16 add=1`
  逐项断言（18 是自动框数，人工补的那条不在其中）；同一次提交里图指纹、宽高、
  配方、参数指纹逐行一致。
- **AC3**：`/frame` 打 500 → 反馈表零新增（`/questions` 那 2 道题是成功的，用来证明
  失败的是第二条）。
- **AC4**：只有 18 个自动框时 `/api/cut-feedback/stats` 返回 `insufficient: true`。
- **AC6**：入库被打回 400 → 红条 + 「已选 2」还在 + 反馈不写。
- 另有一条不起浏览器的用例钉住"页面必须挂出三样 JS 算不出来的东西"（指纹/配方/参数）。

真图只用仓库里的 `docs/samples/*.jpg`；种子卷子用 1000×1400 的合成白图（不跑 OCR）。
浏览器用例依赖本机 `/usr/bin/python3` 里装了 playwright；没有就 skip，接口侧仍有
用例兜底（AC4 的库断言在浏览器用例里，接口侧由 `test_scan_questions.py` 与
`test_scan_frame.py` 各自把关）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn import scan_service
from cairn.models import (
    CutFeedback,
    Learner,
    Question,
    Scan,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.models.learner import utcnow
from cairn.ocr.cut import QuestionBlock
from cairn.web import create_app

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "docs" / "samples"
COMPONENTS_CSS = ROOT / "src" / "cairn" / "static" / "components.css"
V3_CSS = ROOT / "src" / "cairn" / "static" / "v3.css"
SCRIPT = ROOT / "src" / "cairn" / "static" / "select-questions.js"

HI_REDBG = SAMPLES / "hi-redbg.jpg"
USER_PHOTO = SAMPLES / "user-20261101.jpg"
MANUAL_CROP = SAMPLES / "manual-crop.jpg"

WIDTH, HEIGHT = 1000, 1400
EMPTY_LINE = "没认出这页有哪些题，点一下做错的题的位置"


# ---------------------------------------------------------------- 种子：一张卷子


def _plan_image(path: Path) -> None:
    """写一张真的 JPEG（白纸 1000×1400）：/upright 要能发字节、浏览器要能量尺寸。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", (WIDTH, HEIGHT), 255).save(path, format="JPEG")


def _blocks(count: int = 5) -> list[QuestionBlock]:
    """五道题，题框按题号往下排（每块 460×30，够大够点）。"""
    return [
        QuestionBlock(
            no=no,
            bbox=(20, no * 40, 480, no * 40 + 30),
            stem_text=f"{no}. 计算 3 + {no} =",
            line_indexes=[no - 1],
        )
        for no in range(1, count + 1)
    ]


def _seed(db_path: Path, blocks: list[QuestionBlock] | None = None) -> int:
    """落一张照片（+ 可选的切题行）；落好行，GET 就不会去跑真 OCR。"""
    target = db_path.parent / "scans" / "2026" / "11" / "shot.jpg"
    _plan_image(target)
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)  # TestClient 会跑 lifespan；浏览器用例不经过它，这里自己建表
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            if learner is None:  # 浏览器用例没跑 TestClient 的 lifespan，默认孩子自己建
                learner = Learner(name="test-child", grade_level="primary-3")
                session.add(learner)
                session.commit()
            scan = Scan(
                learner_id=learner.id,
                device_id="upload",
                device_name="浏览器上传",
                width=WIDTH,
                height=HEIGHT,
                captured_at=utcnow(),
                path="scans/2026/11/shot.jpg",
                source="upload",
            )
            session.add(scan)
            session.commit()
            if blocks:
                scan_service.store_questions(
                    session,
                    scan.id,
                    blocks,
                    page_width=WIDTH,
                    page_height=HEIGHT,
                )
            return scan.id
    finally:
        engine.dispose()


@pytest.fixture()
def app_client(tmp_path):
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as client:
        yield client, db, app


def _page(client, scan_id: int) -> str:
    response = client.get(f"/scans/{scan_id}/select")
    assert response.status_code == 200, response.text
    return response.text


def _scan_rows(db_path: Path, scan_id: int) -> list[dict]:
    """库里的切题行，摊成普通字典（会话关掉之后照样能断言）。"""
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        with make_session_factory(engine)() as session:
            return [
                {
                    "no": row.no,
                    "question_id": row.question_id,
                    "crop_path": row.crop_path,
                    "bbox": scan_service.row_bbox(row),
                }
                for row in scan_service.list_questions(session, scan_id)
            ]
    finally:
        engine.dispose()


def _question_count(db_path: Path) -> int:
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        with make_session_factory(engine)() as session:
            return len(session.scalars(select(Question.id)).all())
    finally:
        engine.dispose()


def _feedback_rows(db_path: Path, scan_id: int) -> list[dict]:
    """这一页写进 cut_feedback 的快照，摊成普通字典（会话关掉后照样能断言）。"""
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        with make_session_factory(engine)() as session:
            rows = session.scalars(
                select(CutFeedback)
                .where(CutFeedback.scan_id == scan_id)
                .order_by(CutFeedback.frame_key.asc())
            ).all()
            return [
                {
                    "frame_key": row.frame_key,
                    "no": row.question_no,
                    "action": row.action,
                    "source": row.source,
                    "auto_bbox": row.auto_bbox,
                    "human_bbox": row.human_bbox,
                    "iou": row.iou,
                    "selected": row.selected,
                    "image_sha256": row.image_sha256,
                    "image_w": row.image_w,
                    "image_h": row.image_h,
                    "recipe": row.recipe,
                    "params_hash": row.params_hash,
                }
                for row in rows
            ]
    finally:
        engine.dispose()


def _action_counts(rows: list[dict]) -> dict[str, int]:
    counts = {"accept": 0, "delete": 0, "redraw": 0, "add": 0}
    for row in rows:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    return counts


def _seeded_sha256(db_path: Path) -> str:
    """种子卷子的底图指纹（= `_seed` 写进 scans/2026/11/shot.jpg 的那张）。"""
    return hashlib.sha256(
        (db_path.parent / "scans" / "2026" / "11" / "shot.jpg").read_bytes()
    ).hexdigest()


# ---------------------------------------------------------------- CSS 取声明


def declarations(css: str, selector: str) -> dict[str, str]:
    """取 *selector* 那条规则的 {属性: 值}（支持 `.a, .b { … }` 并列写法）。"""
    cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", cleaned):
        if selector in [part.strip() for part in match.group(1).split(",")]:
            return {
                prop.strip(): " ".join(value.split())
                for prop, value in re.findall(r"([-\w]+)\s*:\s*([^;]+)", match.group(2))
            }
    raise AssertionError(f"components.css 里没有这条规则：{selector}")


# ---------------------------------------------------------------- 结构 / 版式


def test_the_page_reads_as_a_printed_form(app_client):
    """G2-3 升级：h1 → .counts → .select-stage → .select-actions → .select-foot，顺序固定。"""
    client, db, _app = app_client
    scan_id = _seed(db)
    html = _page(client, scan_id)

    marks = ['class="page-title"', 'class="counts"', 'class="select-stage"', 'class="select-actions"', 'class="select-foot"']
    positions = [html.find(mark) for mark in marks]
    assert all(pos >= 0 for pos in positions), f"这些块没出现：{dict(zip(marks, positions))}"
    assert positions == sorted(positions), f"块顺序不对：{dict(zip(marks, positions))}"
    assert "点一下做错的题</h1>" in html, "蓝图 E.4 标题逐字"


def test_the_counts_line_is_two_rules_around_one_line(app_client):
    """计数条：一行、两线夹住、段间一个「·」；数字与行数同源。"""
    client, db, _app = app_client
    scan_id = _seed(db, _blocks())
    html = _page(client, scan_id)

    assert f'id="count-auto">{len(_blocks())}<' in html.replace("\n", "")
    assert 'id="count-manual">0<' in html.replace("\n", "")
    assert 'id="count-picked">0<' in html.replace("\n", "")
    assert html.count('class="counts__dot"') == 2

    css = V3_CSS.read_text(encoding="utf-8")
    rule = declarations(css, ".counts")
    assert rule["display"] == "flex"
    assert rule["border-top"] == "1px solid var(--v3-border)"
    assert rule["border-bottom"] == "1px solid var(--v3-border)"
    assert declarations(css, ".counts__value")["font-variant-numeric"] == "tabular-nums"


def test_every_cut_row_gets_one_box_and_no_inline_style(app_client):
    """AC2 的页面侧：一框一行、坐标是原图坐标、零内联样式（AC3）。"""
    client, db, _app = app_client
    blocks = _blocks()
    scan_id = _seed(db, blocks)
    html = _page(client, scan_id)

    boxes = re.findall(r'<div class="select-box"[^>]*>', html)
    assert len(boxes) == len(blocks), f"框数 {len(boxes)} != 行数 {len(blocks)}"
    for no, block in zip(range(1, len(blocks) + 1), blocks):
        assert f'data-no="{no}"' in html
        x0, y0, x1, y1 = block.bbox
        assert (
            f'data-x0="{x0}" data-y0="{y0}" data-x1="{x1}" data-y1="{y1}"' in html
        ), f"第 {no} 题的框坐标没按原图坐标写出来"

    # AC3：零内联样式 / 零内联脚本
    assert "<style" not in html
    assert " style=" not in html
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert scripts == ['<script src="/static/select-questions.js" defer>'], scripts


def test_the_box_is_dashed_until_it_is_picked():
    """G2-3 升级：虚线=没勾，选中换实线加实底——机制不变，锁到活的 v3 样式。"""
    css = V3_CSS.read_text(encoding="utf-8")

    plain = declarations(css, ".select-box")
    assert "dashed" in plain["border"]
    assert plain["position"] == "absolute"

    picked = declarations(css, ".select-box--on")
    assert picked["border-style"] == "solid"
    assert "background" in picked, "选中要有看得见的实底"


def test_the_delete_hotspot_is_44_with_a_12px_glyph():
    """× 是 44×44 的热区 + 12px 的视觉（靶/视觉分离，红点只给删除）。"""
    css = V3_CSS.read_text(encoding="utf-8")

    hot = declarations(css, ".select-box__x")
    assert hot["width"] == "44px"
    assert hot["height"] == "44px"

    glyph = declarations(css, ".select-box__x::after")
    assert glyph["content"] == '"×"'
    assert glyph["font-size"] == "var(--v3-fs-caption)"


def test_the_foot_says_what_counts_and_the_primary_button_carries_the_number(app_client):
    """G2-3 升级：页脚人话（收进去/新题收）；主按钮带数、未选禁用态换「点一下做错的题」。"""
    client, db, _app = app_client
    scan_id = _seed(db)
    html = _page(client, scan_id)

    assert "勾中的就是收进去的；自己圈的当新题收" in html
    assert 'id="submit-count">0<' in html.replace("\n", "")
    assert "就选这" in html and "道" in html
    # 禁用态文案：纯 CSS 两段标签互斥（:disabled 切换），不加脚本
    assert 'class="submit-when-off">点一下做错的题' in html
    assert 'class="submit-when-on"' in html
    # 默认一条都不勾：主按钮先按不下去
    assert 'id="select-submit" disabled' in html


def test_the_secondary_row_is_select_all_and_none(app_client):
    """次按钮行：全选 / 全不选，各占半行（主按钮已整行）。"""
    client, db, _app = app_client
    scan_id = _seed(db)
    html = _page(client, scan_id)

    assert "全选" in html and "全不选" in html
    assert html.find("全选") < html.find("全不选")
    css = V3_CSS.read_text(encoding="utf-8")
    assert declarations(css, ".select-actions__row")["display"] == "flex"


# ---------------------------------------------------------------- AC5 空态 / 404


def test_an_empty_scan_says_draw_it_yourself(app_client, monkeypatch):
    """一张都没切出来：200 + 那句逐字的话（切题关掉时不去跑 OCR）。"""
    client, db, _app = app_client
    monkeypatch.setenv("CAIRN_TOOL_CUT", "0")
    scan_id = _seed(db)

    html = _page(client, scan_id)
    assert EMPTY_LINE in html
    assert 'class="select-box"' not in html


def test_a_missing_scan_is_404_not_500(app_client):
    client, _db, _app = app_client
    response = client.get("/scans/9999/select")
    assert response.status_code == 404


# ---------------------------------------------------------------- AC2 三张真图


@pytest.mark.parametrize("sample", [HI_REDBG, USER_PHOTO, MANUAL_CROP], ids=lambda p: p.stem)
def test_the_three_samples_draw_one_box_per_cut_row(tmp_path, sample: Path):
    """AC2：三张真图各 200，页面上的框数 == 该卷子 scan_question 行数。"""
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as client:
        # This case checks the legacy route's automatic cut and select contract.
        from cairn.models.workflow import Workflow
        with app.state.session_factory() as session:
            session.get(Workflow, "default").active_version_id = "default:v1"
            session.commit()
        uploaded = client.post(
            "/scans/upload",
            files={"file": (sample.name, sample.read_bytes(), "image/jpeg")},
        )
        assert uploaded.status_code == 200, uploaded.text
        scan_id = int(uploaded.url.path.rsplit("/", 1)[-1])

        html = _page(client, scan_id)
        boxes = len(re.findall(r'<div class="select-box"', html))
        rows = len(_scan_rows(db, scan_id))
        print(f"{sample.name}: select 200 | 自动框 {boxes} | scan_question 行 {rows}")
        assert rows > 0, f"{sample.name} 一行都没切出来，AC2 的等价断言没有意义"
        assert boxes == rows, f"{sample.name}: 框 {boxes} != 行 {rows}"
        # 每个框都带得出坐标（没有 bbox 的行会被跳过，那就不会相等了）
        assert html.count("data-x0=") == boxes


# ---------------------------------------------------------------- Phase 3-b：反馈元数据


def test_the_page_carries_the_three_things_javascript_cannot_work_out(app_client):
    """AC2 的地基：底图指纹 / 配方名 / 参数指纹由服务端渲染，且每次一样。

    这三样页面自己算不出来（要读文件、读配方），只能由服务端挂到 `#select-stage`
    的 data-* 上，前端提交成功后原样回传给 `/api/cut-feedback`。指纹必须是**确定
    的**：同一页刷新两次参数指纹要相同，否则同一页的两次提交会被当成两批数据。
    """
    client, db, _app = app_client
    blocks = _blocks(3)
    blocks[1].inferred_number = True  # 第 2 题是跳号推出来的
    scan_id = _seed(db, blocks)

    html = _page(client, scan_id)
    flat = html.replace("\n", " ")

    assert f'data-image-sha256="{_seeded_sha256(db)}"' in flat, "底图指纹不对"
    assert 'data-image-w="1000"' in flat and 'data-image-h="1400"' in flat, (
        "宽高要和 bbox 同一个坐标系"
    )
    assert 'data-recipe="scan-entry"' in flat, "配方名没挂上"

    params = re.search(r'data-params-hash="([0-9a-f]{16})"', flat)
    assert params, "参数指纹没挂上（要 16 位十六进制）"
    assert re.search(r'data-params-hash="%s"' % params.group(1), _page(client, scan_id)), (
        "同一页刷新两次参数指纹应该一样"
    )

    # 每个自动框带一个稳定的 frame_key + 一个 spec 里的 source
    keys = re.findall(r'data-frame-key="([^"]+)"', flat)
    assert keys == ["a1", "a2", "a3"], f"自动框的 frame_key 应该是 a<no>：{keys}"
    sources = re.findall(r'data-strategy="([^"]+)"', flat)
    assert sources == ["anchor_digit", "gap", "anchor_digit"], (
        f"卷面读到题号的是 anchor_digit、跳号推的是 gap：{sources}"
    )
    print(f"元数据：sha {_seeded_sha256(db)[:12]}… | params {params.group(1)} | keys {keys} | {sources}")


def test_stats_says_insufficient_below_twenty_auto_frames(app_client):
    """AC4：只有 18 个自动框时，`/api/cut-feedback/stats` 只给计数，不出率。

    18 是"自动框数"，不含人工补的那一条（`add_rate` 的分母才含它）。
    """
    client, db, _app = app_client
    scan_id = _seed(db, _blocks(18))
    frames = [
        {
            "frame_key": f"a{no}",
            "no": no,
            "source": "anchor_digit",
            "auto_bbox": [20, no * 40, 480, no * 40 + 30],
            "human_bbox": [20, no * 40, 480, no * 40 + 30] if no <= 2 else None,
            "selected": no <= 2,
            "action": "accept" if no <= 2 else "delete",
        }
        for no in range(1, 19)
    ]
    frames.append(
        {
            "frame_key": "m1",
            "no": None,
            "source": "manual",
            "auto_bbox": None,
            "human_bbox": [20, 800, 480, 860],
            "selected": True,
            "action": "add",
        }
    )
    saved = client.post(
        "/api/cut-feedback",
        json={
            "scan_id": scan_id,
            "image_sha256": _seeded_sha256(db),
            "image_w": WIDTH,
            "image_h": HEIGHT,
            "recipe": "scan-entry",
            "params_hash": "params-p3b",
            "frames": frames,
        },
    )
    assert saved.status_code == 201, saved.text
    assert saved.json() == {"saved": 19}

    stats = client.get("/api/cut-feedback/stats").json()
    assert stats["auto_frames"] == 18, stats
    assert stats["added"] == 1, stats
    assert stats["insufficient"] is True, f"18 < 20 应该不出率：{stats}"
    assert "accept_rate" not in stats, stats
    print(f"stats：{json.dumps(stats, ensure_ascii=False, sort_keys=True)}")


# ---------------------------------------------------------------- AC2 / AC3 / AC6 真浏览器


def _browser_python() -> str | None:
    """找一个装了 playwright 的解释器（.venv 里没有，系统 python3 有）。"""
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

base, scan_id, mode = sys.argv[1], sys.argv[2], sys.argv[3]
out = {"mode": mode}
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844})

    def boom(route):
        route.fulfill(status=500, body="boom")

    def reject(route):
        route.fulfill(status=400, body="no")

    # 把一条 POST 打成失败：用来验"入库没成就一个字都不许写反馈"
    if mode == "frame500":
        page.route("**/scans/" + scan_id + "/frame", boom)
    if mode == "reject":
        page.route("**/scans/" + scan_id + "/questions", reject)

    page.goto(base + "/scans/" + scan_id + "/select", wait_until="load")
    page.wait_for_selector(".select-stage")
    out["scroll"] = page.evaluate(
        "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
    )
    out["counts"] = [
        page.inner_text("#count-auto"),
        page.inner_text("#count-manual"),
        page.inner_text("#count-picked"),
    ]
    if mode == "pick":
        page.click('.select-box[data-no="2"]')
        page.click('.select-box[data-no="4"]')
        out["after_pick"] = page.inner_text("#count-picked")

        img = page.locator("#select-image").bounding_box()
        natural = page.evaluate(
            "() => [document.getElementById('select-image').naturalWidth,"
            " document.getElementById('select-image').naturalHeight]"
        )
        x1 = img["x"] + img["width"] * 0.20
        y1 = img["y"] + img["height"] * 0.60
        x2 = img["x"] + img["width"] * 0.50
        y2 = img["y"] + img["height"] * 0.75
        page.mouse.move(x1, y1)
        page.mouse.down()
        page.mouse.move(x2, y2, steps=12)
        page.mouse.up()

        out["after_draw"] = [
            page.inner_text("#count-auto"),
            page.inner_text("#count-manual"),
            page.inner_text("#count-picked"),
        ]
        # 按 rect 比例换算出"这一框在页面上应该是哪四块坐标"
        out["expect"] = [
            round((x1 - img["x"]) * natural[0] / img["width"]),
            round((y1 - img["y"]) * natural[1] / img["height"]),
            round((x2 - img["x"]) * natural[0] / img["width"]),
            round((y2 - img["y"]) * natural[1] / img["height"]),
        ]
        page.click("#select-submit")
        page.wait_for_url("**/scans/" + scan_id, timeout=120000)
        out["landed"] = page.url
    if mode in ("feedback", "frame500", "reject"):
        # 18 个自动框里只勾 2 个（剩下 16 个不勾 = 机器放错、人不要），再自己画 1 个
        page.click('.select-box[data-no="1"]')
        page.click('.select-box[data-no="3"]')
        out["after_pick"] = page.inner_text("#count-picked")
        if mode != "reject":
            img = page.locator("#select-image").bounding_box()
            page.mouse.move(img["x"] + img["width"] * 0.20, img["y"] + img["height"] * 0.60)
            page.mouse.down()
            page.mouse.move(
                img["x"] + img["width"] * 0.50,
                img["y"] + img["height"] * 0.75,
                steps=12,
            )
            page.mouse.up()
        out["after_draw"] = [
            page.inner_text("#count-auto"),
            page.inner_text("#count-manual"),
            page.inner_text("#count-picked"),
        ]
        page.click("#select-submit")
        if mode == "feedback":
            page.wait_for_url("**/scans/" + scan_id, timeout=120000)
            out["landed"] = page.url
        else:
            # 等的是"红条出现"那一刻：提交过程中先显示的是蓝条「正在入库…」
            page.wait_for_function(
                "() => { var el = document.getElementById('select-status');"
                " return !!el && !el.hidden && el.textContent.trim().length > 0"
                " && el.className.indexOf('notice--danger') >= 0; }",
                timeout=60000,
            )
            out["status"] = page.inner_text("#select-status")
            out["status_class"] = page.get_attribute("#select-status", "class")
            out["url"] = page.url
            out["picked"] = page.inner_text("#count-picked")
            out["auto"] = page.inner_text("#count-auto")
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


def _drive(base: str, scan_id: int, mode: str, tmp_path: Path) -> dict:
    script = tmp_path / "browse.py"
    script.write_text(_BROWSER, encoding="utf-8")
    done = subprocess.run(
        [BROWSER_PY, str(script), base, str(scan_id), mode],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, f"浏览器脚本失败：\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_browser
def test_browser_pick_two_and_draw_one_lands_three_questions(tmp_path):
    """AC4：点 2 个自动框 + 画 1 个人工框 → 提交 → 库里恰好多 3 条（逐条断言）。

    库里多出来的三条：`no=2`、`no=4` 的自动框各一条，加上人工框一条（接着编号 => 6）。
    人工框的 bbox 必须落在"按 rect 比例换算"出来的那四块坐标上（±3 像素），
    所以坐标不做换算时这条会红（AC7 变异 ②）；默认全选时条数不对也红（变异 ①）。
    """
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    scan_id = _seed(db, _blocks())
    before = _question_count(db)
    assert before == 0

    with live_server(app) as base:
        seen = _drive(base, scan_id, "pick", tmp_path)

    assert seen["counts"] == ["5", "0", "0"], f"默认应该一条都不勾：{seen['counts']}"
    assert seen["after_pick"] == "2", f"点了两个框应该已选 2：{seen['after_pick']}"
    assert seen["after_draw"] == ["5", "1", "3"], f"画完一个框：{seen['after_draw']}"
    assert seen["landed"].endswith(f"/scans/{scan_id}"), seen["landed"]

    assert _question_count(db) == before + 3, "库里应该恰好多 3 条"

    rows = {row["no"]: row for row in _scan_rows(db, scan_id)}
    assert len(rows) == 6, f"自动 5 行 + 人工 1 行 = 6，实际 {sorted(rows)}"
    picked = sorted(no for no, row in rows.items() if row["question_id"] is not None)
    assert picked == [2, 4, 6], f"入库的应该是第 2、4 题和人工框（6），实际 {picked}"
    assert rows[6]["crop_path"], "人工框也要有裁图"

    expected = seen["expect"]
    got = list(rows[6]["bbox"])
    assert all(abs(a - b) <= 3 for a, b in zip(got, expected)), (
        f"人工框坐标不对：库里 {got}，按 rect 换算应为 {expected}"
    )
    assert got[2] - got[0] > 150 and got[3] - got[1] > 150, f"框被当成屏幕像素了：{got}"
    print(
        f"浏览器：已选 {seen['after_draw'][2]}（自动 2 + 人工 1）| question +{_question_count(db) - before}"
        f" | 行 {sorted(rows)} | 入库 {picked} | 人工 bbox {tuple(got)} 期望 {tuple(expected)}"
        f" | 落点 {seen['landed']}"
    )


@needs_browser
def test_browser_390_has_no_horizontal_overflow(tmp_path):
    """AC6：390 宽下 scrollWidth == clientWidth == 390（图不撑破一屏）。"""
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    scan_id = _seed(db, _blocks())

    with live_server(app) as base:
        seen = _drive(base, scan_id, "measure", tmp_path)

    print(f"390×844：scrollWidth {seen['scroll'][0]} | clientWidth {seen['scroll'][1]}")
    assert seen["scroll"] == [390, 390], f"390 宽下有横向溢出：{seen['scroll']}"


# ---------------------------------------------------------------- Phase 3-b：反馈落库


@needs_browser
def test_browser_every_tick_and_every_left_behind_box_becomes_a_feedback_row(tmp_path):
    """AC2：18 个自动框里勾 2、剩 16 个不要、自己再画 1 个不是机器给的。

    提交后 `cut_feedback` 里这一页应该是 19 行（2 accept + 16 delete + 1 add）——
    注意 18 是**自动框数**（AC4 的 `stats.auto_frames`），人工补的那条不在其中。
    四个动作逐项断言，`redraw` 恒 0（v1 不判"人工框盖住哪个自动框"）。
    """
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    scan_id = _seed(db, _blocks(18))

    with live_server(app) as base:
        seen = _drive(base, scan_id, "feedback", tmp_path)
        with urllib.request.urlopen(base + "/api/cut-feedback/stats", timeout=30) as reply:
            stats = json.loads(reply.read().decode("utf-8"))

    assert seen["counts"] == ["18", "0", "0"], f"默认一条都不勾：{seen['counts']}"
    assert seen["after_pick"] == "2", seen
    assert seen["after_draw"] == ["18", "1", "3"], seen
    assert seen["landed"].endswith(f"/scans/{scan_id}"), seen["landed"]

    rows = _feedback_rows(db, scan_id)
    assert len(rows) == 19, f"2 accept + 16 delete + 1 add = 19 行，实际 {len(rows)}"
    assert _action_counts(rows) == {"accept": 2, "delete": 16, "redraw": 0, "add": 1}, (
        _action_counts(rows)
    )

    accept = [row for row in rows if row["action"] == "accept"]
    assert sorted(row["no"] for row in accept) == [1, 3], accept
    for row in accept:
        assert row["selected"] == 1, row
        assert row["auto_bbox"] == row["human_bbox"], f"接受就是原样收下：{row}"
        assert row["iou"] == 1.0, row
        assert row["source"] == "anchor_digit", row

    delete = [row for row in rows if row["action"] == "delete"]
    assert sorted(row["no"] for row in delete) == [n for n in range(1, 19) if n not in (1, 3)]
    for row in delete:
        assert row["selected"] == 0, row
        assert row["auto_bbox"] and row["human_bbox"] is None, f"删掉的框只留机器给的：{row}"
        assert row["iou"] is None, row

    add = [row for row in rows if row["action"] == "add"]
    assert len(add) == 1, add
    assert add[0]["frame_key"] == "m1", add[0]
    assert add[0]["no"] is None and add[0]["source"] == "manual", add[0]
    assert add[0]["auto_bbox"] is None, "人工框没有机器给的坐标"
    assert add[0]["human_bbox"] and add[0]["selected"] == 1, add[0]

    # 整页一份元数据，逐行一致：指纹、坐标系、配方、参数快照
    for row in rows:
        assert row["image_sha256"] == _seeded_sha256(db), row
        assert (row["image_w"], row["image_h"]) == (WIDTH, HEIGHT), row
        assert row["recipe"] == "scan-entry", row
        assert re.fullmatch(r"[0-9a-f]{16}", row["params_hash"] or ""), row

    assert stats["auto_frames"] == 18, stats
    assert stats["accepted"] == 2 and stats["deleted"] == 16 and stats["added"] == 1, stats
    assert stats["insufficient"] is True, stats

    print(
        f"反馈 {len(rows)} 行 | {_action_counts(rows)}"
        f" | 指纹 {rows[0]['image_sha256'][:12]}… | stats auto_frames {stats['auto_frames']}"
        f" insufficient {stats['insufficient']}"
    )


@needs_browser
def test_browser_frame_failure_writes_no_feedback_at_all(tmp_path):
    """AC3：把 `/frame` 打成 500 → 反馈表零新增（顺序断言）。

    `/questions` 那一条是成功的（所以库里会多 2 道题），失败的只是 `/frame`；
    反馈一条都不许写——否则库里会出现"反馈说选了、人工框其实没入库"的假标注。
    """
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    scan_id = _seed(db, _blocks(18))

    with live_server(app) as base:
        seen = _drive(base, scan_id, "frame500", tmp_path)

    assert "500" in seen["status"], seen["status"]
    assert seen["status_class"] == "notice notice--danger", seen["status_class"]
    assert seen["url"].endswith(f"/scans/{scan_id}/select"), f"失败了不该跳走：{seen['url']}"
    assert seen["picked"] == "3", f"勾的 2 个 + 画的 1 个都要留着：{seen['picked']}"

    assert _question_count(db) == 2, "/questions 是成功的，题库里应该有 2 道"
    assert _feedback_rows(db, scan_id) == [], "入库没全成，反馈表必须零新增"
    print(f"frame 500：红条「{seen['status']}」| 已选 {seen['picked']} | 反馈 0 行")


@needs_browser
def test_browser_rejected_submit_keeps_every_tick(tmp_path):
    """AC6：入库被打回（400）→ 红条提示，计数条上仍显示「已选 2」，反馈也不写。"""
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    scan_id = _seed(db, _blocks(18))

    with live_server(app) as base:
        seen = _drive(base, scan_id, "reject", tmp_path)

    assert "400" in seen["status"], seen["status"]
    assert seen["status_class"] == "notice notice--danger", seen["status_class"]
    assert seen["url"].endswith(f"/scans/{scan_id}/select"), f"失败了不该跳走：{seen['url']}"
    assert seen["picked"] == "2", f"勾过的 2 个还在：{seen['picked']}"
    assert seen["auto"] == "18", f"框一个都没少：{seen['auto']}"

    assert _question_count(db) == 0, "整体失败不该入库"
    assert _feedback_rows(db, scan_id) == [], "整体失败不该写反馈"
    print(f"400：红条「{seen['status']}」| 已选 {seen['picked']} | 自动框 {seen['auto']}")
