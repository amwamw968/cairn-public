"""上传自动预处理卡（迁移 v19）：找纸边 → 透视矫正 → 去阴影，处理不了就退回原图。

单元用例全部用**合成图**（PIL 画一张"深色桌面上的一张白纸"），不依赖 OCR 模型、
不依赖仓库外真图；真图（paper-03/04）的前后对比在 `docs/EVIDENCE.md`。

覆盖：

- 找边/矫正/增强本身：四角落在真值附近、矫正后是矩形、**保留颜色**；
- AC3 回落：纯白 / 极暗 / 满幅文字 → 摆正不生效、上传照常成功（预处理抛异常也一样
  不挡上传）；`processed_path` 存的是**增强图**，没纸边也照样有——增强只看灰度
  分布、不看四边（`test_enhance.py` 里那条真图用例钉的就是这个）；
- AC2 上传：另存 `scans/YYYY/MM/<uuid>.processed.jpg`、写 `processed_path`、原图字节不变；
- AC5 尺寸：长边 ≤3000、小图不放大；
- AC6 读图链路：优先 processed、文件缺失/越界回落原图；切题用的图与 `bbox_json.page` 一致；
- AC8 CLI：`preprocess <输入> <输出>` 打印四角与耗时；
- 迁移 v19：v18 形状的库升上来，存量行 `processed_path` 为 NULL。
"""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select

import cairn.web as web_module
from cairn import scan_service
from cairn.migrations import SCHEMA_VERSION, apply_migrations
from cairn.models import Learner, Scan, init_db, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.ocr import __main__ as ocr_main
from cairn.ocr import preprocess
from cairn.ocr.engine import OcrLine
from cairn.ocr.orient import OrientedPage
from cairn.web import create_app

# 一张"桌上摆着纸"的合成照：深色桌面 + 白色四边形纸张 + 若干行黑字
DESK = (68, 70, 74)
PAPER = (246, 246, 242)
TILTED = ((70, 90), (430, 70), (450, 520), (60, 540))  # 有点斜，正是要矫正的样子


