"""工具层（底座第一步）：八样能力收成同一个形状，八个开关各走各的降级路。

本文件管三件事：

1. 形状（AC2 / AC8）：`status()` 八个工具、五个键、`reason` 开着时为 None；
   registry 是写死的清单，不做动态发现。
2. 开关（AC3）：`CAIRN_TOOL_OCR=0` 与老的 `CAIRN_OCR_DISABLE=1` 完全等价。
3. 降级（AC4 / AC5）：每个工具各关一次，相关页面仍是 200；`/tools` 一眼看清
   谁关了、为什么关。

每条用例都从"八全开"起跑（`_all_tools_on`），要关谁自己 setenv；
真实 DB 用 tmp_path 下的 sqlite 文件，不碰仓库里的 cairn.db。
"""

from __future__ import annotations

import dataclasses
import io
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select

from cairn import dict as dict_service
from cairn import scan_service, tools
from cairn.models import ErrorItem, Learner, Question, Scan, make_engine, make_session_factory
from cairn.models.review import Review
from cairn.models.scan import ScanQuestion
from cairn.web import create_app

TOOL_TITLES = ("预处理", "裁边", "增强", "切题", "文字识别", "判题", "词典", "朗读")


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _all_tools_on(monkeypatch):
    """每条用例从"八全开"起跑：谁要关谁自己 setenv（含老变量）。"""
    for spec in tools.TOOLS:
        monkeypatch.delenv(spec.env, raising=False)
        if spec.legacy_env is not None:
            monkeypatch.delenv(spec.legacy_env, raising=False)
    yield


@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "web.db"


@pytest.fixture()
def client(db_path) -> TestClient:
    app = create_app(f"sqlite:///{db_path}")
    with TestClient(app) as c:
        # Exercise the historical upload and scan contract.
        from cairn.models.workflow import Workflow
        with app.state.session_factory() as session:
            session.get(Workflow, "default").active_version_id = "default:v1"
            session.commit()
        yield c


def open_session(db_path):
    engine = make_engine(f"sqlite:///{db_path}")
    return make_session_factory(engine)()


# ---------------------------------------------------------------- 素材


def page_photo(size: tuple[int, int] = (1000, 1200)) -> Image.Image:
    """合成一张"拍歪了的卷子"：深色桌面 + 白色四边形纸张 + 几行黑字。

    短边 1000 正好踩在上传下限上；纸占画面中间 6 成，预处理找得到四边。
    """
    quad = ((140, 180), (860, 140), (900, 1040), (120, 1080))
    image = Image.new("RGB", size, (68, 70, 74))
    draw = ImageDraw.Draw(image)
    draw.polygon(quad, fill=(246, 246, 242))
    left, right = 260, 740
    for index in range(8):
        y = 320 + index * 90
        draw.rectangle([left, y, right, y + 14], fill=(25, 25, 25))
    return image


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def upload_scan(client) -> int:
    """走真实上传通道（multipart），返回新的 scan id。"""
    response = client.post(
        "/scans/upload",
        files={"file": ("page.png", png_bytes(page_photo()), "image/png")},
        data={"quality": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[-1])


def make_item(
    db_path,
    *,
    content: str = "12 + 15 = ?",
    answer: str | None = "27",
    audio_text: str | None = None,
    lang: str = "zh",
) -> int:
    """直接建一道题 + 一条错题记录，返回 error_item id（练习页用）。"""
    with open_session(db_path) as session:
        learner_id = session.scalars(select(Learner)).first().id
        question = Question(
            learner_id=learner_id,
            subject="数学",
            grade_level="primary",
            question_type="填空",
            content=content,
            answer=answer,
            lang=lang,
            audio_text=audio_text,
        )
        session.add(question)
        session.flush()
        item = ErrorItem(learner_id=learner_id, question_id=question.id)
        session.add(item)
        session.commit()
        return item.id


def build_fake_dict(path: Path) -> None:
    """小型假词典：结构与 scripts/build_dict.py 的产物一致。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE en(word TEXT PRIMARY KEY, translation TEXT, phonetic TEXT, pos TEXT);
        CREATE TABLE en_variant(variant TEXT PRIMARY KEY, lemma TEXT);
        CREATE TABLE zh(term TEXT, pinyin TEXT, PRIMARY KEY(term, pinyin));
        """
    )
    conn.execute("INSERT INTO en VALUES (?,?,?,?)", ("apple", "n. 苹果", "ˈæp(ə)l", "n:"))
    conn.execute("INSERT INTO en VALUES (?,?,?,?)", ("pear", "n. 梨", "peə", "n:"))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 形状（AC2）


