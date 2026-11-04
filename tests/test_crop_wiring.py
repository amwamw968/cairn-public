"""裁到纸 · 第二步 a：把第一步的纯函数接进上传链路、配方与痕迹。

五件事：

1. **第 8 个工具 `crop`**：默认开；`CAIRN_TOOL_CROP=0` = 从不自动裁（走原图、
   不报错、不 500），那一步照实说"裁切 已关闭"。
2. **上传链路**：方向归正 → `detect_paper` → 闸门 → 裁或原样 → 落盘。裁的时候
   读的是**原图**（增强图是后面才产出的）；结果写进**另一份文件**
   `<uuid>.crop.png`（同目录、无损 PNG），**上传的那份原图一个字节不改**。
3. **读图链路**：裁好的 → 增强图 → 原图；「看原图」那个入口仍然是上传时的原字节。
4. **配方**：两份配方各有「裁切」节点，位置在「摆正」之后、「增强」之前，工具名
   `crop`；上传默认配方是 `scan-entry`（不带增强）。
5. **痕迹**：八句人话原样念给家长（`docs/PLAN.md`「卡：裁到纸 · 第二步 a」）。

真图用例只用仓库里的 `docs/samples/*.jpg`，不依赖 `CAIRN_SAMPLE_DIR`。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn import recipe, scan_service, tools
from cairn.models import Scan, make_engine, make_session_factory
from cairn.models.scan import ScanQuestion
from cairn.web import SCAN_RECIPE, create_app

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "docs" / "samples"

# 第一步实测的两张：红底那张该裁（纸 45%），白纸白底那张不该裁（分不开）
HI_REDBG = SAMPLES / "hi-redbg.jpg"
USER_PHOTO = SAMPLES / "user-20261101.jpg"

_ENGINE_INSTALLED = importlib.util.find_spec("rapidocr_onnxruntime") is not None
needs_engine = pytest.mark.skipif(
    not _ENGINE_INSTALLED, reason="真图切题需要已安装 rapidocr-onnxruntime"
)


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _tools_on_and_recipe_fresh(monkeypatch):
    """每条用例从"八全开 + 配方缓存干净"起跑。"""
    for spec in tools.TOOLS:
        monkeypatch.delenv(spec.env, raising=False)
        if spec.legacy_env is not None:
            monkeypatch.delenv(spec.legacy_env, raising=False)
    recipe.clear_cache()
    yield
    recipe.clear_cache()


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


@pytest.fixture()
def restore_engine():
    """真引擎跑完放回去，`test_ocr` 那条"导入不加载引擎"才不会被带坏。"""
    from cairn.ocr import engine as ocr_engine

    saved = ocr_engine._engine
    yield
    ocr_engine._engine = saved


# ---------------------------------------------------------------- 素材


def upload(client: TestClient, path: Path) -> int:
    """按浏览器上传那条路交一张真图，返回 scan id（端点 303 到 `/scans/<id>`）。"""
    data = path.read_bytes()
    response = client.post(
        "/scans/upload",
        files={"file": (path.name, data, "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    return int(response.url.path.rsplit("/", 1)[-1])


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def crop_file_for(stored: Path) -> Path:
    """原图 → 它旁边那份裁好的图（`<uuid>.jpg` → `<uuid>.crop.png`）。"""
    return stored.with_name(f"{stored.stem}{scan_service.CROP_FILE_MARK}.png")


def load_scan(db_path: Path, scan_id: int) -> tuple[Scan, Path]:
    """取一行 scan 和它 `path` 指向的绝对路径（会话关掉再用）。"""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            scan = session.get(Scan, scan_id)
            return scan, db_path.parent / scan.path
    finally:
        engine.dispose()


def records(db_path: Path) -> list[Path]:
    return sorted((db_path.parent / "scans").rglob(f"*{scan_service.CROP_RECORD_SUFFIX}"))


def question_numbers(db_path: Path, scan_id: int) -> list[int]:
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            rows = session.scalars(
                select(ScanQuestion).where(ScanQuestion.scan_id == scan_id)
            ).all()
            return [row.no for row in rows]
    finally:
        engine.dispose()


# ---------------------------------------------------------------- AC2：配方


def test_default_recipe_is_scan_entry_without_enhance() -> None:
    """AC2：默认配方是 scan-entry（不带增强），顺序摆正 → 裁切 → 切题 → 认字。"""
    assert SCAN_RECIPE == "scan-entry"

    current = recipe.load(SCAN_RECIPE)

    assert [step.name for step in current.steps] == ["摆正", "裁切", "切题", "认字"]
    assert [step.tool for step in current.steps] == ["preprocess", "crop", "cut", "ocr"]
    assert "增强" not in [step.name for step in current.steps]


def test_both_recipes_cut_between_upright_and_enhance() -> None:
    """两份配方各有一个「裁切」节点：摆正之后、增强之前，工具名 crop。"""
    for name in ("scan-entry", "scan-enhance"):
        current = recipe.load(name)
        tools_used = [step.tool for step in current.steps]
        assert "crop" in tools_used, name
        assert tools_used[0] == "preprocess", name
        assert tools_used.index("crop") == tools_used.index("preprocess") + 1, name
        if "enhance" in tools_used:  # scan-enhance 才有这一步
            assert tools_used.index("crop") < tools_used.index("enhance"), name
            assert tools_used.index("enhance") < tools_used.index("cut"), name


def test_crop_is_the_eighth_tool_and_on_by_default() -> None:
    rows = {row["name"]: row for row in tools.status()}

    assert len(rows) == 8
    assert rows["crop"]["title"] == "裁边"
    assert rows["crop"]["enabled"] is True
    assert rows["crop"]["reason"] is None


def test_tools_page_shows_the_crop_switch(client, monkeypatch) -> None:
    on_page = client.get("/tools")
    assert on_page.status_code == 200
    assert "CAIRN_TOOL_CROP=0" in on_page.text
    assert "把纸从背景里裁出来" in on_page.text
    assert "裁切" in on_page.text  # 当前配方那一节里也有它

    monkeypatch.setenv("CAIRN_TOOL_CROP", "0")
    off_page = client.get("/tools")
    assert off_page.status_code == 200
    assert "裁边已关闭（CAIRN_TOOL_CROP）" in off_page.text


# ---------------------------------------------------------------- AC2：真裁，原图不动


def test_upload_crops_the_paper_into_a_second_file(client, db_path, restore_engine) -> None:
    """AC2：红底那张裁得动——裁好的另存 `<uuid>.crop.png`，上传的原图一个字节不改。"""
    data = HI_REDBG.read_bytes()
    before = image_size(HI_REDBG)
    uploaded_sha = hashlib.sha256(data).hexdigest()

    scan_id = upload(client, HI_REDBG)
    scan, stored = load_scan(db_path, scan_id)
    cropped = crop_file_for(stored)

    # 原图：字节与上传时完全一致（sha256 前后对比）
    assert before == (1098, 2560)
    assert stored.read_bytes() == data
    assert sha256_file(stored) == uploaded_sha
    assert image_size(stored) == before

    # 裁好的那张：同目录、名字多一段 .crop，无损 PNG（我们自己产生的一律无损）
    assert cropped.parent == stored.parent
    assert stored.suffix == ".jpg"
    assert cropped.name == f"{stored.stem}.crop.png"
    assert cropped.is_file()
    assert cropped.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert image_size(cropped) == (2000, 1493)  # 裁到纸（长边 2000）再摆正 90°

    # 读图链路拿到的就是裁好的那张（切题/OCR/展示共用一个口径）
    db_url = f"sqlite:///{db_path}"
    assert scan_service.crop_path_for_scan(scan, db_url) == cropped
    assert scan_service.read_image_path(scan, db_url) == cropped

    # 它确实是"裁到纸"之后的样子：再检测一次就是满幅
    from cairn.ocr.crop import detect_paper
    import cv2

    result = detect_paper(cv2.imread(str(cropped)))
    assert result.area_ratio > 0.85
    assert result.reason == "already_full_page"

    page = client.get(f"/scans/{scan_id}/ocr")
    assert page.status_code == 200
    assert "裁切 完成（纸占 45%，从 1098x2560 裁到 2000x1493）" in page.text


def test_crop_note_is_recorded_next_to_the_image(client, db_path, restore_engine) -> None:
    """结论写进图旁边的 `<uuid>.crop.json`，配方那一步照念，不重算。"""
    scan_id = upload(client, HI_REDBG)
    _, stored = load_scan(db_path, scan_id)

    record_path = stored.with_suffix(scan_service.CROP_RECORD_SUFFIX)
    assert record_path.is_file()
    payload = json.loads(record_path.read_text(encoding="utf-8"))

    assert payload["reason"] == "ok"
    assert payload["note"].startswith("完成（纸占 45%")
    assert payload["area_ratio"] == pytest.approx(0.4453, abs=5e-3)


# ---------------------------------------------------------------- AC3：不裁


def test_blended_background_is_left_alone_and_explained(client, db_path, restore_engine) -> None:
    """AC3：白纸白底那张不裁——不出 crop 文件，原图字节不变，痕迹说「分不开」。"""
    data = USER_PHOTO.read_bytes()
    before = image_size(USER_PHOTO)

    scan_id = upload(client, USER_PHOTO)
    scan, stored = load_scan(db_path, scan_id)

    assert image_size(stored) == before == (2560, 1098)
    assert stored.read_bytes() == data  # 不裁也不转：一个字节不改
    assert not crop_file_for(stored).exists()  # 没有裁好的那份
    assert scan_service.crop_path_for_scan(scan, f"sqlite:///{db_path}") == stored

    page = client.get(f"/scans/{scan_id}/ocr")
    assert page.status_code == 200
    assert "裁切 跳过（纸和背景分不开，请换深色背景或自己框）" in page.text
    assert "裁切 完成" not in page.text


# ---------------------------------------------------------------- AC4：关掉


def test_crop_off_never_crops_and_never_errors(client, db_path, monkeypatch, restore_engine) -> None:
    """AC4：`CAIRN_TOOL_CROP=0` → 不裁、不报错、不出 crop 文件、痕迹说「已关闭」。"""
    monkeypatch.setenv("CAIRN_TOOL_CROP", "0")
    data = HI_REDBG.read_bytes()

    scan_id = upload(client, HI_REDBG)
    _, stored = load_scan(db_path, scan_id)

    assert image_size(stored) == (1098, 2560)  # 连方向归正都不做：原图一个字节不改
    assert stored.read_bytes() == data
    assert not crop_file_for(stored).exists()
    assert records(db_path) == []  # 没裁就没有结论文件

    page = client.get(f"/scans/{scan_id}/ocr")
    assert page.status_code == 200
    assert "裁切 已关闭" in page.text
    assert "裁切 完成" not in page.text


def test_crop_off_keeps_the_rest_of_the_chain_working(client, db_path, monkeypatch, restore_engine) -> None:
    """关掉裁边只是那一步不做，切题/认字照跑（不 500、不掉行）。"""
    monkeypatch.setenv("CAIRN_TOOL_CROP", "0")

    scan_id = upload(client, HI_REDBG)

    assert client.get(f"/scans/{scan_id}").status_code == 200
    assert client.get(f"/scans/{scan_id}/ocr").status_code == 200


# ---------------------------------------------------------------- AC5：看原图


def test_original_entry_serves_the_uploaded_bytes(client, db_path, restore_engine) -> None:
    """AC5：「看原图」拿到的是上传时那张原字节；默认入口发的是裁好的那张。"""
    data = HI_REDBG.read_bytes()

    scan_id = upload(client, HI_REDBG)
    _, stored = load_scan(db_path, scan_id)
    cropped = crop_file_for(stored)

    original = client.get(f"/scans/{scan_id}/image?source=original")
    assert original.status_code == 200
    assert original.headers["x-cairn-source"] == "original"
    assert original.content == data

    # 详情页（原图视图）用的就是这条入口
    detail = client.get(f"/scans/{scan_id}?source=original")
    assert detail.status_code == 200
    assert f'/scans/{scan_id}/image?source=original' in detail.text

    default = client.get(f"/scans/{scan_id}/image")
    assert default.status_code == 200
    assert default.headers["x-cairn-source"] == "crop"
    assert default.content == cropped.read_bytes()


# ---------------------------------------------------------------- AC6/AC3：端到端切题


@needs_engine
def test_cropped_page_still_cuts_eighteen_questions(client, db_path, restore_engine) -> None:
    """AC2 后半：裁好的图跑切题仍是 18 块、题号 1–18（走上传 + 页面，不是裸调 crop.py）。"""
    scan_id = upload(client, HI_REDBG)

    response = client.get(f"/scans/{scan_id}/questions")
    assert response.status_code == 200

    assert question_numbers(db_path, scan_id) == list(range(1, 19))
    assert "共 18 题" in response.text


@needs_engine
def test_uncropped_page_still_cuts_eighteen_questions(client, db_path, restore_engine) -> None:
    """AC3 后半：没裁的那张（没有 crop 文件）照样切出 18 块。"""
    scan_id = upload(client, USER_PHOTO)
    _, stored = load_scan(db_path, scan_id)

    assert not crop_file_for(stored).exists()

    response = client.get(f"/scans/{scan_id}/questions")
    assert response.status_code == 200

    assert question_numbers(db_path, scan_id) == list(range(1, 19))
    assert "共 18 题" in response.text


# ---------------------------------------------------------------- 人话：八句


def test_every_gate_has_one_sentence_from_the_card() -> None:
    """八句文案只从这张表来（一字不改），`not_found` 并进"分不开"那句。"""
    expected = {
        "already_full_page": "跳过（纸已满幅）",
        "cannot_separate_background": "跳过（纸和背景分不开，请换深色背景或自己框）",
        "paper_too_small": "跳过（纸太小，找不到整页）",
        "degenerate_mask": "跳过（检出的不是四角）",
        "paper_cut_by_frame": "跳过（纸被画面截断，请重拍或自己框）",
        "weak_edge": "跳过（纸边不清楚）",
    }
    assert scan_service._CROP_SKIP_NOTES["not_found"] == (
        scan_service._CROP_SKIP_NOTES["cannot_separate_background"]
    )
    for reason, sentence in expected.items():
        assert scan_service._CROP_SKIP_NOTES[reason] == sentence