def page_photo(
    size: tuple[int, int] = (500, 600),
    quad: tuple[tuple[int, int], ...] = TILTED,
    *,
    lines: int = 8,
    red: bool = False,
    desk: tuple[int, int, int] = DESK,
) -> Image.Image:
    """合成一张"拍歪了的卷子"：桌面是暗的、纸是亮的、纸上有字（可选一道红笔）。"""
    width, height = size
    image = Image.new("RGB", size, desk)
    draw = ImageDraw.Draw(image)
    draw.polygon(quad, fill=PAPER)
    # 字条两侧留出纸边：真卷面的字不会顶到裁切边，贴边的话整块暗区会把纸
    # 的亮区从中间断开（那是"满幅文字"，本来就该判成没有纸边）
    inset = max(45, int(min(size) * 0.12))
    left = min(x for x, _ in quad) + inset
    right = max(x for x, _ in quad) - inset
    top = min(y for _, y in quad) + inset
    bottom = max(y for _, y in quad) - inset
    step = max(30, (bottom - top) // max(1, lines))
    for index in range(lines):
        y = top + index * step
        if y + 12 > bottom:
            break
        draw.rectangle([left, y, right, y + 12], fill=(25, 25, 25))
    if red:
        y = bottom - 20
        draw.line([(left, y), (right, y - 10)], fill=(200, 20, 20), width=6)
    return image


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _nearest(points, target):
    return min(points, key=lambda p: (p[0] - target[0]) ** 2 + (p[1] - target[1]) ** 2)


# ------------------------------------------------------------ 找边 / 矫正 / 增强


def test_find_page_quad_locates_the_sheet():
    """纸在画面中间、拍歪几度：四角要落在真值附近（±15px），顺序是 左上→左下。"""
    quad = preprocess.find_page_quad(page_photo())

    assert quad is not None
    assert len(quad.points) == 4
    for expected in TILTED:
        found = _nearest(quad.points, expected)
        assert abs(found[0] - expected[0]) <= 15, (found, expected)
        assert abs(found[1] - expected[1]) <= 15, (found, expected)
    # 左上角 = x+y 最小的那个；顺序不对的话后续透视变换会把图翻过去
    assert quad.points[0] == min(quad.points, key=lambda p: p[0] + p[1])
    assert 0.4 < quad.area_ratio < 0.8


def test_warp_page_output_is_rectangle_sized_by_the_sheet():
    """矫正后的宽高取对边长度的较大值：斜拍不该把纸压扁成一条。"""
    image = page_photo()
    quad = preprocess.find_page_quad(image)
    warped = preprocess.warp_page(image, quad)

    # 纸的上/下边 ~365、左/右边 ~450；形态学开闭会让边界胖出几个像素
    assert abs(warped.width - 365) <= 40
    assert abs(warped.height - 450) <= 40
    assert 0.7 < warped.width / warped.height < 1.0
    assert warped.size != image.size


def test_processed_page_keeps_colour():
    """不二值化、不转灰度：红笔那道线处理完还得是红的（颜色通道没被拍平）。"""
    result = preprocess.preprocess_page(page_photo(red=True))

    assert result.changed
    assert result.image.mode == "RGB"
    pixels = np.asarray(result.image)
    red = (pixels[..., 0] > 120) & (pixels[..., 1] < 100) & (pixels[..., 2] < 100)
    assert int(red.sum()) > 50


def test_pure_white_page_has_no_boundary_and_falls_back():
    """AC3：纯白页找不到纸边 → 原图原样返回，不当成"处理过"。"""
    white = Image.new("RGB", (600, 800), "white")

    assert preprocess.find_page_quad(white) is None
    result = preprocess.preprocess_page(white)
    assert result.changed is False
    assert result.quad is None
    assert result.image.size == (600, 800)
    assert result.image is not None and np.asarray(result.image).std() == 0


def test_dark_page_has_no_boundary_and_falls_back():
    """AC3：整图极暗（没开灯/镜头盖）分不出纸和背景 → 不处理。"""
    dark = Image.new("RGB", (600, 800), (18, 18, 20))

    assert preprocess.find_page_quad(dark) is None
    assert preprocess.preprocess_page(dark).changed is False


def test_full_frame_text_has_no_boundary_and_falls_back():
    """AC3：满幅文字（没有纸边、只有密密麻麻的字）→ 不处理。

    画满一张网格：亮区被黑线切碎，最大亮块拼不出 15% 画面以上的四边形。
    """
    image = Image.new("RGB", (600, 800), PAPER)
    draw = ImageDraw.Draw(image)
    for y in range(0, 800, 40):
        draw.rectangle([0, y, 600, y + 8], fill=(20, 20, 20))
    for x in range(0, 600, 60):
        draw.rectangle([x, 0, x + 6, 800], fill=(20, 20, 20))

    assert preprocess.find_page_quad(image) is None
    assert preprocess.preprocess_page(image).changed is False


def test_low_contrast_desk_has_no_boundary_and_falls_back():
    """AC3：白纸放在浅色桌面上，四条边都没有"里亮外暗"的落差 → 边不可信，不处理。

    只靠分割会认出一个几乎等于整张画面的四边形，照着它去矫正是白折腾
    （真图上更糟：纸面上的阴影分界线会被当成纸边，把纸切掉一块）。
    """
    image = page_photo(desk=(242, 242, 240))  # 与纸面 (246,246,242) 只差 4

    assert preprocess.find_page_quad(image) is None
    assert preprocess.preprocess_page(image).changed is False


def test_save_processed_writes_nothing_without_a_page(tmp_path):
    """没找到边界就不写盘：`processed_path` 留空（= 读图回落原图）靠文件在不在判。"""
    target = tmp_path / "scan.processed.jpg"

    assert preprocess.save_processed(Image.new("RGB", (600, 800), "white"), target) is False
    assert not target.exists()


def test_long_side_is_capped_at_3000():
    """AC5：长边 4200 的图裁完仍然是 4200，必须等比缩到 3000。"""
    image = page_photo(size=(4200, 3200), quad=((60, 60), (4140, 60), (4140, 3140), (60, 3140)))
    result = preprocess.preprocess_page(image)

    assert result.changed
    assert max(result.size) == preprocess.PAGE_MAX_SIDE
    assert abs(result.size[0] / result.size[1] - 4200 / 3200) < 0.02


def test_small_image_is_not_upscaled():
    """AC5：小图不放大（放大只会变糊，还骗不过 OCR）。"""
    image = page_photo(size=(320, 400), quad=((30, 40), (290, 35), (300, 370), (25, 380)))
    result = preprocess.preprocess_page(image)

    assert result.changed
    assert max(result.size) <= 400
    assert max(result.size) >= 300


# ---------------------------------------------------------------- 上传（AC2 / AC3）


@contextmanager
def _db(tmp_path):
    """临时库 + 一个孩子；`db_url` 指向库里，图片就落在它旁边。"""
    db_path = tmp_path / "app.db"
    url = f"sqlite:///{db_path}"
    engine = make_engine(url)
    init_db(engine)
    session = make_session_factory(engine)()
    learner = Learner(name="kid", grade_level="primary-3")
    session.add(learner)
    session.commit()
    try:
        yield session, learner, url
    finally:
        session.close()
        engine.dispose()


def test_upload_saves_processed_copy_and_keeps_original(tmp_path):
    """AC2：原图字节一个不改；另存 `<uuid>.processed.jpg` 并把相对路径写进库。

    裁到纸（第二步 a）也一样：结果写进旁边那份 `<uuid>.crop.jpg`，`scan.path`
    指向的原图一个字节不动。
    """
    data = png_bytes(page_photo(size=(1200, 1600), quad=((150, 200), (1050, 170), (1100, 1400), (120, 1450))))
    with _db(tmp_path) as (session, learner, db_url):
        scan = scan_service.save_upload(session, learner.id, data, db_url=db_url)

        original = tmp_path / scan.path
        assert original.is_file()
        assert original.read_bytes() == data  # 原图一律保留、一个字节不改
        assert re.fullmatch(r"scans/\d{4}/\d{2}/[0-9a-f]{32}\.jpg", scan.path)

        assert scan.processed_path is not None
        assert re.fullmatch(
            r"scans/\d{4}/\d{2}/[0-9a-f]{32}\.processed\.jpg", scan.processed_path
        )
        processed = tmp_path / scan.processed_path
        assert processed.is_file()
        assert processed.read_bytes()[:3] == b"\xff\xd8\xff"  # 存的是 JPEG
        # 同一目录、同一个 uuid 前缀，家长在 Finder 里一眼能对上
        assert processed.stem == f"{Path(scan.path).stem}.processed"
        # 这张纸占六成、裁得动：旁边另有一张裁好的，读图先拿它
        cropped = scan_service.crop_path_for_scan(scan, db_url)
        assert cropped.name == f"{original.stem}.crop.png"
        assert cropped.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert scan_service.read_image_path(scan, db_url) == cropped


def test_upload_without_page_boundary_still_saves_the_enhanced_copy(tmp_path, caplog):
    """AC3：纯白页找不到纸边 → 上传照常成功、照实记 warning，但增强照跑。

    第二版起 `processed_path` 指的是**增强图**，增强只看灰度分布、不看四边
    （四张真样本全都找不到纸边，旧门槛会让它们在真实上传里一张都不被增强）。
    这里钉的是新契约：摆正没生效、原图一个字节没动，处理图照样有。
    """
    data = png_bytes(Image.new("RGB", (1200, 1600), "white"))
    with _db(tmp_path) as (session, learner, db_url):
        with caplog.at_level(logging.WARNING, logger="cairn.scan_service"):
            scan = scan_service.save_upload(session, learner.id, data, db_url=db_url)

        assert scan.processed_path is not None
        processed = tmp_path / scan.processed_path
        folder = (tmp_path / scan.path).parent
        # 第二步 a 起，裁切结论也写在同一个目录（`<uuid>.crop.json`）：这张纯白图
        # 分不出纸，所以记录里是"跳过"，照片本身一个字节不改。
        assert sorted(p.name for p in folder.iterdir()) == sorted(
            [
                Path(scan.path).name,
                processed.name,
                Path(scan.path).with_suffix(scan_service.CROP_RECORD_SUFFIX).name,
            ]
        )
        assert processed.read_bytes()[:3] == b"\xff\xd8\xff"  # 增强后的灰度 JPEG
        assert "找不到纸张边界" in caplog.text
        assert (tmp_path / scan.path).read_bytes() == data  # 原图没动
        assert scan_service.read_image_path(scan, db_url) == processed.resolve()


def test_upload_survives_preprocess_crash(tmp_path, monkeypatch, caplog):
    """硬约束：预处理自己炸了也不能挡上传——退回原图增强，照片照常入库。"""
    def boom(*_args, **_kwargs):
        raise RuntimeError("cv2 在算的时候炸了")

    monkeypatch.setattr(preprocess, "save_processed", boom)
    data = png_bytes(page_photo(size=(1200, 1600)))
    with _db(tmp_path) as (session, learner, db_url):
        with caplog.at_level(logging.WARNING, logger="cairn.scan_service"):
            scan = scan_service.save_upload(session, learner.id, data, db_url=db_url)

        assert scan.id is not None
        assert (tmp_path / scan.path).read_bytes() == data
        assert "预处理失败" in caplog.text
        # 摆正炸了不连累增强：直接拿原图增强，处理图仍然有一张
        assert scan.processed_path is not None
        processed = tmp_path / scan.processed_path
        assert processed.is_file()
        assert processed.read_bytes()[:3] == b"\xff\xd8\xff"


# ------------------------------------------------------------ 读图链路（AC6 的前半）


def _seed_scan(session, learner, db_url, *, processed: bool) -> tuple[Scan, Path, Path]:
    """直接造一行 scan + 磁盘上的原图/处理图（绕过上传，方便控制两张图的尺寸）。"""
    root = scan_service.db_dir(db_url)
    folder = root / "scans" / "2026" / "11"
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / "shot.png"
    page_photo(size=(1200, 1600)).save(original)
    # 处理图故意做成另一个尺寸：这样"用的是哪张"能直接从宽高看出来
    processed = folder / "shot.processed.jpg"
    Image.new("RGB", (900, 1300), "white").save(processed, format="JPEG")
    scan = Scan(
        learner_id=learner.id,
        device_id="upload",
        device_name="浏览器上传",
        width=1200,
        height=1600,
        captured_at=utcnow(),
        path=original.relative_to(root).as_posix(),
        source="upload",
        processed_path=processed.relative_to(root).as_posix() if processed else None,
    )
    session.add(scan)
    session.commit()
    return scan, original, processed


def test_read_path_prefers_processed_then_falls_back(tmp_path):
    """AC6：有处理图就用它；文件被删了、或路径越界，一律安静回落原图（不抛）。"""
    with _db(tmp_path) as (session, learner, db_url):
        scan, original, processed = _seed_scan(session, learner, db_url, processed=True)
        assert scan_service.read_image_path(scan, db_url) == processed.resolve()

        processed.unlink()
        assert scan_service.read_image_path(scan, db_url) == original.resolve()

        scan.processed_path = "../越界.jpg"  # 库里被改脏也不放飞
        assert scan_service.read_image_path(scan, db_url) == original.resolve()


# ------------------------------------------------------------------------ 页面（AC6）


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'web.db'}")
    with TestClient(app) as c:
        yield c, tmp_path / "web.db"