def test_status_has_eight_tools_with_five_keys() -> None:
    rows = tools.status()

    assert len(rows) == 8
    assert [row["name"] for row in rows] == [
        "preprocess",
        "crop",
        "enhance",
        "cut",
        "ocr",
        "grading",
        "dict",
        "tts",
    ]
    for row in rows:
        assert set(row) == {"name", "title", "description", "enabled", "reason"}
        assert row["title"] in TOOL_TITLES
        assert row["description"]  # 一句话说它干什么
        assert row["enabled"] is True
        assert row["reason"] is None  # 开着时没有原因（AC2）


def test_switch_turns_only_that_tool_off(monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_TOOL_CUT", "0")

    rows = {row["name"]: row for row in tools.status()}

    assert rows["cut"]["enabled"] is False
    assert "CAIRN_TOOL_CUT" in rows["cut"]["reason"]
    assert rows["grading"]["enabled"] is True
    assert rows["grading"]["reason"] is None


def test_spec_is_frozen_and_defaults_to_on() -> None:
    spec = tools.get("dict")

    assert spec.enabled() is True
    assert spec.disabled_reason() is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.title = "改了"  # type: ignore[misc]


def test_unrecognised_value_falls_back_to_default(monkeypatch) -> None:
    """写了个看不懂的值（不是 0/1）时不猜：按默认开，别把工具悄悄关掉。"""
    monkeypatch.setenv("CAIRN_TOOL_DICT", "maybe")

    assert tools.is_enabled("dict") is True


def test_require_raises_when_the_tool_is_off(monkeypatch) -> None:
    assert tools.require("tts").name == "tts"

    monkeypatch.setenv("CAIRN_TOOL_TTS", "0")
    with pytest.raises(tools.ToolDisabled) as excinfo:
        tools.require("tts")
    assert "朗读已关闭" in str(excinfo.value)


def test_unknown_tool_name_is_loud() -> None:
    """名字写错要吵：静默返回 False 会让"开关没生效"和"拼错了"长得一样。"""
    with pytest.raises(tools.UnknownTool):
        tools.is_enabled("ocrr")


def test_registry_is_a_static_list() -> None:
    """AC8：写死的清单，不许 importlib / pkgutil / os.listdir 之类动态发现。"""
    source = Path(tools.registry.__file__).read_text(encoding="utf-8")

    for forbidden in ("importlib", "pkgutil", "listdir", "walk_packages", "__import__"):
        assert forbidden not in source, forbidden
    assert isinstance(tools.TOOLS, tuple)


# ------------------------------------------------- 开关等价（AC3）


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [("CAIRN_TOOL_OCR", "0"), ("CAIRN_OCR_DISABLE", "1")],
)
def test_ocr_switch_and_legacy_switch_are_equivalent(
    client, db_path, monkeypatch, env_name: str, env_value: str
) -> None:
    """新变量关掉 = 老变量关掉：同一张照片、同一个页面、同一句提示。"""
    scan_id = upload_scan(client)
    monkeypatch.setenv(env_name, env_value)

    assert tools.is_enabled("ocr") is False
    assert "文字识别已关闭" in tools.disabled_reason("ocr")
    response = client.get(f"/scans/{scan_id}/ocr")
    assert response.status_code == 200
    assert "文字识别已关闭" in response.text


# ------------------------------------------------- 六条降级（AC4）


def test_preprocess_off_keeps_original_and_leaves_processed_path_null(
    client, db_path, monkeypatch
) -> None:
    # 先证明这张合成图确实能被预处理（不然"关掉后没有 processed"是废话）
    kept = upload_scan(client)
    with open_session(db_path) as session:
        assert session.get(Scan, kept).processed_path is not None

    monkeypatch.setenv("CAIRN_TOOL_PREPROCESS", "0")
    scan_id = upload_scan(client)
    with open_session(db_path) as session:
        scan = session.get(Scan, scan_id)
        assert scan.processed_path is None
        assert scan_service.image_path(scan, f"sqlite:///{db_path}").is_file()  # 原图还在

    # 磁盘上只有刚才那张开着的处理图，关掉的那次一张都没留
    processed = list((Path(db_path).parent / "scans").rglob("*.processed.jpg"))
    assert len(processed) == 1

    detail = client.get(f"/scans/{scan_id}")
    assert detail.status_code == 200


