"""整页方向判定卡：四角度合成用例、不重采样、探针/缓存、CLI、web、deskew 顺序。

单元用例全部用**合成图 + 会读像素的假引擎**，不依赖真模型与仓库外真图：
假引擎按墨迹几何出框、按"行框是否横排 + 行首记号在哪端"给置信度，方向判定
必须靠这两条把 0/90/180/270 分开。真图（paper-03 / paper-04）回归放在本文件
末尾，靠 `CAIRN_SAMPLE_DIR` 打开，未设置时跳过（真机实测数据见
docs/EVIDENCE.md）。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from cairn.ocr import __main__ as ocr_main
from cairn.ocr.cut import cut_questions
from cairn.ocr.deskew import deskew_image
from cairn.ocr.engine import OcrError, OcrLine
from cairn.ocr.orient import OrientedPage, detect_orientation, orient, read_page, text_score
from cairn.web import create_app

PAGE_W, PAGE_H = 1600, 1200

# 注意：`from cairn.ocr import orient` 拿到的是函数（包 __init__ 已导出），
# 要模块本身得走 import_module
orient_mod = importlib.import_module("cairn.ocr.orient")


# ---------- 合成"试卷" + 会读像素的假引擎 ----------


def draw_page(path: Path, width: int = 480, height: int = 420, lines: int = 6) -> Path:
    """画一张正向"试卷"：每行一条墨迹带，行首另有一个小方块（方向记号）。

    行首记号是关键：它在图像里位于行的**左**端，倒置后跑到右端——相当于给
    每一行做了个方向标记，假引擎靠它判断"这一版读得顺不顺"。
    """
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    step = (height - 40) // lines
    for index in range(lines):
        top = 20 + index * step
        draw.rectangle([40, top, 52, top + 40], fill=0)  # 行首记号（窄竖条）
        draw.rectangle([72, top, width - 40, top + 40], fill=0)  # 一行"文字"
    image.save(path)
    return path


def rotate_copy(source: Path, angle: int, target: Path) -> Path:
    with Image.open(source) as image:
        image.rotate(angle, expand=True, fillcolor=255).save(target)
    return target


def _profile(image: Image.Image, axis: str) -> list[int]:
    """投影：沿 axis 压成 1 像素宽/高，得到每行（或每列）的平均亮度（纯 C 速度）。"""
    if axis == "row":
        squeezed = image.resize((1, image.height), Image.Resampling.BOX)
    else:
        squeezed = image.resize((image.width, 1), Image.Resampling.BOX)
    return list(squeezed.tobytes())  # L 模式：一字节一个像素值


def _bands(profile: list[int], ink: int = 200) -> list[tuple[int, int]]:
    """亮度低于阈值的连续区间 = 一段墨迹。"""
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(profile):
        if value < ink:
            if start is None:
                start = index
        elif start is not None:
            bands.append((start, index))
            start = None
    if start is not None:
        bands.append((start, len(profile)))
    return bands


def _head_at_left(cell: Image.Image) -> bool:
    """行首记号在左端？记号是"短墨迹 + 明显空隙"，倒在右端则相反。"""
    runs = _bands(_profile(cell, "col"))
    if len(runs) < 2:
        return False
    first_start, first_end = runs[0]
    second_start, _ = runs[1]
    return (first_end - first_start) <= 30 and (second_start - first_end) >= 6


def _cells(gray: Image.Image) -> list[tuple[int, int, int, int, bool, bool]]:
    """切成墨迹格：正常行带 = 一行；占了大半页高的"块" = 竖排文字，按列再切。"""
    cells = []
    for y0, y1 in _bands(_profile(gray, "row")):
        if y1 - y0 <= gray.height * 0.25:
            runs = _bands(_profile(gray.crop((0, y0, gray.width, y1)), "col"))
            cells.append((runs[0][0], y0, runs[-1][1], y1))
        else:  # 整页一条带：文字是竖着排的（躺倒页），按列切出每一"行"
            for x0, x1 in _bands(_profile(gray, "col")):
                runs = _bands(_profile(gray.crop((x0, 0, x1, gray.height)), "row"))
                cells.append((x0, runs[0][0], x1, runs[-1][1]))
    result = []
    for x0, y0, x1, y1 in cells:
        cell = gray.crop((x0, y0, x1, y1))
        horizontal = cell.width >= 2 * cell.height
        result.append((x0, y0, x1, y1, horizontal, horizontal and _head_at_left(cell)))
    return result


class PixelPageEngine:
    """假引擎：真读像素出框。

    真引擎四个角度都能读出一片中文（它按框角度转正裁剪图、再用 cls 修 180°），
    方向差异只剩"检测器吐得全不全"。这里用一个更直接的等价模型：只有"行框
    横排且行首记号在左端"的那一版算读顺（0.95），其余给 0.3。
    """

    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    def __call__(self, path):
        with Image.open(path) as image:
            self.sizes.append(image.size)
            cells = _cells(image.convert("L"))
        result = [
            (
                ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
                f"{index + 1}.第{index + 1}题内容",
                0.95 if horizontal and head_left else 0.3,
            )
            for index, (x0, y0, x1, y1, horizontal, head_left) in enumerate(cells)
        ]
        return result, 0.01


@pytest.fixture(autouse=True)
def _clear_direction_cache():
    """判定缓存是进程级的：每个用例前后清干净，免得串味。"""
    orient_mod._CACHE.clear()
    yield
    orient_mod._CACHE.clear()


@pytest.fixture()
def pixel_engine(monkeypatch):
    from cairn.ocr import engine as ocr_engine

    engine = PixelPageEngine()
    monkeypatch.setattr(ocr_engine, "_engine", engine)
    return engine


# ---------- AC2：四个角度都能判回来 ----------


@pytest.mark.parametrize("rotated", [0, 90, 180, 270])
def test_synthetic_page_orientation_is_recovered(rotated, tmp_path, pixel_engine):
    source = draw_page(tmp_path / "page.png")
    target = source if rotated == 0 else rotate_copy(source, rotated, tmp_path / f"rot{rotated}.png")
    angle, score = detect_orientation(str(target))
    # 图被逆时针转了 rotated 度 → 要摆正还得再逆时针转 360-rotated 度
    assert angle == (360 - rotated) % 360
    assert score > 0


def test_upright_page_reports_zero_and_keeps_score(tmp_path, pixel_engine):
    path = draw_page(tmp_path / "page.png")
    angle, score = detect_orientation(str(path))
    assert angle == 0
    assert score > 0


def test_sideways_page_is_rotated_counter_clockwise(tmp_path, pixel_engine):
    # 原图逆时针 270°（= 顺时针 90°）→ 需要逆时针 90° 摆正
    source = draw_page(tmp_path / "page.png")
    target = rotate_copy(source, 270, tmp_path / "side.png")
    image, angle = orient(str(target))
    assert angle == 90
    assert image.size == (480, 420)  # 摆正后回到正向尺寸
    with Image.open(target) as raw:
        assert image.tobytes() == raw.rotate(90, expand=True).tobytes()  # 90° 倍数为无损转置


# ---------- AC4：angle=0 不重采样 ----------


def test_angle_zero_returns_pixels_untouched(tmp_path, pixel_engine):
    path = draw_page(tmp_path / "page.png")
    image, angle = orient(str(path))
    assert angle == 0
    with Image.open(path) as original:
        assert image.size == original.size
        assert image.tobytes() == original.tobytes()  # 逐字节相等：没有插值、没有重编码


def test_angle_zero_keeps_original_file_size(tmp_path, pixel_engine):
    # 重采样会改变像素统计（哪怕是 0.0001 的插值）；这里断言像素直方图一致
    path = draw_page(tmp_path / "page.png")
    image, _ = orient(str(path))
    with Image.open(path) as original:
        assert image.convert("L").histogram() == original.convert("L").histogram()


# ---------- AC5：低分辨率预判 + 缓存 ----------


def test_probe_is_downscaled_and_only_two_full_passes(tmp_path, pixel_engine):
    path = draw_page(tmp_path / "big.png", width=1200, height=900, lines=6)
    detect_orientation(str(path))
    sizes = pixel_engine.sizes
    assert max(sizes[0]) <= orient_mod.PROBE_LONG_SIDE  # 先是低分辨率探针
    full = [size for size in sizes if size == (1200, 900)]
    assert len(full) == 2  # 只对候选轴上的两个方向做完整 OCR（不是四角度全量）
    assert len(sizes) == 3


def test_detection_runs_once_for_detect_orient_and_read_page(tmp_path, pixel_engine):
    path = draw_page(tmp_path / "page.png")
    detect_orientation(str(path))
    first = len(pixel_engine.sizes)
    orient(str(path))
    read_page(str(path))
    assert len(pixel_engine.sizes) == first  # 三步共用一次判定


def test_cache_follows_file_change(tmp_path, pixel_engine):
    path = draw_page(tmp_path / "page.png")
    assert detect_orientation(str(path))[0] == 0
    rotate_copy(path, 180, path)
    assert detect_orientation(str(path))[0] == 180


def test_blank_page_is_not_rotated(tmp_path, pixel_engine):
    path = tmp_path / "blank.png"
    Image.new("L", (600, 400), 255).save(path)
    angle, score = detect_orientation(str(path))
    assert (angle, score) == (0, 0.0)


# ---------- 打分公式 ----------


def _line(text: str, confidence: float, width: int = 200, height: int = 24) -> OcrLine:
    box = ((0, 0), (width, 0), (width, height), (0, height))
    return OcrLine(text=text, box=box, confidence=confidence, y_center=height // 2)


def test_text_score_is_lines_times_confidence_times_readable_ratio():
    assert text_score([]) == 0.0
    lines = [_line("中文一行", 1.0), _line("另一行", 1.0)]
    assert text_score(lines) == pytest.approx(2 * 1.0 * 1.0)
    noisy = [_line("中文一行", 1.0), _line("§§", 1.0)]
    assert text_score(noisy) == pytest.approx(2 * 1.0 * (4 / 6))  # "§" 不算有效字符


def test_text_score_prefers_more_lines_at_same_confidence():
    few = [_line("中文", 0.9)] * 3
    many = [_line("中文", 0.9)] * 7
    assert text_score(many) > text_score(few)


# ---------- 错误路径 ----------


def test_orient_raises_when_disabled(tmp_path, pixel_engine, monkeypatch):
    path = draw_page(tmp_path / "page.png")
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    with pytest.raises(OcrError, match="CAIRN_OCR_DISABLE"):
        orient(str(path))


def test_orient_raises_for_missing_file(tmp_path):
    with pytest.raises(OcrError, match="图片不存在"):
        detect_orientation(str(tmp_path / "no-such.png"))


def _blind_engine(monkeypatch):
    """不读文件的假引擎（模拟"引擎认得、但 PIL 解不开"的非常规格式）。"""
    from cairn.ocr import engine as ocr_engine

    result = [(((0, 0), (100, 0), (100, 20), (0, 20)), "1.一眼认得", 0.9)]
    monkeypatch.setattr(ocr_engine, "_engine", lambda path: (result, 0.01))


def test_unreadable_image_defers_to_recognizer(tmp_path, monkeypatch):
    # 解不开的图（损坏/非常规格式）不猜方向、不硬报错：原样交给识别器
    _blind_engine(monkeypatch)
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")
    angle, score = detect_orientation(str(path))
    assert angle == 0
    assert score > 0


def test_read_page_reports_zero_size_when_image_unreadable(tmp_path, monkeypatch):
    _blind_engine(monkeypatch)
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")
    page = read_page(str(path))
    assert page.angle == 0
    assert (page.width, page.height) == (0, 0)
    assert page.lines[0].text == "1.一眼认得"


# ---------- AC7：CLI ----------


def test_cli_orient_prints_angle_score_and_question_count(tmp_path, pixel_engine, capsys):
    path = draw_page(tmp_path / "page.png")
    assert ocr_main.main(["orient", str(path)]) == 0
    out = capsys.readouterr().out
    assert "判定方向：0°" in out
    assert "无需旋转" in out
    assert "得分" in out
    assert "共 6 题" in out
    assert "题号 [1, 2, 3, 4, 5, 6]" in out


def test_cli_orient_reports_rotation_for_sideways_page(tmp_path, pixel_engine, capsys):
    source = draw_page(tmp_path / "page.png")
    target = rotate_copy(source, 270, tmp_path / "side.png")
    assert ocr_main.main(["orient", str(target)]) == 0
    out = capsys.readouterr().out
    assert "判定方向：90°" in out
    assert "已逆时针旋转 90°" in out
    assert "共 6 题" in out


def test_cli_orient_raises_ocr_error_when_disabled(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    path = tmp_path / "any.png"
    path.write_bytes(b"fake")
    assert ocr_main.main(["orient", str(path)]) == 1
    assert "CAIRN_OCR_DISABLE" in capsys.readouterr().err


# ---------- AC6：先 orient 再 deskew（两者不冲突） ----------


def _tilted_lines(dy: int = 9, width: int = 200) -> list[OcrLine]:
    box = ((100, 100), (100 + width, 100 + dy), (100 + width, 124 + dy), (100, 124))
    return [OcrLine(text="1.歪的", box=box, confidence=0.9, y_center=112 + dy // 2)]


def _flat_lines() -> list[OcrLine]:
    box = ((100, 100), (300, 100), (300, 124), (100, 124))
    return [OcrLine(text="1.平的", box=box, confidence=0.9, y_center=112)]


def test_deskew_image_small_angle_returns_same_object():
    image = Image.new("RGB", (100, 100), "white")
    out, angle = deskew_image(image, _flat_lines())
    assert out is image  # 不重采样
    assert abs(angle) < 0.5


def test_deskew_image_reuses_lines_without_another_ocr(monkeypatch):
    deskew_mod = importlib.import_module("cairn.ocr.deskew")
    monkeypatch.setattr(deskew_mod, "recognize", lambda path: pytest.fail("不该再 OCR 一遍"))
    image = Image.new("RGB", (400, 300), "white")
    out, angle = deskew_image(image, _tilted_lines())
    assert angle == pytest.approx(2.576, abs=1e-2)
    assert out.size != image.size  # expand=True


def test_orient_then_deskew_only_handles_the_small_angle(tmp_path, pixel_engine):
    # 侧躺页：orient 只负责 90°，剩下的小角度才轮到 deskew——两者不叠加、不打架
    source = draw_page(tmp_path / "page.png")
    target = rotate_copy(source, 270, tmp_path / "side.png")
    upright, big = orient(str(target))
    assert big == 90
    tilted, small = deskew_image(upright.convert("RGB"), _tilted_lines())
    assert abs(small) < 5  # 只剩小角度，没有把 90° 再算一遍
    assert tilted.size != upright.size


# ---------- AC8：展示页方向提示 ----------


def _make_scan(tmp_path: Path) -> tuple[object, int, Path]:
    from cairn.models import Scan, make_engine, make_session_factory
    from cairn.models.learner import utcnow

    db_path = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db_path}")
    (tmp_path / "scans").mkdir()
    Image.new("RGB", (PAGE_W, PAGE_H), "white").save(tmp_path / "scans" / "fake.png")
    with TestClient(app) as client:
        engine = make_engine(f"sqlite:///{db_path}")
        factory = make_session_factory(engine)
        with factory() as session:
            scan = Scan(
                learner_id=client.app.state.learner_id,
                device_id="upload",
                device_name="测试",
                width=PAGE_W,
                height=PAGE_H,
                captured_at=utcnow(),
                path="scans/fake.png",
                source="upload",
            )
            session.add(scan)
            session.commit()
            scan_id = scan.id
    return app, scan_id, tmp_path / "scans" / "fake.png"


def test_scan_ocr_page_shows_detected_direction(tmp_path, monkeypatch):
    from cairn.ocr import engine as ocr_engine

    app, scan_id, image_path = _make_scan(tmp_path)
    draw_page(image_path, width=PAGE_W, height=PAGE_H, lines=3)
    monkeypatch.setattr(ocr_engine, "_engine", PixelPageEngine())
    with TestClient(app) as client:
        resp = client.get(f"/scans/{scan_id}/ocr")
    assert resp.status_code == 200
    assert "照片方向没问题，不用转" in resp.text
    assert "读出了 3 道题" in resp.text


def test_scan_ocr_page_shows_auto_rotation_hint(tmp_path, monkeypatch):
    from cairn import ocr as ocr_pkg

    app, scan_id, _ = _make_scan(tmp_path)
    lines = [_line("1.第一题", 0.95, width=900)]
    page = OrientedPage(image=None, angle=90, score=82.9, lines=lines)
    monkeypatch.setattr(ocr_pkg, "read_page", lambda path: page)
    with TestClient(app) as client:
        resp = client.get(f"/scans/{scan_id}/ocr")
    assert resp.status_code == 200
    assert "照片歪了 90°" in resp.text
    assert "已经帮你转正了" in resp.text


# ---------- 真图回归（需 CAIRN_SAMPLE_DIR 指向样例目录） ----------

SAMPLE_DIR = os.environ.get("CAIRN_SAMPLE_DIR")
_ENGINE_INSTALLED = importlib.util.find_spec("rapidocr_onnxruntime") is not None
real_page = pytest.mark.skipif(
    not SAMPLE_DIR or not _ENGINE_INSTALLED,
    reason="真图回归需要 CAIRN_SAMPLE_DIR + 已安装 rapidocr-onnxruntime",
)


def _sample(name: str) -> str:
    return str(Path(SAMPLE_DIR or ".") / f"{name}-original.jpg")


def _cut_of(name: str):
    page = read_page(_sample(name))
    return cut_questions(page.lines, page.width, page.height)


@real_page
def test_real_upright_page_keeps_angle_zero_and_cuts_18():
    angle, score = detect_orientation(_sample("paper-03"))
    assert (angle, score > 0) == (0, True)
    blocks = _cut_of("paper-03")
    assert [block.no for block in blocks] == list(range(1, 19))


@real_page
def test_real_lying_page_needs_counter_clockwise_90():
    angle, _ = detect_orientation(_sample("paper-04"))
    assert angle == 90
    blocks = _cut_of("paper-04")
    # 7 题、首题 24 是 Lead 手工摆正后的实测结果。这张图是两页跨页（左页 19~23、
    # 右页 24~28），整页切题必然混号，所以只锚"7 题 + 从 24 起"，不锚连续性。
    assert len(blocks) == 7
    assert blocks[0].no == 24