def _app_session(db_path):
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    return engine, make_session_factory(engine)


def test_image_endpoint_serves_processed_and_says_so(client):
    """展示也用处理图（和切题同一张），响应头标出来源；文件没了就退回原图。"""
    c, db = client
    engine, factory = _app_session(db)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner)).first()
            scan, original, processed = _seed_scan(
                session, learner, f"sqlite:///{db}", processed=True
            )
            scan_id = scan.id
    finally:
        engine.dispose()

    resp = c.get(f"/scans/{scan_id}/image")
    assert resp.status_code == 200
    assert resp.headers["x-cairn-source"] == "processed"
    assert resp.content == processed.read_bytes()

    processed.unlink()
    resp = c.get(f"/scans/{scan_id}/image")
    assert resp.status_code == 200
    assert resp.headers["x-cairn-source"] == "original"
    assert resp.content == original.read_bytes()


def test_questions_are_cut_on_the_processed_image(client, monkeypatch):
    """AC6：切题用处理图 → `bbox_json.page` 记的就是处理图的宽高（与展示同一张）。"""
    c, db = client
    engine, factory = _app_session(db)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner)).first()
            scan, _original, processed = _seed_scan(
                session, learner, f"sqlite:///{db}", processed=True
            )
            scan_id = scan.id
    finally:
        engine.dispose()

    def fake_read_page(path):
        with Image.open(path) as opened:
            opened.load()
            image = opened.copy()
        width, _height = image.size
        line = OcrLine(
            text="1. 计算 3 + 4 =",
            box=((40, 40), (width - 40, 40), (width - 40, 80), (40, 80)),
            confidence=0.95,
            y_center=60,
        )
        return OrientedPage(image=image, angle=0, score=1.0, lines=[line])

    monkeypatch.setattr(web_module.ocr, "read_page", fake_read_page)
    resp = c.get(f"/scans/{scan_id}/questions")
    assert resp.status_code == 200
    # 处理图 900×1300（原图 1200×1600）：page 必须记 900×1300，否则叠框整体错位
    assert "aspect-ratio: 900 / 1300" in resp.text

    with factory() as session:
        rows = scan_service.list_questions(session, scan_id)
    assert rows
    assert scan_service.page_geometry(rows) == (900, 1300, 0)
    assert processed.is_file()


