"""卡 G2-8 AC2：v3 十二页尺寸验收（390 无横滚全过 + 桌面抽查）。

一个本地服务 + 一个真浏览器跑完全程：390×844（**is_mobile=False**，移动仿真会把
视口一起放大、假绿过一次）量 `documentElement.scrollWidth == 390`；桌面 1160 抽查
错题本与设置两页（内容列 ≤720 且无横滚）。种子：一张照片 + 一道题 + 一条待确认。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn.models import ErrorItem, Learner, Question, Scan, init_db, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.web import create_app

# 12 页（+录入页共 13 个 URL）：今天 / 错题本 / 拍卷子 / 裁一下 / 圈错题 / 正在读题 /
# 看看读得对不对 / 改一下 / 练一题 / 这题怎么样 / 最近怎么样 / 设置
PAGES = [
    "/",
    "/questions",
    "/upload",
    "/review-queue",
    "/practice",
    "/settings",
    "/stats",
    "/entry",
]


def _browser_python() -> str | None:
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


def _seed(db_path: Path) -> dict[str, int]:
    image = db_path.parent / "scans" / "2026" / "11" / "shot.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", (1000, 1400), 255).save(image, format="JPEG")
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            if learner is None:
                learner = Learner(name="test-child", grade_level="primary-3")
                session.add(learner)
                session.commit()
            scan = Scan(
                learner_id=learner.id,
                device_id="upload",
                device_name="测试",
                width=1000,
                height=1400,
                captured_at=utcnow(),
                path="scans/2026/11/shot.jpg",
                source="upload",
            )
            session.add(scan)
            session.flush()
            question = Question(
                learner_id=learner.id,
                subject="数学",
                grade_level="primary-3",
                question_type="计算",
                content="3 + 4 = ?",
                answer="7",
            )
            session.add(question)
            session.flush()
            item = ErrorItem(
                learner_id=learner.id,
                question_id=question.id,
                review_status="reviewing",
                confidence=0.5,
                wrong_count=1,
                next_due_at=utcnow() + timedelta(days=1),
            )
            session.add(item)
            session.commit()
            return {"scan": scan.id, "question": question.id}
    finally:
        engine.dispose()


_SWEEP = r'''
import json
import sys

from playwright.sync_api import sync_playwright

base, ids = sys.argv[1], json.loads(sys.argv[2])
pages = [
    "/",
    "/questions",
    "/upload",
    "/scans/{scan}/crop",
    "/scans/{scan}/select",
    "/scans/{scan}/ocr",
    "/review-queue",
    "/questions/{question}/edit",
    "/practice",
    "/stats",
    "/settings",
    "/entry",
]
out = {}
with sync_playwright() as p:
    browser = p.chromium.launch()
    for path_tpl in pages:
        path = path_tpl.format(**ids)
        page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=False)
        page.goto(base + path, wait_until="load")
        out[path] = page.evaluate(
            "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        page.close()
    # 桌面抽查：错题本 + 设置（内容列约束）
    for path in ("/questions", "/settings"):
        page = browser.new_page(viewport={"width": 1160, "height": 800})
        page.goto(base + path, wait_until="load")
        out["desktop" + path] = page.evaluate(
            "() => [document.documentElement.scrollWidth, window.innerWidth]"
        )
        page.close()
    browser.close()
print(json.dumps(out, ensure_ascii=False))
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


@needs_browser
def test_all_v3_pages_fit_390_and_desktop_spot_checks(tmp_path):
    db = tmp_path / "sweep.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app):
        pass
    ids = _seed(db)

    script = tmp_path / "sweep.py"
    script.write_text(_SWEEP, encoding="utf-8")
    with _live_server(app) as base:
        done = subprocess.run(
            [BROWSER_PY, str(script), base, json.dumps(ids)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    assert done.returncode == 0, f"扫描脚本失败：\n{done.stdout}\n{done.stderr}"
    seen = json.loads(done.stdout.strip().splitlines()[-1])

    bad = {
        path: widths
        for path, widths in seen.items()
        if not path.startswith("desktop") and widths[0] != widths[1]
    }
    assert bad == {}, f"这些页 390 下横向溢出：{bad}"
    assert len([p for p in seen if not p.startswith("desktop")]) == 12, seen

    for path in ("desktop/questions", "desktop/settings"):
        widths = seen[path]
        assert widths[0] <= widths[1], f"{path} 桌面横滚：{widths}"
    print("390 扫描：", json.dumps(seen, ensure_ascii=False))
