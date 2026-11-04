"""卡 P5-b：数字化确认页（机器读题 → 家长改 → 收进去）。

这一页只做一件事：让机器把题块图读成三字段，画进表单，家长改对了再保存。

- **AC2 占位对**：GET 200，一页里有题块图、三个字段（预填库里的值）、两个 provider
  单选、一个主按钮；`<form>` 恰好 1 个（读与保存共用同一个表单，读键靠 formaction）。
- **AC3 读得出来**：POST `provider=local`（`describe` 被替换）→ 200，三字段里是假模型
  返回的值，`.facts` 里出现模型名与耗时；**库里一个字都没变**。
- **AC4 切得动**：POST `provider=cloud` → 假模型收到的 provider 是 `cloud`。
- **AC5 不认的值**：POST `provider=gpt` → 400，页面照常渲染，且一次模型调用都没发。
- **AC6 失败不炸**：`describe` 抛 `DigitizeError` → 200，红条 + 中文原因，三个字段仍
  可编辑，库里 `content` 不变。
- **AC7 390 宽**：固定视口 `scrollWidth == 390`，主按钮整行、2px 墨线、带箭头。

模型调用一层在 `tests/test_digitize_client.py`（P5-a）；这里整段替换 `cairn.web.describe`，
不碰真网络、不读真 key。
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
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn import web as web_module
from cairn.digitize import Digitized, DigitizeError
from cairn.models import Learner, Question, Scan, init_db, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.models.scan import ScanQuestion
from cairn.web import create_app

CROP = "scans/2026/11/crops/1-1.jpg"
QUESTION_CONTENT = "12 + 15 = ?"
QUESTION_ANSWER = "27"
QUESTION_ANALYSIS = "先算 12 + 15"
MODEL_STEM = "13 + 15 = ？"
MODEL_ANSWER = "28"
MODEL_ANALYSIS = "13 + 15 = 28，先凑十再相加"
MODEL_NAME = "Qwen3-VL-4B-Instruct-4bit"
FAIL_REASON = "连不上视觉模型服务（拒绝连接）。检查服务是否在跑。"


@pytest.fixture()
def web(tmp_path):
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as client:
        yield client, db, tmp_path


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


def _learner_id(db: Path) -> int:
    with _session(db) as session:
        learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
        if learner is None:  # 浏览器用例不经过 lifespan，默认孩子自己建
            learner = Learner(name="test-child", grade_level="primary-3")
            session.add(learner)
            session.commit()
        return learner.id


def _add_question(
    db: Path,
    *,
    content: str = QUESTION_CONTENT,
    answer: str | None = QUESTION_ANSWER,
    analysis: str | None = QUESTION_ANALYSIS,
) -> int:
    with _session(db) as session:
        question = Question(
            learner_id=_learner_id(db),
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content=content,
            answer=answer,
            analysis=analysis,
        )
        session.add(question)
        session.commit()
        return question.id


def _attach_crop(db: Path, root: Path, question_id: int, *, with_file: bool = True) -> None:
    """给题挂一条带裁图的切题行；`with_file=False` 只写库不落文件（模拟图丢了）。"""
    target = root / CROP
    target.parent.mkdir(parents=True, exist_ok=True)
    if with_file:
        Image.new("RGB", (120, 60), "white").save(target, format="JPEG")
    with _session(db) as session:
        scan = Scan(
            learner_id=_learner_id(db),
            device_id="upload",
            device_name="浏览器上传",
            width=1000,
            height=1400,
            captured_at=utcnow(),
            path="scans/2026/11/shot.jpg",
            source="upload",
        )
        session.add(scan)
        session.commit()
        session.add(
            ScanQuestion(
                scan_id=scan.id,
                no=1,
                bbox_json='{"bbox": [0, 0, 120, 60], "page": [1000, 1400], "angle": 0}',
                stem_text="",
                is_wrong=True,
                question_id=question_id,
                crop_path=CROP,
            )
        )
        session.commit()


def _seeded(db: Path, root: Path) -> int:
    question_id = _add_question(db)
    _attach_crop(db, root, question_id)
    return question_id


def _row(db: Path, question_id: int) -> dict:
    with _session(db) as session:
        question = session.get(Question, question_id)
        return {
            "content": question.content,
            "answer": question.answer,
            "analysis": question.analysis,
            "edited_at": question.edited_at,
        }


def _fingerprint(row: dict) -> str:
    joined = "\x1f".join(str(row[key]) for key in ("content", "answer", "analysis"))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class _FakeDescribe:
    """替身：记下每次调用的参数，按设定的结果或异常回话；不发请求、不读 key。"""

    def __init__(self, *, error: Exception | None = None, ms: int = 912) -> None:
        self.calls: list[dict] = []
        self.error = error
        self.ms = ms

    def __call__(self, image_path: str, *, provider: str | None = None) -> Digitized:
        self.calls.append({"image_path": image_path, "provider": provider})
        if self.error is not None:
            raise self.error
        return Digitized(
            stem=MODEL_STEM,
            answer=MODEL_ANSWER,
            analysis=MODEL_ANALYSIS,
            provider=provider or "local",
            model=MODEL_NAME,
            ms=self.ms,
        )


def _primary_count(html: str) -> int:
    return html.count('class="btn btn--primary"')


# --- AC2：页面形状 ----------------------------------------------------------


def test_page_shows_crop_three_fields_two_choices_and_one_primary(web):
    """AC2：图 + 三字段（预填库里）+ 两个单选 + 一个主按钮，一个 <form>。"""
    client, db, _root = web
    question_id = _seeded(db, _root)

    page = client.get(f"/questions/{question_id}/digitize")
    html = page.text

    assert page.status_code == 200
    assert f'src="/questions/{question_id}/crop"' in html
    assert f">{QUESTION_CONTENT}</textarea>" in html
    assert f'value="{QUESTION_ANSWER}"' in html
    assert f">{QUESTION_ANALYSIS}</textarea>" in html

    assert html.count('type="radio"') == 2
    assert 'name="provider" value="local"' in html
    assert 'name="provider" value="cloud"' in html

    assert html.count("<form") == 1
    assert _primary_count(html) == 1
    # 还没读：主按钮就是「让机器读这题」，且指向 digitize
    assert 'id="digitize-read"' in html
    read_tag = html.split('id="digitize-read"', 1)[0].rsplit("<button", 1)[1]
    assert "btn--primary" in read_tag
    assert f'formaction="/questions/{question_id}/digitize"' in html
    # 保存键一直在，只是还没读到东西时是次按钮
    assert 'id="digitize-save"' in html

    # 硬约束：页面不出现「AI」，模板里没有脚本、没有内联样式
    assert "AI" not in html
    template = Path("src/cairn/templates/digitize.html").read_text(encoding="utf-8")
    assert "<script" not in template
    assert "<style" not in template
    assert "style=" not in template
    assert "AI" not in template

    # GET 不发模型请求
    row = _row(db, question_id)
    assert row["content"] == QUESTION_CONTENT


def test_digitize_page_of_someone_elses_question_is_404(web):
    """别人的题当不存在。"""
    client, db, _root = web
    with _session(db) as session:
        other = Learner(name="另一个孩子", grade_level="primary-3")
        session.add(other)
        session.commit()
        other_id = other.id
    with _session(db) as session:
        question = Question(
            learner_id=other_id,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content="别人的题",
            answer="1",
        )
        session.add(question)
        session.commit()
        other_question_id = question.id

    assert client.get(f"/questions/{other_question_id}/digitize").status_code == 404
    assert (
        client.post(
            f"/questions/{other_question_id}/digitize", data={"provider": "local"}
        ).status_code
        == 404
    )


# --- AC3：读得出来，但不落库 ------------------------------------------------


def test_local_run_renders_model_values_and_stays_out_of_the_db(web, monkeypatch):
    """AC3：三字段变成假模型返回的值，`.facts` 有模型名与耗时；库里原值还在。"""
    client, db, _root = web
    question_id = _seeded(db, _root)
    before = _row(db, question_id)
    fake = _FakeDescribe()
    monkeypatch.setattr(web_module, "describe", fake)

    resp = client.post(f"/questions/{question_id}/digitize", data={"provider": "local"})

    assert resp.status_code == 200
    html = resp.text
    assert fake.calls and fake.calls[0]["provider"] == "local"
    assert fake.calls[0]["image_path"].endswith(CROP)

    assert f">{MODEL_STEM}</textarea>" in html
    assert f'value="{MODEL_ANSWER}"' in html
    assert f">{MODEL_ANALYSIS}</textarea>" in html
    assert QUESTION_CONTENT not in html.split('name="content"', 1)[1].split("</textarea>", 1)[0]

    # 结论条 + 事实条
    assert "这题读出来了" in html
    assert f"本地 {MODEL_NAME} · 0.9s" in html
    assert "3 个字段" in html

    # 读到了之后，主按钮换成「保存这道题」，读键退成次按钮
    assert _primary_count(html) == 1
    save_tag = html.split('id="digitize-save"', 1)[0].rsplit("<button", 1)[1]
    assert "btn--primary" in save_tag
    assert "再读一次" in html

    # 不落库：一个字都没写
    after = _row(db, question_id)
    assert after == before, after
    assert after["edited_at"] is None
    assert _fingerprint(after) == _fingerprint(before)
    print(f"读出来三个字段后库里仍是指纹 {_fingerprint(after)[:16]}…")


def test_cloud_run_passes_provider_through(web, monkeypatch):
    """AC4：`provider=cloud` 原样交给模型层（断言调用参数，不打印 key）。"""
    client, db, _root = web
    question_id = _seeded(db, _root)
    fake = _FakeDescribe(ms=2300)
    monkeypatch.setattr(web_module, "describe", fake)

    resp = client.post(f"/questions/{question_id}/digitize", data={"provider": "cloud"})

    assert resp.status_code == 200
    assert fake.calls[0]["provider"] == "cloud"
    assert "云端 " + MODEL_NAME in resp.text
    # 选过的那个单选保持选中，方便「再读一次」
    assert re.search(r'value="cloud"\s+checked', resp.text)
    assert _row(db, question_id)["content"] == QUESTION_CONTENT


def test_missing_provider_falls_back_to_local(web, monkeypatch):
    """缺省 provider 走本地（页面缺了 radio 也不至于 400）。"""
    client, db, _root = web
    question_id = _seeded(db, _root)
    fake = _FakeDescribe()
    monkeypatch.setattr(web_module, "describe", fake)

    resp = client.post(f"/questions/{question_id}/digitize", data={})

    assert resp.status_code == 200
    assert fake.calls[0]["provider"] == "local"


# --- AC5：不认的 provider ---------------------------------------------------


def test_unknown_provider_is_400_and_never_calls_the_model(web, monkeypatch):
    """AC5：`provider=gpt` → 400，页面照常（字段还在），一次模型调用都没发。"""
    client, db, _root = web
    question_id = _seeded(db, _root)
    fake = _FakeDescribe()
    monkeypatch.setattr(web_module, "describe", fake)

    resp = client.post(f"/questions/{question_id}/digitize", data={"provider": "gpt"})

    assert resp.status_code == 400
    assert fake.calls == []
    assert "本机" in resp.text and "云端" in resp.text
    assert f">{QUESTION_CONTENT}</textarea>" in resp.text
    assert resp.text.count("<form") == 1
    assert _row(db, question_id)["content"] == QUESTION_CONTENT


# --- AC6：模型失败也不炸、不改库 --------------------------------------------


def test_model_failure_renders_red_bar_and_keeps_fields_editable(web, monkeypatch):
    """AC6：`DigitizeError` → 200 + 红条中文原因；三字段仍可编辑；库不变。"""
    client, db, _root = web
    question_id = _seeded(db, _root)
    before = _row(db, question_id)
    fake = _FakeDescribe(error=DigitizeError(FAIL_REASON))
    monkeypatch.setattr(web_module, "describe", fake)

    resp = client.post(f"/questions/{question_id}/digitize", data={"provider": "local"})

    assert resp.status_code == 200
    html = resp.text
    assert "模型没读出来" in html
    assert "请手动填，或换云端模型再试" in html
    assert FAIL_REASON in html
    assert re.search(
        rf'<p class="notice notice--danger" role="alert">\s*{re.escape(FAIL_REASON)}\s*</p>',
        html,
    )

    # 三个字段还是在表单里、还是库里的值，可以接着手改
    assert f">{QUESTION_CONTENT}</textarea>" in html
    assert f'value="{QUESTION_ANSWER}"' in html
    assert f">{QUESTION_ANALYSIS}</textarea>" in html
    assert html.count("<textarea") == 2

    # 没写库
    after = _row(db, question_id)
    assert after == before, after
    assert after["edited_at"] is None
    print(f"失败页仍是 {resp.status_code}，库里 content={after['content']!r}")


def test_reading_never_writes_even_when_it_succeeds(web, monkeypatch):
    """AC6 的后半句「任何情况都不许写库」：失败不写，读成功了也不写。"""
    client, db, _root = web
    question_id = _seeded(db, _root)
    before = _row(db, question_id)
    monkeypatch.setattr(web_module, "describe", _FakeDescribe())

    ok = client.post(f"/questions/{question_id}/digitize", data={"provider": "cloud"})

    assert ok.status_code == 200
    assert MODEL_STEM in ok.text          # 页面上确实换了值
    after = _row(db, question_id)
    assert after == before, after          # 库里还是原值：确认页不落库
    assert after["edited_at"] is None


def test_question_without_crop_says_so_instead_of_500(web, monkeypatch):
    """没有原图：同一页给红条，不 500，也不去调模型。"""
    client, db, _root = web
    question_id = _add_question(db)
    fake = _FakeDescribe()
    monkeypatch.setattr(web_module, "describe", fake)

    page = client.get(f"/questions/{question_id}/digitize")
    assert page.status_code == 200
    assert "/crop" not in page.text

    resp = client.post(f"/questions/{question_id}/digitize", data={"provider": "local"})
    assert resp.status_code == 200
    assert "没有原图" in resp.text
    assert fake.calls == []
    assert _row(db, question_id)["content"] == QUESTION_CONTENT


def test_saving_from_the_digitize_page_still_goes_through_edit(web, monkeypatch):
    """读到的值要家长点保存才收进去：POST /edit 那条路没变，读的那次不写库。"""
    client, db, _root = web
    question_id = _seeded(db, _root)
    fake = _FakeDescribe()
    monkeypatch.setattr(web_module, "describe", fake)

    client.post(f"/questions/{question_id}/digitize", data={"provider": "local"})
    assert _row(db, question_id)["content"] == QUESTION_CONTENT

    saved = client.post(
        f"/questions/{question_id}/edit",
        data={
            "content": MODEL_STEM,
            "answer": MODEL_ANSWER,
            "analysis": MODEL_ANALYSIS,
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    row = _row(db, question_id)
    assert row["content"] == MODEL_STEM
    assert row["answer"] == MODEL_ANSWER
    assert row["edited_at"] is not None


# --- 扫描页入口：ready 状态多一个「看这道题」 -------------------------------


def _ready_scan(db: Path) -> tuple[int, int]:
    """一张裁好的卷子 + 三道已收好的题；返回 (scan_id, 第一条题 id)。"""
    with _session(db) as session:
        scan = Scan(
            learner_id=_learner_id(db),
            device_id="upload",
            device_name="浏览器上传",
            width=1000,
            height=1400,
            captured_at=utcnow(),
            path="scans/2026/11/shot.jpg",
            source="upload",
        )
        session.add(scan)
        session.commit()
        first_question_id = None
        for no in (1, 2, 3):
            question = Question(
                learner_id=_learner_id(db),
                subject="数学",
                grade_level="primary-3",
                question_type="计算",
                content=f"{no}. 计算 {no} + {no} =",
                answer=str(no * 2),
            )
            session.add(question)
            session.commit()
            if first_question_id is None:
                first_question_id = question.id
            session.add(
                ScanQuestion(
                    scan_id=scan.id,
                    no=no,
                    bbox_json='{"bbox": [0, 0, 10, 10], "page": [1000, 1400], "angle": 0}',
                    stem_text="",
                    is_wrong=True,
                    question_id=question.id,
                )
            )
        session.commit()
        return scan.id, first_question_id


def test_scan_ready_offers_review_link_to_first_question(web, tmp_path):
    """ready 状态的卷子详情页多一个次按钮「看这道题」，指向第一条题的确认页。"""
    client, db, _root = web
    scan_id, first_question_id = _ready_scan(db)
    # 裁切记录要落 sidecar，stage 才是 ready
    record = tmp_path / "scans" / "2026" / "11" / "shot.crop.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        '{"reason": "ok", "area_ratio": 0.44, "note": "完成"}', encoding="utf-8"
    )

    html = client.get(f"/scans/{scan_id}").text

    assert "开始复习" in html
    assert (
        f'href="/questions/{first_question_id}/digitize">看这道题</a>' in html
    )


# --- AC7：390 宽无横向溢出（真浏览器） -------------------------------------


def _browser_python() -> str | None:
    import os

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

base, qid = sys.argv[1], sys.argv[2]
out = {}
with sync_playwright() as p:
    browser = p.chromium.launch()
    # 固定 390 视口：is_mobile=True 会自己撑到最小内容宽，量不出溢出
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=False)
    page.goto(base + "/questions/" + qid + "/digitize", wait_until="load")
    out["scroll"] = page.evaluate(
        "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
    )
    out["save"] = page.evaluate(
        "() => {"
        " var el = document.querySelector('.select-actions .btn--primary');"
        " var s = getComputedStyle(el);"
        " return {"
        "  id: el.id,"
        "  w: Math.round(el.getBoundingClientRect().width),"
        "  form: Math.round(el.closest('form').getBoundingClientRect().width),"
        "  bg: s.backgroundColor,"
        "  height: el.getBoundingClientRect().height"
        " };"
        "}"
    )
    out["forms"] = page.evaluate("() => document.querySelectorAll('form').length")
    out["choices"] = page.evaluate(
        "() => document.querySelectorAll('input[name=provider]').length"
    )
    out["crop"] = page.locator('.figure img').evaluate(
        "el => ({visible: !!el.getClientRects().length && getComputedStyle(el.parentElement).display !== 'none', loaded: el.naturalWidth > 0, width: el.getBoundingClientRect().width})"
    )
    out["quiet"] = page.locator('#digitize-save').evaluate(
        "el => ({border: getComputedStyle(el).borderTopWidth, background: getComputedStyle(el).backgroundColor})"
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
def test_browser_390_no_horizontal_overflow(tmp_path):
    """At 390px the page fits and its primary action is full width and green."""
    db = tmp_path / "web.db"
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)  # 浏览器用例不经过 lifespan，自己建表
    engine.dispose()
    app = create_app(f"sqlite:///{db}")
    question_id = _add_question(db)
    _attach_crop(db, tmp_path, question_id)
    # 塞一段很长的题干：确认页的断点风险就在长文本上
    with _session(db) as session:
        session.get(Question, question_id).content = "12 + 15 = ？" * 40
        session.commit()

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
    assert seen["forms"] == 1
    assert seen["choices"] == 2
    assert seen["crop"]["visible"] and seen["crop"]["loaded"]
    assert 0 < seen["crop"]["width"] <= 390
    assert seen["quiet"]["border"] == "1px" and seen["quiet"]["background"] == "rgb(255, 255, 255)"
    assert seen["save"]["w"] == seen["save"]["form"], seen["save"]
    assert seen["save"]["bg"] == "rgb(15, 110, 86)", seen["save"]
    assert seen["save"]["height"] >= 44, seen["save"]
    print(
        f"390×844：scrollWidth {seen['scroll'][0]} | 主按钮 {seen['save']['id']}"
        f" {seen['save']['w']}px（表单 {seen['save']['form']}px）"
        f"| 背景 {seen['save']['bg']} | 高度 {seen['save']['height']}"
    )