# ------------------------------------------------------------------- CLI（AC8）

def test_cli_preprocess_prints_corners_and_time(tmp_path, capsys):
    source = tmp_path / "page.png"
    page_photo().save(source)
    target = tmp_path / "out.jpg"

    assert ocr_main.main(["preprocess", str(source), str(target)]) == 0

    printed = capsys.readouterr().out
    assert "纸张四角" in printed
    for corner in ("左上", "右上", "右下", "左下"):
        assert corner in printed
    assert "耗时" in printed
    assert target.is_file()


def test_cli_preprocess_reports_no_boundary_and_writes_nothing(tmp_path, capsys):
    source = tmp_path / "white.png"
    Image.new("RGB", (600, 800), "white").save(source)
    target = tmp_path / "out.jpg"

    assert ocr_main.main(["preprocess", str(source), str(target)]) == 0

    assert "没找到纸张边界" in capsys.readouterr().out
    assert not target.exists()


def test_cli_preprocess_needs_two_paths(capsys):
    assert ocr_main.main(["preprocess", "only-one.png"]) == 2
    assert "preprocess" in capsys.readouterr().err


# ----------------------------------------------------------------- 迁移 v19


def _make_v18_db(db_path: Path) -> None:
    """v18 形状的库：先按当前模型建库，再摘掉 v19 加的那一列并把版本退回 18。"""
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE scan DROP COLUMN processed_path")
        conn.exec_driver_sql("UPDATE schema_version SET version = 18 WHERE id = 1")
    engine.dispose()


def test_v19_adds_processed_path_without_touching_rows(tmp_path):
    """v18 老库升级：列加回来、版本 19、存量行的其余字段逐个不变、新列为 NULL。"""
    db = tmp_path / "v19.db"
    _make_v18_db(db)

    engine = make_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO learner (name, grade_level, created_at) VALUES ('kid', 'primary-3', '2026-11-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO scan (learner_id, device_id, device_name, width, height, captured_at, path, source) "
            "VALUES (1, 'upload', '浏览器上传', 1200, 1600, '2026-11-01', 'scans/2026/11/a.jpg', 'upload')"
        )
    assert apply_migrations(engine) == SCHEMA_VERSION
    engine.dispose()

    con = sqlite3.connect(str(db))
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(scan)")}
        version = con.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
        row = con.execute(
            "SELECT device_id, path, source, processed_path FROM scan"
        ).fetchone()
    finally:
        con.close()

    assert "processed_path" in columns
    assert version == SCHEMA_VERSION
    assert row == ("upload", "scans/2026/11/a.jpg", "upload", None)
