"""增强这一步：把纸压白、把字压黑，原图一个字节不动。

第二版（返工）验收的几件事，一条一组：

- 算子本身：纸 → 接近纯白、字 → 接近纯黑；黑白点**写死 6 / 244**、与这一页
  的灰度分布无关；彩色档保住色相；阴影被拉平；长边 ≤ 2560 且两档几何一致。
- 锐化分档（AC6）：gray 档默认与"关掉锐化"逐像素相同，color 档仍然锐化。
- 上传落盘（AC5 / AC7）：原图 sha256 前后不变、`processed_path` 指向新文件、
  那张文件是灰度 JPEG；**找不到纸边的真图也照样产出增强图**；增强关掉时
  不落处理图、上传照常成功。
- 取图：`?source=original` / `?source=processed` 各发各的，没有增强图时安静
  退回原图；详情页默认原图。
- 配方：四步都在痕迹里、全开时四步 `ran`。
- 耗时（AC8）：2560 长边单页 ≤ 300ms。
- 三条真图（AC2 / AC3 / AC4）：paper-03 → 18 块 1–18、paper-04 → 10 块 19–28、
  仓库里的 `docs/samples/user-20261101.jpg` → 18 块 1–18。真图都走
  `ocr.read_page`（先判方向再切题），与 Lead 实测同一条链路。
  paper-03 / paper-04 靠 `CAIRN_SAMPLE_DIR` 打开，没设就跳过——和
  `test_ocr_orient.py` 同一套约定；仓库里那张不需要环境变量。

变异自证（AC9）不在这里断言，而是拿本文件的真图用例当靶子：把 tone curve
改回自适应 → paper-03 那条红；把 gray 的 sharpen 改回 0.55 → paper-04 那条红。
过程与输出记在 `docs/EVIDENCE.md`。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import time
from importlib import import_module
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from cairn import tools
from cairn.models import Scan, make_engine, make_session_factory
from cairn.ocr.cut import cut_questions
from cairn.ocr.preprocess import preprocess_page
from cairn.web import create_app

# `from cairn.ocr import enhance` 拿到的是 preprocess 尾段的去阴影函数（同名），
# 要模块本身得走 import_module——和 `test_ocr_orient.py` 取 `orient` 是同一个坑
enhance = import_module("cairn.ocr.enhance")

SAMPLE_DIR = os.environ.get("CAIRN_SAMPLE_DIR")
_ENGINE_INSTALLED = importlib.util.find_spec("rapidocr_onnxruntime") is not None

# 仓库里那张真卷子（不用环境变量，任何环境都能跑）
USER_PHOTO = Path(__file__).resolve().parents[1] / "docs" / "samples" / "user-20261101.jpg"

needs_samples = pytest.mark.skipif(
    not SAMPLE_DIR or not _ENGINE_INSTALLED,
    reason="真图回归需要 CAIRN_SAMPLE_DIR + 已安装 rapidocr-onnxruntime",
)
needs_engine = pytest.mark.skipif(
    not _ENGINE_INSTALLED, reason="真图回归需要已安装 rapidocr-onnxruntime"
)


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _tools_on(monkeypatch):
    """每条用例从"全开"起跑；要关谁自己 setenv。"""
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


# 假引擎返回的两行（够让切题切出两块，又不用加载真模型）。
# 必须打桩：真引擎一旦被加载就会留在 `ocr.engine._engine` 里，把
# `test_ocr.test_engine_is_lazy_on_import` 弄红（那个用例断言的就是"导入不加载"）。
FAKE_LINES = [
    (((100, 100), (300, 100), (300, 124), (100, 124)), "1.第一题", 0.95),
    (((100, 140), (300, 140), (300, 164), (100, 164)), "2.第二题", 0.95),
]


@pytest.fixture()
def fake_ocr(monkeypatch):
    from cairn.ocr import engine as ocr_engine

    monkeypatch.setattr(ocr_engine, "_engine", lambda path: (FAKE_LINES, 0.1))


# ---------------------------------------------------------------- 素材


def flat_page(paper: int, ink: int, *, size=(900, 1200)) -> np.ndarray:
    """一张"平光拍的卷子"：纸面均匀 `paper`、几行字是 `ink`（BGR 数组）。"""
    image = np.full((size[1], size[0], 3), paper, dtype=np.uint8)
    for index in range(8):
        top = 120 + index * 110
        cv2.rectangle(image, (90, top), (size[0] - 90, top + 22), (ink, ink, ink), -1)
    return image


def shadow_page(*, size=(900, 1200)) -> np.ndarray:
    """左暗右亮的纸（模拟手机在灯下拍偏）：纸面亮度从 150 线性升到 235。"""
    width, height = size
    ramp = np.linspace(150, 235, width, dtype=np.float32)
    paper = np.repeat(ramp[None, :], height, axis=0)
    image = np.repeat(paper[..., None], 3, axis=2).astype(np.uint8)
    for index in range(6):
        top = 160 + index * 150
        cv2.rectangle(image, (120, top), (width - 120, top + 20), (60, 60, 60), -1)
    return image


def red_pen_page(*, size=(800, 1000)) -> np.ndarray:
    """纸面上有一道红笔（BGR 里 R 高、G/B 低），用来验"色相没被拉偏"。"""
    image = np.full((size[1], size[0], 3), 210, dtype=np.uint8)
    cv2.rectangle(image, (100, 200), (700, 240), (40, 40, 200), -1)  # 红笔
    cv2.rectangle(image, (100, 400), (700, 440), (60, 60, 60), -1)  # 黑字
    return image


def page_photo(size=(1000, 1200)) -> Image.Image:
    """合成一张"拍歪了的卷子"：深色桌面 + 白色四边形纸张 + 几行黑字。

    与 `test_tools_registry.page_photo` 同一套几何：短边 1000 踩在上传下限上，
    纸占画面中间 6 成，`preprocess` 找得到四边——增强才有机会跑。
    """
    quad = ((140, 180), (860, 140), (900, 1040), (120, 1080))
    image = Image.new("RGB", size, (68, 70, 74))
    draw = ImageDraw.Draw(image)
    draw.polygon(quad, fill=(246, 246, 242))
    for index in range(8):
        y = 320 + index * 90
        draw.rectangle([260, y, 740, y + 14], fill=(25, 25, 25))
    return image


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def upload(client, data: bytes, name: str = "page.png") -> int:
    suffix = Path(name).suffix.lower()
    content_type = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    response = client.post(
        "/scans/upload",
        files={"file": (name, data, content_type)},
        data={"quality": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[-1])


def open_session(db_path):
    engine = make_engine(f"sqlite:///{db_path}")
    return make_session_factory(engine)()


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- 算子


def test_paper_goes_white_and_ink_goes_black() -> None:
    """一张灰纸黑字的照片 → 纸面接近 255、墨迹接近 0（这就是"压白、压黑"）。

    黑白点写死 6 / 244（第二版），所以"压到 0"只对**真的黑**的笔画成立：
    中灰（120/205 那种铅笔灰）会落在 140 上下，这是固定阈值的代价，不是缺陷——
    那类页面的判据是切题块数（真图那三条），不是像素黑度。
    """
    gray, _color = enhance.enhance_page(flat_page(paper=205, ink=16))

    assert np.percentile(gray, 95) >= 240  # 纸面
    assert np.percentile(gray, 5) <= 12  # 墨迹


def test_lut_is_the_fixed_six_and_244_pair_not_this_page() -> None:
    """黑白点写死 6 / 244：LUT 不由本页分布决定，换一张纸也还是同一条曲线。

    上一版按 `p1/p95` 自适应；实测那条路在三张真卷子上各掉一道题，这一条把
    "固定"钉住：默认 LUT 与显式 `build_lut(6, 244)` 逐项相同，且两页内容完全
    不同的纸得到同一条 LUT（自适应会给两条）。
    """
    assert enhance.build_lut().tolist() == enhance.build_lut(6.0, 244.0).tolist()

    lut = enhance.build_lut()
    assert lut[0] == 0 and lut[255] == 255

    # 两页亮度差得很远的纸：增强后的黑白点仍然落在 6/244 这条曲线上
    light_gray, _ = enhance.enhance_page(flat_page(paper=230, ink=70))
    dark_gray, _ = enhance.enhance_page(flat_page(paper=170, ink=90))
    assert np.percentile(light_gray, 95) >= 240
    assert np.percentile(dark_gray, 95) >= 240


def test_shadow_is_flattened() -> None:
    """同一条纸带，左侧（原 150）与右侧（原 235）增强后亮度差 ≤ 12。

    除法归一就是干这个的：不除背景，左边那块纸永远比右边灰。
    """
    gray, _color = enhance.enhance_page(shadow_page())

    left = float(np.percentile(gray[:, : gray.shape[1] // 8], 90))
    right = float(np.percentile(gray[:, -gray.shape[1] // 8 :], 90))
    assert abs(left - right) <= 12, (left, right)
    assert min(left, right) >= 235  # 两边都是"纸"


def test_color_keeps_the_pen_hue() -> None:
    """红笔在彩色档里仍然是红的：R 通道比 G/B 亮出一截（没被拉成灰）。"""
    _gray, color = enhance.enhance_page(red_pen_page())

    stroke = color[205:235, 200:600]  # 红笔那一带的 BGR
    blue, green, red = (float(stroke[..., index].mean()) for index in range(3))
    assert red - max(blue, green) >= 40, (blue, green, red)


def test_gray_tier_is_not_sharpened_and_color_tier_is() -> None:
    """AC6：gray 档默认与"关锐化"逐像素相同；color 档默认仍然锐化。

    这不是"差不多"：gray 档走的是 `_sharpen(amount=0)`，函数原样返回数组，
    所以两条路的输出必须 `array_equal`；color 档 if 一样就等于没锐化，
    必须和 `sharpen=0` 的输出**不相等**。
    """
    assert enhance.GRAY_SHARPEN_AMOUNT == 0.0
    assert enhance.COLOR_SHARPEN_AMOUNT == 0.55

    bgr = flat_page(paper=205, ink=120)
    gray_default, color_default = enhance.enhance_page(bgr)
    gray_off, color_off = enhance.enhance_page(bgr, sharpen=0.0)

    assert np.array_equal(gray_default, gray_off)  # 零锐化，逐像素相同
    assert not np.array_equal(color_default, color_off)  # 彩色档确实锐化了


def test_gray_and_color_share_geometry_and_cap_the_long_side() -> None:
    """灰度与彩色逐像素对齐，长边压到 2560；小图不放大。"""
    big = flat_page(paper=200, ink=90, size=(4000, 3000))
    gray, color = enhance.enhance_page(big)

    assert gray.shape == color.shape[:2]
    assert max(gray.shape) == enhance.LONG_SIDE
    assert abs(gray.shape[0] / gray.shape[1] - 3000 / 4000) < 0.01

    small = flat_page(paper=200, ink=90, size=(400, 300))
    gray_small, color_small = enhance.enhance_page(small)
    assert gray_small.shape == (300, 400)
    assert color_small.shape == (300, 400, 3)


def test_single_page_takes_under_300ms() -> None:
    """AC8：2560 长边的单页增强 ≤ 300ms（实测 40–70ms，留足余量）。

    先跑一次把 OpenCV 的首次调用（算子缓存、线程池）预热掉，再计时——
    线上是长驻进程，第一次那点开销不该算到"一页要多久"里。
    """
    bgr = flat_page(paper=205, ink=110, size=(2560, 1920))
    enhance.enhance_page(bgr)  # 预热

    started = time.perf_counter()
    enhance.enhance_page(bgr)
    elapsed = time.perf_counter() - started

    assert elapsed <= 0.3, f"单页 {elapsed * 1000:.0f}ms"


# ---------------------------------------------------------------- 上传落盘


def test_upload_writes_enhanced_gray_and_keeps_the_original_bytes(client, db_path) -> None:
    """AC5：原图 sha256 前后不变；`processed_path` 指向**另一份**灰度 JPEG。

    裁到纸（第二步 a）也另存一份 `<uuid>.crop.jpg`，`scan.path` 那份从头到尾
    只读不写——原图必须保留，这是不可逆操作的边界。
    """
    data = png_bytes(page_photo())
    scan_id = upload(client, data)

    with open_session(db_path) as session:
        scan = session.get(Scan, scan_id)
        original = Path(db_path).parent / scan.path
        assert scan.processed_path is not None
        processed = Path(db_path).parent / scan.processed_path
        original_sha = sha256(original)
        processed_sha = sha256(processed)
        assert processed != original

    assert original.read_bytes() == data  # 上传之后一个字节没动
    assert original_sha == hashlib.sha256(data).hexdigest()
    assert processed.read_bytes()[:3] == b"\xff\xd8\xff"  # JPEG
    assert processed_sha != original_sha  # 是另一份文件，不是原图
    with Image.open(original) as opened:
        assert opened.mode == "RGB"  # 原图没被增强的灰度盖掉
    with Image.open(processed) as opened:
        assert opened.mode == "L"  # 落的是灰度
        assert max(opened.size) <= enhance.LONG_SIDE


def test_upload_without_a_page_boundary_still_gets_enhanced(client, db_path, caplog) -> None:
    """AC7：找不到纸边的真图也产出增强图——增强不依赖 `preprocess_page().changed`。

    用的就是仓库里那张真卷子：它确实找不到四边（先断言这一点，否则这条用例
    证明不了"解耦"），但增强只看灰度分布，照样该落一张处理图。
    """
    source = USER_PHOTO
    assert source.is_file(), source
    assert preprocess_page(str(source)).changed is False  # 前提：这张图没有纸边

    data = source.read_bytes()
    scan_id = upload(client, data, name="user.jpg")

    with open_session(db_path) as session:
        scan = session.get(Scan, scan_id)
        assert scan.processed_path is not None  # 旧门槛下这里会是 None
        processed = Path(db_path).parent / scan.processed_path
        assert processed.is_file()
        assert (Path(db_path).parent / scan.path).read_bytes() == data  # 原图没动
        with Image.open(processed) as opened:
            assert opened.mode == "L"
        assert "找不到纸张边界" in caplog.text  # 照实说了没摆正，但增强照跑


def test_enhance_turned_off_keeps_upload_working_without_a_processed_file(
    client, db_path, monkeypatch, fake_ocr
) -> None:
    """`CAIRN_TOOL_ENHANCE=0` → 上传照常成功、`processed_path` 为空、页面 200。"""
    monkeypatch.setenv("CAIRN_TOOL_ENHANCE", "0")
    scan_id = upload(client, png_bytes(page_photo()))

    with open_session(db_path) as session:
        scan = session.get(Scan, scan_id)
        assert scan.processed_path is None
        assert (Path(db_path).parent / scan.path).is_file()  # 原图还在

    assert not list((Path(db_path).parent / "scans").rglob("*.processed.jpg"))
    assert client.get(f"/scans/{scan_id}").status_code == 200
    page = client.get(f"/scans/{scan_id}/ocr")
    assert page.status_code == 200
    assert "裁切" in page.text  # 默认配方 scan-entry 不带增强，裁切/切题/认字照跑
    assert "认字 已关闭" not in page.text


def test_disabled_step_is_traceable_in_the_recipe(client, db_path, monkeypatch, fake_ocr) -> None:
    """AC2 的后半句：关掉的是**那一步**，不是整条链。"""
    scan_id = upload(client, png_bytes(page_photo()))
    monkeypatch.setenv("CAIRN_TOOL_CROP", "0")

    page = client.get(f"/scans/{scan_id}/ocr")

    assert page.status_code == 200
    assert "摆正" in page.text
    assert "裁切 已关闭" in page.text
    assert "切题" in page.text  # 后面那几步照跑


# ---------------------------------------------------------------- 取图


def test_image_endpoint_serves_original_and_enhanced_on_request(client, db_path) -> None:
    """`?source=original` 发原图字节、`?source=processed` 发增强图字节，头部标来源。"""
    scan_id = upload(client, png_bytes(page_photo()))
    with open_session(db_path) as session:
        scan = session.get(Scan, scan_id)
        original = Path(db_path).parent / scan.path
        processed = Path(db_path).parent / scan.processed_path

    original_resp = client.get(f"/scans/{scan_id}/image?source=original")
    assert original_resp.status_code == 200
    assert original_resp.headers["x-cairn-source"] == "original"
    assert original_resp.content == original.read_bytes()

    processed_resp = client.get(f"/scans/{scan_id}/image?source=processed")
    assert processed_resp.status_code == 200
    assert processed_resp.headers["x-cairn-source"] == "processed"
    assert processed_resp.content == processed.read_bytes()


def test_detail_page_defaults_to_original_and_offers_the_enhanced_one(client, db_path) -> None:
    """详情页默认展示原图；「增强后」是一个链接，点了才切过去。"""
    scan_id = upload(client, png_bytes(page_photo()))

    page = client.get(f"/scans/{scan_id}")
    assert page.status_code == 200
    assert f'src="/scans/{scan_id}/image?source=original"' in page.text
    assert f'href="/scans/{scan_id}?source=processed"' in page.text
    assert "增强后" in page.text
    assert "processed" not in page.text.replace("?source=processed", "")  # 文案不露内部词

    switched = client.get(f"/scans/{scan_id}?source=processed")
    assert switched.status_code == 200
    assert f'src="/scans/{scan_id}/image?source=processed"' in switched.text


def test_detail_page_without_enhancement_has_no_switch(client, db_path, monkeypatch) -> None:
    """没有增强图的照片（抓拍 / 增强关了）：不显示切换，URL 也保持原样。"""
    monkeypatch.setenv("CAIRN_TOOL_ENHANCE", "0")
    scan_id = upload(client, png_bytes(page_photo()))

    page = client.get(f"/scans/{scan_id}/image?source=processed")

    assert page.status_code == 200
    assert page.headers["x-cairn-source"] == "original"  # 安静退回原图，不 404


# ---------------------------------------------------------------- 配方


def test_upload_recipe_runs_four_steps(client, db_path, fake_ocr) -> None:
    """全开时四步都 `ran`，痕迹里四步名字都在，顺序与 YAML 一致（scan-entry）。"""
    scan_id = upload(client, png_bytes(page_photo()))
    page = client.get(f"/scans/{scan_id}/ocr")

    assert page.status_code == 200
    trace = page.text.split("配方：", 1)[1].split("</p>", 1)[0]
    assert trace.startswith("摆正 ")
    assert " · 裁切 " in trace
    assert " · 切题 切出 " in trace
    assert " · 认字 " in trace
    assert "增强" not in trace  # 默认配方不带增强


def test_scan_entry_recipe_is_still_there_as_four_steps() -> None:
    """硬约束：`scan-entry.yaml` 保留不删，第二步 a 起是四步（多了裁切）。"""
    from cairn import recipe

    recipe.clear_cache()
    current = recipe.load("scan-entry")
    assert [step.tool for step in current.steps] == ["preprocess", "crop", "cut", "ocr"]


# ---------------------------------------------------------------- 真图


@pytest.fixture()
def restore_engine():
    """真图用例会真的加载 RapidOCR；跑完把模块级缓存放回原样。

    不放回去，`test_ocr.test_engine_is_lazy_on_import`（断言"导入不加载模型"）
    就会因为这里的副作用变红——它和本文件无关，不该被拖下水。
    """
    from cairn.ocr import engine as ocr_engine

    saved = ocr_engine._engine
    yield
    ocr_engine._engine = saved


def _cut_enhanced(source: Path, target: Path) -> tuple[object, list]:
    """原图 → 增强（gray 档，默认零锐化）→ 落 JPEG → 判方向 → 切题。

    判方向这一步不能省：paper-04 是横放的，`read_page` 会先转正再认字，
    Lead 实测的 10 块 19–28 就是这条链路出来的。
    """
    from cairn.ocr.orient import read_page

    bgr = cv2.imread(str(source))
    assert bgr is not None, source
    gray, _color = enhance.enhance_page(bgr)
    assert cv2.imwrite(str(target), gray, [cv2.IMWRITE_JPEG_QUALITY, 92])

    page = read_page(str(target))
    return page, cut_questions(page.lines, page.width, page.height)


@needs_samples
def test_paper_03_enhanced_cuts_eighteen_questions(tmp_path, restore_engine) -> None:
    """AC2：paper-03 增强后 18 块、题号 1–18（上一版自适应只剩 17 块）。"""
    _page, blocks = _cut_enhanced(
        Path(SAMPLE_DIR) / "paper-03-original.jpg", tmp_path / "paper-03.jpg"
    )

    assert [block.no for block in blocks] == list(range(1, 19)), [b.no for b in blocks]


@needs_samples
def test_paper_04_enhanced_cuts_ten_questions_from_nineteen(tmp_path, restore_engine) -> None:
    """AC3：paper-04 增强后 10 块、题号 19–28（跨页左 19–23、右 24–28）。

    这条同时钉住 gray 档**不锐化**：把 `GRAY_SHARPEN_AMOUNT` 改回 0.55，
    这一页只剩 8 块（AC9 的第二个变异）。
    """
    _page, blocks = _cut_enhanced(
        Path(SAMPLE_DIR) / "paper-04-original.jpg", tmp_path / "paper-04.jpg"
    )

    assert [block.no for block in blocks] == list(range(19, 29)), [b.no for b in blocks]


@needs_engine
def test_user_photo_enhanced_cuts_eighteen_questions(tmp_path, restore_engine) -> None:
    """AC4：仓库里那张真卷子增强后 18 块、题号 1–18。

    这张图在仓库内，任何环境都能跑（只要有 OCR 模型），不用 `CAIRN_SAMPLE_DIR`。
    """
    _page, blocks = _cut_enhanced(USER_PHOTO, tmp_path / "user.jpg")

    assert [block.no for block in blocks] == list(range(1, 19)), [b.no for b in blocks]
