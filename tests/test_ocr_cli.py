"""`python -m cairn.ocr cut` 的自动摆正回归。

拍歪的卷子走原图识别只会切出 0 题（题号行被读成竖条），产品路径
`read_page` + `cut_questions` 却是 10 题。这里用合成图锁住"CLI 也走摆正路径"：

- 合成 4 行带题号的文字 + 顶部实心块，整页顺时针转 90°（需逆时针 90° 摆正）；
- 假引擎靠顶栏朝向反推角度、靠"横排 + 行首记号在左"决定读不读得出题号，
  躺倒/倒置一律读不出（等价于真引擎的方向差异）；
- 不碰真图路径（真图验证在 docs/EVIDENCE.md）。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from cairn.ocr import __main__ as ocr_main
from cairn.ocr import engine as ocr_engine
from cairn.ocr.engine import OcrLine
from cairn.ocr.orient import OrientedPage

orient_mod = importlib.import_module("cairn.ocr.orient")

WIDTH, HEIGHT, QUESTIONS = 520, 440, 4
# 顶栏（整页最大的实心块）：假引擎靠它的朝向反推"需要逆时针转多少度"
TITLE = (40, 20, 380, 64)
# 顶栏在投影里是大面积着墨（约 0.65），文字行只有 0.1 上下
_DENSE = 60


@pytest.fixture(autouse=True)
def _clear_direction_cache():
    """方向判定缓存是进程级的：每个用例前后清干净，免得串味。"""
    orient_mod._CACHE.clear()
    yield
    orient_mod._CACHE.clear()


# ---------- 合成"试卷" + 只认几何的假引擎 ----------


def draw_page(path: Path) -> Path:
    """画一页"试卷"：顶部实心块 + 4 行带题号的文字（每行行首另有短记号）。

    文字本身假引擎读不出来——它只认"行框横排 + 行首记号在左"，对应真引擎
    "方向对时才读得全"的差异。顶栏和记号都跟着整页一起转，假引擎据此判断朝向。
    """
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    draw.rectangle(TITLE, fill=0)  # 顶栏
    for index in range(QUESTIONS):
        top = 120 + index * 75
        draw.rectangle([40, top, 52, top + 28], fill=0)  # 行首记号（倒置后跑到右边）
        draw.text((72, top + 2), f"{index + 1}.question", fill=0, font=font)
    image.save(path)
    return path


def rotate_copy(source: Path, angle: int, target: Path) -> Path:
    with Image.open(source) as image:
        image.rotate(angle, expand=True, fillcolor=255).save(target)
    return target


def _profile(image: Image.Image, axis: str) -> list[int]:
    """先二值化再投影成 1 像素宽/高：得到每行（或每列）的墨迹占比（0~255）。"""
    ink = image.point(lambda value: 255 if value < 128 else 0)
    size = (1, image.height) if axis == "row" else (image.width, 1)
    return list(ink.resize(size, Image.Resampling.BOX).tobytes())


def _bands(profile: list[int], ink: int = 4) -> list[tuple[int, int]]:
    """墨迹占比高于阈值的连续区间 = 一段墨迹。"""
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(profile):
        if value > ink:
            if start is None:
                start = index
        elif start is not None:
            bands.append((start, index))
            start = None
    if start is not None:
        bands.append((start, len(profile)))
    return bands


def _dense_bands(profile: list[int]) -> list[tuple[int, int]]:
    """大面积着墨的带 = 顶栏（文字行达不到这个占比）。"""
    return [band for band in _bands(profile, ink=_DENSE) if band[1] - band[0] >= 10]


def _title_angle(gray: Image.Image) -> int | None:
    """顶栏朝向 → 摆正所需角度：横着看在上半/下半页，竖着看偏左/偏右。"""
    width, height = gray.size
    rows = _dense_bands(_profile(gray, "row"))
    if rows:
        y0, y1 = rows[0]
        return 0 if (y0 + y1) / 2 < height / 2 else 180
    cols = _dense_bands(_profile(gray, "col"))
    if not cols:
        return None
    x0, x1 = cols[0]
    return 90 if (x0 + x1) / 2 > width / 2 else 270


def _head_at_left(strip: Image.Image) -> bool:
    """行首记号在左端？记号是"短墨迹 + 明显空隙"，倒在右端则相反。"""
    runs = _bands(_profile(strip, "col"))
    if len(runs) < 2:
        return False
    first_start, first_end = runs[0]
    second_start, _ = runs[1]
    return (first_end - first_start) <= 30 and (second_start - first_end) >= 6


def _overlaps(band: tuple[int, int], others: list[tuple[int, int]]) -> bool:
    return any(band[0] < end and start < band[1] for start, end in others)


class GeometryEngine:
    """只看墨迹几何的假引擎：方向对才读得出题号。

    对应真引擎的行为差异：躺倒时行框成竖条、文字读不出；倒置时行框仍横排，
    但行首记号跑到右端，也读不出。只有正着放时才吐出带题号的横排行。
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        with Image.open(path) as image:
            gray = image.convert("L")
        angle = _title_angle(gray)
        if angle is None:
            return [], 0.01
        if angle in (90, 270):  # 躺倒：题号行是竖条，一行都读不出来
            return [
                (
                    ((x0, 20), (x1, 20), (x1, gray.height - 20), (x0, gray.height - 20)),
                    "",
                    0.2,
                )
                for x0, x1 in _bands(_profile(gray, "col"))
            ], 0.01
        dense_rows = _dense_bands(_profile(gray, "row"))
        result = []
        index = 0
        for y0, y1 in _bands(_profile(gray, "row")):
            if _overlaps((y0, y1), dense_rows):
                continue  # 顶栏不是一行文字
            strip = gray.crop((0, y0, gray.width, y1))
            runs = _bands(_profile(strip, "col"))
            if not runs:
                continue
            x0, x1 = runs[0][0], runs[-1][1]
            index += 1
            readable = _head_at_left(strip)
            result.append(
                (
                    ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
                    f"{index}.question" if readable else "",
                    0.95 if readable else 0.2,
                )
            )
        return result, 0.01


