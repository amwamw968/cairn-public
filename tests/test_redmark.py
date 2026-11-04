"""红笔痕迹检测卡：只判"题框里有没有红笔"，不判勾/叉。

覆盖：

- AC2 判据/阈值：红勾、红叉、红分数检出；坏框不炸、不给假阳性；
- AC3 无红笔的卷子：印刷黑字 + 灰线的"干净卷面"全部 False（真图 paper-01..04
  不在仓库，真机数据见 docs/EVIDENCE.md）；
- AC4 误判防线：蓝笔、黑笔、浅灰阴影都不许命中；
- AC5 页面：检出红笔的题默认勾上 + 标「检测到红笔」，家长可以不勾；
- AC6 性能：2560×1098 整页 18 题 ≤ 0.5 秒；
- AC7 `CAIRN_RED_MARK=0` 整层关闭。

合成图在这里画，真图路径一个都不写死。
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from cairn import scan_service
from cairn.models import Learner, Scan, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.ocr import redmark
from cairn.ocr import __main__ as ocr_main
from cairn.ocr.cut import QuestionBlock
from cairn.ocr.orient import OrientedPage
from cairn.web import create_app

# 合成图配色：红笔用正红，干扰项用真实笔色（蓝圆珠笔 / 黑笔 / 纸上的浅灰阴影）
PAPER = (252, 251, 247)  # 略偏暖的白纸，不是纯白
RED = (200, 30, 40)
BLUE = (30, 60, 190)
BLACK = (20, 20, 20)
GRAY = (205, 205, 200)
PRINT = (25, 25, 25)
RULE = (170, 170, 170)

BOX = 460, 220  # 单题框尺寸：与阈值标定用的框一致（见 redmark 模块注释）


def _blank(size: tuple[int, int] = BOX) -> Image.Image:
    return Image.new("RGB", size, PAPER)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 老 Pillow 的 load_default 不吃 size
        return ImageFont.load_default()


def _draw_tick(target: Image.Image, color, width: int = 6) -> None:
    """画一个对勾：两笔折线，就是老师在题上打的"√"。"""
    draw = ImageDraw.Draw(target)
    draw.line([(120, 120), (175, 175)], fill=color, width=width)
    draw.line([(175, 175), (280, 70)], fill=color, width=width)


def _draw_cross(target: Image.Image, color, width: int = 6) -> None:
    draw = ImageDraw.Draw(target)
    draw.line([(130, 70), (280, 190)], fill=color, width=width)
    draw.line([(280, 70), (130, 190)], fill=color, width=width)


def _draw_score(target: Image.Image, color, text: str = "-5", size: int = 56) -> None:
    ImageDraw.Draw(target).text((300, 60), text, fill=color, font=_font(size))


# ---------------------------------------------------------------- AC4 真正检出


def test_red_tick_is_detected():
    """红勾 → True（最常见的批改痕迹）。"""
    image = _blank()
    _draw_tick(image, RED)

    assert redmark.detect_red_marks(image, [(0, 0, *BOX)]) == [True]


def test_red_cross_is_detected():
    """红叉 → True（本卡不分勾/叉，两样都得检出）。"""
    image = _blank()
    _draw_cross(image, RED)

    assert redmark.detect_red_marks(image, [(0, 0, *BOX)]) == [True]


def test_red_score_is_detected():
    """红色分数 → True（老师直接写分的卷子）。"""
    image = _blank()
    _draw_score(image, RED)

    assert redmark.detect_red_marks(image, [(0, 0, *BOX)]) == [True]


def test_jpeg_compression_keeps_detection():
    """真机是 JPEG：压缩后的色块边缘变糊，判据要照样过。"""
    image = _blank()
    _draw_tick(image, RED)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    compressed = Image.open(io.BytesIO(buffer.getvalue()))

    assert redmark.detect_red_marks(compressed, [(0, 0, *BOX)]) == [True]


# ---------------------------------------------------------------- AC4 不许误判


@pytest.mark.parametrize(
    "name,paint",
    [
        ("蓝笔勾", lambda image: _draw_tick(image, BLUE)),
        ("黑笔勾", lambda image: _draw_tick(image, BLACK)),
        ("浅灰阴影", lambda image: ImageDraw.Draw(image).rectangle(
            [60, 40, 400, 200], fill=GRAY)),
    ],
)
def test_non_red_ink_is_not_detected(name, paint):
    """蓝笔 / 黑笔 / 浅灰阴影 → False（不是红笔就不许预勾）。"""
    image = _blank()
    paint(image)

    ratios = redmark.red_ratios(image, [(0, 0, *BOX)])
    assert redmark.detect_red_marks(image, [(0, 0, *BOX)]) == [False], name
    assert ratios == [0.0], name


def test_warm_white_paper_is_not_detected():
    """暖光下的白纸（R 高、但 G/B 也跟着高）不许命中。"""
    image = Image.new("RGB", BOX, (252, 240, 220))

    assert redmark.detect_red_marks(image, [(0, 0, *BOX)]) == [False]


# ---------------------------------------------------------------- AC3 干净卷面


def _clean_page(width: int = 1000, height: int = 1400) -> Image.Image:
    """一页"没有批改痕迹"的卷子：黑字 + 灰横线 + 灰题框，模拟印刷卷。"""
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)
    font = _font(24)
    for index in range(6):
        top = 40 + index * 210
        draw.rectangle([40, top, width - 40, top + 180], outline=RULE, width=2)
        draw.text((60, top + 20), f"{index + 1}. 计算 128 + 356 =", fill=PRINT, font=font)
        draw.text((60, top + 70), "答：____", fill=PRINT, font=font)
        draw.line([(60, top + 130), (width - 80, top + 130)], fill=RULE, width=1)
    return image


def test_clean_page_detects_nothing():
    """AC3：无红笔的整页 → 每个题框都 False（真图 paper-01..04 的替身）。"""
    image = _clean_page()
    boxes = [(40, 40 + index * 210, 960, 220 + index * 210) for index in range(6)]

    ratios = redmark.red_ratios(image, boxes)

    assert redmark.detect_red_marks(image, boxes) == [False] * 6
    assert max(ratios) == 0.0


def test_clean_page_survives_jpeg(tmp_path):
    """AC3：干净卷面存成 JPEG（真机路径）后仍然一个都不检出。"""
    path = tmp_path / "clean.jpg"
    _clean_page().save(path, quality=75)
    boxes = [(40, 40 + index * 210, 960, 220 + index * 210) for index in range(6)]

    assert redmark.detect_in_page(path, boxes) == [False] * 6


# ---------------------------------------------------------------- 判据与框的边界


def test_ratio_is_share_of_box_area():
    """比值口径 = 红像素 / 该框面积（不是整页），阈值才有可比性。"""
    image = _blank()
    _draw_tick(image, RED)

    whole, tight = redmark.red_ratios(image, [(0, 0, *BOX), (60, 40, 340, 200)])

    assert 0.005 < whole < 0.03  # 整框里 1% 上下（标定实测 1.10%）
    assert tight > whole  # 框缩到墨迹附近，占比必须上升


def test_degenerate_and_out_of_bounds_boxes_are_false():
    """坏框（零面积 / 反向 / 越界）判 False，且不抛异常。"""
    image = _blank()
    _draw_tick(image, RED)
    boxes = [
        (10, 10, 10, 10),  # 零面积
        (300, 10, 200, 100),  # x 反向
        (-500, -500, -100, -100),  # 整块在图外
        (0, 0, BOX[0] * 3, BOX[1] * 3),  # 越界：裁到图内
    ]

    assert redmark.detect_red_marks(image, boxes) == [False, False, False, True]


def test_empty_boxes_returns_empty():
    assert redmark.red_ratios(_blank(), []) == []
    assert redmark.detect_red_marks(_blank(), []) == []


def test_grayscale_image_never_matches():
    """灰度扫描件三通道相等，红色判据天然不成立。"""
    image = _blank().convert("L")

    assert redmark.detect_red_marks(image, [(0, 0, *BOX)]) == [False]


# ---------------------------------------------------------------- 摆正后的坐标（AC5 前置）


def test_detect_in_page_uses_oriented_coordinates(tmp_path):
    """侧拍页：bbox 是"摆正后"的坐标，按 angle 转过来才框得住红笔。

    正着的一页里红勾在左上 → 顺时针转 90°（需逆时针 90° 摆正）后红勾跑到
    右上。用左上那块坐标去判：
    - angle=90（先摆正）→ 命中；
    - angle=0（拿未摆正的图直接套）→ 那块是白纸，必须 False。
    """
    upright = Image.new("RGB", (400, 300), PAPER)
    ImageDraw.Draw(upright).line(
        [(30, 90), (60, 60)], fill=RED, width=6
    )
    sideways = tmp_path / "sideways.png"
    upright.rotate(270, expand=True).save(sideways)  # 400×300 → 300×400

    box = (20, 20, 110, 100)  # 摆正后：红勾所在的左上块
    assert redmark.detect_in_page(sideways, [box], angle=90) == [True]
    assert redmark.detect_in_page(sideways, [box], angle=0) == [False]


def test_detect_in_page_tolerates_missing_file(tmp_path):
    """图不在了 → 全 False，不抛（标错页不能因为红笔检测 500）。"""
    assert redmark.detect_in_page(tmp_path / "nope.jpg", [(0, 0, 100, 100)]) == [False]


# ---------------------------------------------------------------- AC7 开关


def test_enabled_by_default_and_disabled_by_env(monkeypatch):
    monkeypatch.delenv(redmark.ENV_DISABLE, raising=False)
    assert redmark.is_enabled() is True

    monkeypatch.setenv(redmark.ENV_DISABLE, "1")
    assert redmark.is_enabled() is True  # 只有显式 0 才算关

    monkeypatch.setenv(redmark.ENV_DISABLE, "0")
    assert redmark.is_enabled() is False


def test_detect_in_page_short_circuits_when_disabled(tmp_path, monkeypatch):
    """关掉时不读图：给个不存在的路径也不报错，直接全 False。"""
    monkeypatch.setenv(redmark.ENV_DISABLE, "0")

    assert redmark.detect_in_page(tmp_path / "nope.jpg", [(0, 0, 10, 10)]) == [False]


# ---------------------------------------------------------------- AC6 性能


def _full_page(width: int = 2560, height: int = 1098) -> tuple[Image.Image, list]:
    """一整页 18 题（3 栏 × 6 行）+ 其中两道打红勾。"""
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)
    font = _font(26)
    boxes = []
    for row in range(6):
        for col in range(3):
            x0, y0 = 40 + col * 840, 30 + row * 175
            box = (x0, y0, x0 + 780, y0 + 150)
            boxes.append(box)
            draw.rectangle(box, outline=RULE, width=2)
            draw.text((x0 + 12, y0 + 12), f"{row * 3 + col + 1}. 计算 128 + 356 = 484",
                      fill=PRINT, font=font)
    for index in (5, 12):
        x0 = 40 + ((index - 1) % 3) * 840
        y0 = 30 + ((index - 1) // 3) * 175
        draw.line([(x0 + 600, y0 + 80), (x0 + 640, y0 + 120)], fill=RED, width=6)
        draw.line([(x0 + 640, y0 + 120), (x0 + 710, y0 + 40)], fill=RED, width=6)
    return image, boxes


def test_eighteen_questions_under_half_a_second(tmp_path):
    """AC6：2560×1098、18 题，含 JPEG 解码在内必须 ≤ 0.5 秒。"""
    image, boxes = _full_page()
    path = tmp_path / "page.jpg"
    image.save(path, quality=85)

    started = time.perf_counter()
    flags = redmark.detect_in_page(path, boxes)
    elapsed = time.perf_counter() - started

    assert [index + 1 for index, flag in enumerate(flags) if flag] == [5, 12]
    assert elapsed < 0.5, f"18 题检测用了 {elapsed:.3f}s"


# ---------------------------------------------------------------- CLI


def _blocks() -> list[QuestionBlock]:
    return [
        QuestionBlock(no=1, bbox=(0, 0, 460, 220), stem_text="1. 计算 3 + 4 =", line_indexes=[0]),
        QuestionBlock(no=2, bbox=(0, 240, 460, 460), stem_text="2. 计算 9 - 5 =", line_indexes=[1]),
    ]


def test_cli_redmark_prints_ratio_per_question(tmp_path, monkeypatch, capsys):
    """CLI：打印每题红像素占比与判定，末尾给预勾清单。"""
    page_image = Image.new("RGB", (460, 470), PAPER)
    draw = ImageDraw.Draw(page_image)
    draw.line([(120, 300), (175, 355)], fill=RED, width=6)  # 第 2 题的框里
    draw.line([(175, 355), (280, 280)], fill=RED, width=6)
    path = tmp_path / "page.png"
    page_image.save(path)

    monkeypatch.setattr(
        ocr_main,
        "read_page",
        lambda target: OrientedPage(image=page_image, angle=0, score=1.0, lines=[]),
    )
    monkeypatch.setattr(ocr_main, "cut_questions", lambda lines, width, height: _blocks())

    assert ocr_main.main(["redmark", str(path)]) == 0

    out = capsys.readouterr().out
    assert "共 2 题" in out
    assert "[ 1] 无   红像素 0.00%" in out
    assert "[ 2] 红笔 红像素 " in out
    assert "检出 1 题有红笔痕迹（会预勾）：[2]" in out


def test_cli_redmark_reports_no_marks(tmp_path, monkeypatch, capsys):
    path = tmp_path / "page.png"
    Image.new("RGB", (460, 470), PAPER).save(path)
    monkeypatch.setattr(
        ocr_main,
        "read_page",
        lambda target: OrientedPage(image=Image.new("RGB", (460, 470), PAPER), angle=0,
                                    score=1.0, lines=[]),
    )
    monkeypatch.setattr(ocr_main, "cut_questions", lambda lines, width, height: _blocks())

    assert ocr_main.main(["redmark", str(path)]) == 0

    out = capsys.readouterr().out
    assert "没检出红笔痕迹（一题都不预勾）" in out
    assert "红笔 " not in out


def test_cli_redmark_respects_disable_switch(tmp_path, monkeypatch, capsys):
    """AC7：关掉时连 read_page 都不该调。"""
    monkeypatch.setenv(redmark.ENV_DISABLE, "0")
    called: list[str] = []
    monkeypatch.setattr(ocr_main, "read_page", lambda target: called.append(target))

    assert ocr_main.main(["redmark", str(tmp_path / "page.png")]) == 0

    captured = capsys.readouterr()
    assert called == []
    assert "红笔检测已关闭" in captured.out


def test_cli_usage_mentions_redmark(capsys):
    assert ocr_main.main([]) == 2
    assert "redmark" in capsys.readouterr().err


# ---------------------------------------------------------------- AC5 页面


WIDTH, HEIGHT = 1000, 1400


def _scan(session, learner, path: str = "scans/2026/11/shot.jpg") -> Scan:
    row = Scan(
        learner_id=learner.id,
        device_id="upload",
        device_name="浏览器上传",
        width=WIDTH,
        height=HEIGHT,
        captured_at=utcnow(),
        path=path,
        source="upload",
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture()
def app_client(tmp_path):
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as client:
        yield client, db


def _seed_page(db_path, *, red_in: tuple[int, ...], draw_marks: bool = True) -> int:
    """落一张照片 + 5 道题；`red_in` 里的题号在题框内画红勾。"""
    root = db_path.parent
    target = root / "scans" / "2026" / "11" / "shot.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    font = _font(26)
    blocks = []
    for index in range(5):
        top = 60 + index * 260
        no = index + 1
        box = (40, top, 960, top + 220)
        blocks.append(
            QuestionBlock(no=no, bbox=box, stem_text=f"{no}. 计算 {no} + {no} =", line_indexes=[index])
        )
        draw.rectangle(box, outline=RULE, width=2)
        draw.text((60, top + 20), f"{no}. 计算 {no} + {no} =", fill=PRINT, font=font)
        if draw_marks and no in red_in:
            draw.line([(700, top + 150), (760, top + 60)], fill=RED, width=6)
            draw.line([(760, top + 60), (860, top + 150)], fill=RED, width=6)
    image.save(target, quality=85)

    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            scan = _scan(session, learner)
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


def _checkbox(body: str, no: int) -> str:
    """取出某个复选框的那一行 HTML，用来断言 checked 有没有。"""
    marker = f'name="no" value="{no}"'
    start = body.index(marker)
    return body[start:body.index(">", start) + 1]


def _badge_count(body: str) -> int:
    return body.count(">检测到红笔</span>")


def test_page_prechecks_questions_with_red_marks(app_client):
    """AC5：红笔题默认勾上 + 标「检测到红笔」，没红笔的题保持未勾。"""
    client, db = app_client
    scan_id = _seed_page(db, red_in=(2, 4))

    body = client.get(f"/scans/{scan_id}/questions").text

    assert "checked" in _checkbox(body, 2)
    assert "checked" in _checkbox(body, 4)
    assert "checked" not in _checkbox(body, 1)
    assert "checked" not in _checkbox(body, 3)
    assert _badge_count(body) == 2
    assert "有 2 道题的题框里检测到红笔痕迹" in body


def test_page_without_red_marks_checks_nothing(app_client):
    """AC3：没有红笔的卷子 → 一个都不预勾，也没有红笔标记。"""
    client, db = app_client
    scan_id = _seed_page(db, red_in=(), draw_marks=False)

    body = client.get(f"/scans/{scan_id}/questions").text

    assert "checked" not in body
    assert "检测到红笔" not in body


def test_page_red_precheck_can_be_cancelled(app_client):
    """AC5：预勾只是默认值——家长全不勾提交，就一题都不收进去。"""
    client, db = app_client
    scan_id = _seed_page(db, red_in=(1, 2, 3, 4, 5))

    body = client.post(
        f"/scans/{scan_id}/questions",
        data={"subject": "数学", "question_type": "计算"},
    ).text

    assert "没有勾选任何题，一道都没收" in body
    assert "已收好 0 题" not in body


def test_page_red_marks_off_when_disabled(app_client, monkeypatch):
    """AC7：CAIRN_RED_MARK=0 → 页面回到"全部不勾"的旧行为。"""
    client, db = app_client
    scan_id = _seed_page(db, red_in=(2, 4))
    monkeypatch.setenv(redmark.ENV_DISABLE, "0")

    body = client.get(f"/scans/{scan_id}/questions").text

    assert "checked" not in body
    assert "检测到红笔" not in body


def test_page_survives_unreadable_image(app_client):
    """图读不了（老数据/坏文件）→ 红笔全 False，页面照常 200。"""
    client, db = app_client
    scan_id = _seed_page(db, red_in=(1,))

    # 把真图换成坏字节：切题行已经落库，页面不该去重切
    (db.parent / "scans" / "2026" / "11" / "shot.jpg").write_bytes(b"\xff\xd8\xff\xe0broken")

    resp = client.get(f"/scans/{scan_id}/questions")

    assert resp.status_code == 200
    assert "checked" not in resp.text
    assert "检测到红笔" not in resp.text
