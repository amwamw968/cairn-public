"""圈题打磨卡（2026-11-01）：窄框提示 + 切错时的出口 + 撤销/清空。

只锁这张卡的三件小事，不动框选后端（幂等指纹、编号续号）与切题算法：

- AC2 窄框提示：框任一边 < 60px → 框旁边一句「这个框有点窄」；**不拦提交**
  （页面上那条判据在这里翻成 Python 跑一遍，同一个窄框照样能收进去）；
- AC3 出口：切题结果页每题都有「切得不对？自己框」，指回 `/scans/{id}/frame`；
- AC4 空页：一条题都没切出来时，页面正中给一个显眼的「自己框题」按钮；
- AC5 手势：撤销/清空是前端行为，只做静态断言（按钮 + JS 函数名 + 框数同步）；
- AC6 页面 200、用 `/static` 的 `.btn/.notice`、不新增内联 `<style>`。

真 OCR 一律不打：`_FakeOcr` 顶替 `cairn.ocr.recognize`，跟 test_scan_frame.py 同一套。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn import ocr, scan_service
from cairn.models import Learner, Scan, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.ocr.cut import QuestionBlock
from cairn.ocr.engine import OcrLine
from cairn.web import create_app

WIDTH, HEIGHT = 900, 1200
SHOT = "scans/2026/11/hints.jpg"


class _FakeOcr:
    """顶替整页 OCR：框内识别一律返回一行字，不打真模型。"""

    def __init__(self) -> None:
        self.text = "1. 计算 3 + 4 ="

    def __call__(self, path: str) -> list[OcrLine]:
        return [
            OcrLine(
                text=self.text,
                box=((0, 0), (40, 0), (40, 12), (0, 12)),
                confidence=0.95,
                y_center=6,
            )
        ]


@pytest.fixture()
def fake_ocr(monkeypatch):
    fake = _FakeOcr()
    monkeypatch.setattr(ocr, "recognize", fake)
    return fake


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """真文件库 + 一张能解码的整页图；OCR 关掉，避免现判方向时加载模型。"""
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    db = tmp_path / "hints.db"
    target = tmp_path / SHOT
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (WIDTH, HEIGHT), "white").save(target, format="JPEG", quality=95)
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as c:
        yield c, db


def _seed(db_path: Path, *, rows: int = 0) -> int:
    """落一张照片；`rows` > 0 时顺带落几条自动切题行（省掉现切题的 OCR）。"""
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
            )
            session.add(scan)
            session.commit()
            if rows:
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
                        for no in range(1, rows + 1)
                    ],
                    page_width=WIDTH,
                    page_height=HEIGHT,
                )
            return scan.id
    finally:
        engine.dispose()


def _count(db_path: Path, table: str) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return int(con.execute(f"select count(*) from {table}").fetchone()[0])
    finally:
        con.close()


# ------------------------------------------------------------------ 小工具


def _narrow_rule(html: str) -> tuple[int, str]:
    """取页面里的窄框阈值和判据表达式（阈值只认页面下发的那一个数）。"""
    threshold = re.search(r"var NARROW = (\d+);", html)
    assert threshold, "框选页里没有窄框阈值 var NARROW"
    body = re.search(r"function isNarrow\(f\) \{\s*return (.+?);\s*\}", html, re.S)
    assert body, "框选页里没有 isNarrow 判据"
    return int(threshold.group(1)), body.group(1)


def _is_narrow(expr: str, threshold: int, box: tuple[int, int, int, int]) -> bool:
    """把页面里那条判据翻成 Python 跑一遍：只换 `||` 与 `Math.abs`，不改比较。"""
    code = expr.replace("||", " or ").replace("&&", " and ").replace("Math.abs", "abs")
    x0, y0, x1, y1 = box
    frame = SimpleNamespace(x0=x0, y0=y0, x1=x1, y1=y1)
    scope = {"abs": abs, "NARROW": threshold, "f": frame}
    return bool(eval(code, {"__builtins__": {}}, scope))  # noqa: S307 - 只跑页面自己那条判据


# ------------------------------------------------------------------ AC2 窄框


def test_narrow_threshold_is_60_and_only_marks_thin_boxes(client, fake_ocr):
    """AC2：阈值 60；宽或高 < 60 才算窄，正常框（含正好 60）不算。"""
    c, db = client
    html = c.get(f"/scans/{_seed(db)}/frame").text

    threshold, expr = _narrow_rule(html)
    assert threshold == 60

    assert _is_narrow(expr, threshold, (60, 80, 110, 400))  # 宽 50
    assert _is_narrow(expr, threshold, (60, 80, 700, 130))  # 高 50
    assert not _is_narrow(expr, threshold, (60, 80, 120, 400))  # 宽正好 60
    assert not _is_narrow(expr, threshold, (60, 80, 540, 300))  # 正常框

    # 提示文案挂在框里（客户端渲染），且只在窄框那条分支上挂
    assert html.count("这个框有点窄，可能只框到一行") == 1
    assert "if (!isDraft && isNarrow(f))" in html


def test_narrow_frame_still_submits_and_lands_in_db(client, fake_ocr):
    """AC2：窄框只是提示——同一个窄框照样提交成功、收进去（不拦）。"""
    c, db = client
    scan_id = _seed(db)

    # 50×320：宽 50 < 60（会带提示），但仍高于后端 30px 的硬下限
    resp = c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,110,400", "subject": "数学", "question_type": "计算"},
    )

    assert resp.status_code == 200
    assert "已收好 1 题" in resp.text
    assert _count(db, "scan_question") == 1
    assert _count(db, "question") == 1


def test_submit_handler_never_looks_at_the_narrow_hint(client, fake_ocr):
    """AC2：提交那段 JS 不许拿 isNarrow 说事（拦提交的只有"一个框都没有"）。"""
    c, db = client
    html = c.get(f"/scans/{_seed(db)}/frame").text

    start = html.index("form.addEventListener('submit'")
    submit_block = html[start : start + 800]

    assert "isNarrow" not in submit_block
    assert "!frames.length" in submit_block  # 唯一拦下来的情况


# ------------------------------------------------------------------ AC3 出口


def test_questions_page_links_every_question_to_frame_page(client, fake_ocr):
    """AC3：每题旁边都有「切得不对？自己框」，href 指回这张照片的框选页。"""
    c, db = client
    scan_id = _seed(db, rows=3)

    body = c.get(f"/scans/{scan_id}/questions").text

    assert "共 3 题" in body
    assert body.count("切得不对？自己框") == 3
    # 只数逐题那批：列表外还有一条同样的页头入口（上一张卡就有）
    picks = body[body.index('<ul class="pick">') : body.index("</ul>")]
    assert picks.count(f'href="/scans/{scan_id}/frame"') == 3
    assert 'class="btn btn--ghost"' in picks


# ------------------------------------------------------------------ AC4 空页


def test_zero_questions_offers_prominent_frame_button(client, fake_ocr):
    """AC4：一条题都没切出来时，页面正中一个明示按钮「自己框题」。"""
    c, db = client
    scan_id = _seed(db)

    body = c.get(f"/scans/{scan_id}/questions").text

    assert "共 0 题" in body
    assert '<div class="empty">' in body
    assert '<a class="btn btn--primary" href="/scans/{}/frame">自己框题</a>'.format(scan_id) in body


# ------------------------------------------------------- AC5 撤销 / 清空


def test_undo_and_clear_are_frontend_only_with_count_in_sync(client, fake_ocr):
    """AC5：两个按钮 + 对应 JS 函数都在页面里；框数在同一个 render() 里同步。"""
    c, db = client
    html = c.get(f"/scans/{_seed(db)}/frame").text

    assert 'id="undo"' in html and "撤销上一个框" in html
    assert 'id="clear"' in html and "清空全部" in html
    assert "function undoLast()" in html
    assert "function clearAll()" in html
    assert "document.getElementById('undo').addEventListener('click', undoLast)" in html
    assert "document.getElementById('clear').addEventListener('click', clearAll)" in html

    # 撤销/清空都只改前端数组，然后 render()；框数显示跟着 frames.length 走
    assert "countEl.textContent = frames.length" in html
    render_block = html[html.index("function render()") : html.index("function showNarrowSummary()")]
    assert "countEl.textContent = frames.length" in render_block
    undo_block = html[html.index("function undoLast()") : html.index("function clearAll()")]
    assert "frames.pop()" in undo_block and "render()" in undo_block
    clear_block = html[html.index("function clearAll()") : html.index("function tip(text)")]
    assert "frames = []" in clear_block and "render()" in clear_block


# ------------------------------------------------------------------ AC6 页面


def test_frame_page_uses_static_assets_and_components(client, fake_ocr):
    """AC6：框选页 200，引 /static 两件样式、用 .btn/.notice，且没多出 <style>。"""
    c, db = client
    resp = c.get(f"/scans/{_seed(db)}/frame")

    assert resp.status_code == 200
    html = resp.text
    assert re.search(r'<link rel="stylesheet" href="/static/v3\.css(?:\?[^" ]+)?">', html)
    assert "tokens.css" not in html and "base.css" not in html
    assert 'class="btn' in html
    assert 'class="notice"' in html
    assert html.count("<style") == 0


def test_questions_page_uses_static_assets_and_components(client, fake_ocr):
    """AC6：切题结果页同样走 /static 的组件，不新增内联 <style>。"""
    c, db = client
    resp = c.get(f"/scans/{_seed(db, rows=2)}/questions")

    assert resp.status_code == 200
    html = resp.text
    assert re.search(r'<link rel="stylesheet" href="/static/v3\.css(?:\?[^" ]+)?">', html)
    assert "tokens.css" not in html and "base.css" not in html
    assert 'class="btn' in html
    assert html.count("<style") == 0