@pytest.fixture()
def geometry_engine(monkeypatch):
    engine = GeometryEngine()
    monkeypatch.setattr(ocr_engine, "_engine", engine)
    return engine


# ---------- AC2/AC4：CLI 摆正后再切题 ----------


def test_cli_cut_straightens_sideways_page(tmp_path, geometry_engine, capsys):
    # 原图顺时针转 90°（PIL 逆时针 270°）→ 需要逆时针 90° 摆正
    source = draw_page(tmp_path / "page.png")
    sideways = rotate_copy(source, 270, tmp_path / "sideways.png")

    assert ocr_main.main(["cut", str(sideways)]) == 0

    out = capsys.readouterr().out
    first = out.splitlines()[0]
    assert first.startswith("判定方向 90°，共 4 题，耗时 ")
    assert "[ 1]" in out and "[ 4]" in out
    assert "[? " not in out  # 题号全都在卷面上认出来了，没有推测题


def test_cli_cut_keeps_upright_page_at_zero(tmp_path, geometry_engine, capsys):
    path = draw_page(tmp_path / "page.png")

    assert ocr_main.main(["cut", str(path)]) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("判定方向 0°，共 4 题，耗时 ")


# ---------- AC4/AC2：结构上不许退回不摆正的识别 ----------


def _line(text: str, y: int) -> OcrLine:
    box = ((100, y), (300, y), (300, y + 24), (100, y + 24))
    return OcrLine(text=text, box=box, confidence=0.95, y_center=y + 12)


def test_cli_cut_reads_through_read_page(tmp_path, monkeypatch, capsys):
    """直接断言 CLI 调用了带方向判定的 `read_page`，而不是裸 `recognize`。"""
    path = tmp_path / "page.png"
    path.write_bytes(b"spy only")
    page = OrientedPage(
        image=None,
        angle=90,
        score=12.0,
        lines=[_line("1.第一题", 100), _line("第一题的续行", 140), _line("3.第三题", 180)],
    )
    seen: list[str] = []

    def fake_read_page(target: str) -> OrientedPage:
        seen.append(target)
        return page

    monkeypatch.setattr(ocr_main, "read_page", fake_read_page)

    assert ocr_main.main(["cut", str(path)]) == 0

    out = capsys.readouterr().out
    assert seen == [str(path)]  # 确实走的是 read_page
    assert out.splitlines()[0].startswith("判定方向 90°，共 3 题，耗时 ")
    assert "[? 2]" in out  # 旧标签保留
    assert "[推测]" in out  # 推测题号另有中文标记


# ---------- AC5：禁用时报可读错误，不吞异常 ----------


def test_cli_cut_reports_readable_error_when_disabled(tmp_path, monkeypatch, capsys):
    path = draw_page(tmp_path / "page.png")
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")

    assert ocr_main.main(["cut", str(path)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OCR 失败" in captured.err
    assert "CAIRN_OCR_DISABLE" in captured.err
    assert "Traceback" not in captured.err