def test_cut_off_does_not_cut_and_tells_the_parent_to_frame(client, db_path, monkeypatch) -> None:
    scan_id = upload_scan(client)
    monkeypatch.setenv("CAIRN_TOOL_CUT", "0")

    page = client.get(f"/scans/{scan_id}/questions")
    assert page.status_code == 200
    assert "自动切题已关闭" in page.text

    with open_session(db_path) as session:
        assert session.scalars(select(ScanQuestion)).all() == []

    # 直接 POST 也不许 500
    posted = client.post(
        f"/scans/{scan_id}/questions",
        data={"subject": "数学", "question_type": "填空"},
    )
    assert posted.status_code == 200
    assert "自动切题已关闭" in posted.text


def test_ocr_off_guides_to_manual_framing(client, db_path, monkeypatch) -> None:
    scan_id = upload_scan(client)
    monkeypatch.setenv("CAIRN_TOOL_OCR", "0")

    ocr_page = client.get(f"/scans/{scan_id}/ocr")
    assert ocr_page.status_code == 200
    assert "文字识别已关闭" in ocr_page.text

    questions = client.get(f"/scans/{scan_id}/questions")
    assert questions.status_code == 200
    assert "文字识别已关闭" in questions.text


def test_grading_off_always_asks_the_parent(client, db_path, monkeypatch) -> None:
    item_id = make_item(db_path)

    # 开着：确定性判题直接给结果，不出现两键
    auto = client.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "27"}
    )
    assert auto.status_code == 200
    assert "机器判的" in auto.text  # G2-5 文案
    with open_session(db_path) as session:
        reviews_after_auto = len(session.scalars(select(Review)).all())

    monkeypatch.setenv("CAIRN_TOOL_GRADING", "0")
    manual = client.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "27"}
    )
    assert manual.status_code == 200
    assert "这道题还没有标准答案" in manual.text  # 复用 ManualGrade 的文案
    assert "做对了" in manual.text
    assert "没做对" in manual.text
    assert "机器判的" not in manual.text

    with open_session(db_path) as session:
        # 判题关掉时一行簿记都不写（与"没有标准答案"同一个分支）
        assert len(session.scalars(select(Review)).all()) == reviews_after_auto


def test_dict_off_stops_autofill(client, db_path, monkeypatch, tmp_path) -> None:
    path = tmp_path / "dict.sqlite"
    build_fake_dict(path)
    monkeypatch.setenv("CAIRN_DICT_DB", str(path))
    dict_service.reset_cache()
    try:
        apple = {"subject": "英语", "question_type": "单词", "content": "apple", "answer": "apple"}
        assert client.post("/entry", data=apple).status_code == 200
        with open_session(db_path) as session:
            assert session.scalars(select(Question)).one().content == "n. 苹果"

        monkeypatch.setenv("CAIRN_TOOL_DICT", "0")
        pear = {"subject": "英语", "question_type": "单词", "content": "pear", "answer": "pear"}
        assert client.post("/entry", data=pear).status_code == 200
        with open_session(db_path) as session:
            contents = sorted(q.content for q in session.scalars(select(Question)))
        # 关掉后词典不参与：家长填 "pear" 就是 "pear"，不会被补成 "n. 梨"
        assert contents == ["n. 苹果", "pear"]
    finally:
        dict_service.reset_cache()


def test_tts_off_returns_503_and_hides_the_play_button(client, db_path, monkeypatch) -> None:
    item_id = make_item(
        db_path, content="水果的一种", answer="苹果", audio_text="píngguǒ", lang="zh"
    )

    on = client.get(f"/practice?item_id={item_id}")
    assert on.status_code == 200
    assert 'class="play"' in on.text  # 听写题的朗读按钮

    monkeypatch.setenv("CAIRN_TOOL_TTS", "0")
    off = client.get(f"/practice?item_id={item_id}")
    assert off.status_code == 200
    assert 'class="play"' not in off.text
    assert "水果的一种" in off.text  # 题面照常，只是不念了

    speech = client.post("/v1/speech", json={"text": "第一题"})
    assert speech.status_code == 503
    assert "朗读已关闭" in speech.json()["error"]


# ------------------------------------------------- /tools 页（AC5）


def test_tools_page_lists_all_eight_tools(client) -> None:
    response = client.get("/tools")

    assert response.status_code == 200
    for title in TOOL_TITLES:
        assert title in response.text
    assert response.text.count("开着") >= 8
    # 用统一的基础组件，不写内联样式
    assert "<style" not in response.text
    assert "/static/v3.css" in response.text
    assert "/static/tokens.css" not in response.text and "/static/base.css" not in response.text


def test_tools_page_shows_eight_reasons_when_everything_is_off(client, monkeypatch) -> None:
    for spec in tools.TOOLS:
        monkeypatch.setenv(spec.env, "0")

    response = client.get("/tools")

    assert response.status_code == 200
    assert response.text.count("已关闭") == 8
    assert "关了" in response.text
