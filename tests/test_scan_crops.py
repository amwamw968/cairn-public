"""题块裁图卡（迁移 v17）：切题落库时按 bbox 从摆正后的页面上裁题块图。

覆盖 AC2–AC5：

- AC5 迁移 v17：v16 老库（`scan_question` 没有 `crop_path`）升级 → 列在、
  存量行一条不动、版本到 17；重复升级幂等。
- AC2 裁图：裁出来的尺寸与 bbox 一致（宽高都相等）、落在与照片同级的
  `crops/` 目录、文件名 `<scan_id>-<no>.jpg`、`crop_path` 回填成相对路径。
- AC5 幂等：同一批切题再跑一遍，已存在的裁图不重写（用哨兵字节证明没被覆盖）。
- AC4 失败不挡收进去：坐标整块在页外、页图不可用都只记 warning，行照样在。
- AC3 详情页：有裁图就出 `<img>`；裁图被删/没有裁图的题显示"暂无原图"，不 500。

服务层用**文件库**（`tmp_path`）测：裁图路径是相对库目录算的，内存库会落到
工作目录，没法断言落盘位置。页面用 TestClient。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn import scan_service
from cairn.migrations import SCHEMA_VERSION, apply_migrations
from cairn.models import (
    Learner,
    Scan,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.models.learner import utcnow
from cairn.models.scan import ScanQuestion
from cairn.ocr.cut import QuestionBlock
from cairn.web import create_app

WIDTH, HEIGHT = 1000, 1400
SHOT = "scans/2026/11/shot.jpg"


def _block(no: int, bbox: tuple[int, int, int, int], stem: str | None = None) -> QuestionBlock:
    return QuestionBlock(
        no=no,
        bbox=bbox,
        stem_text=stem or f"{no}. 计算 {no} + {no} =",
        line_indexes=[no - 1],
    )


def _write_page(tmp_path) -> Path:
    """在库目录下摆一张合成页图（白底 1000×1400），返回路径。"""
    target = tmp_path / SHOT
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (WIDTH, HEIGHT), "white").save(target, format="JPEG", quality=95)
    return target


def _open_page(tmp_path) -> Image.Image:
    return Image.open(_write_page(tmp_path))


@pytest.fixture()
def env(tmp_path):
    """文件库 + 一个孩子 + 一张照片行；裁图目录跟着库目录走。"""
    db = tmp_path / "crops.db"
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    factory = make_session_factory(engine)
    session = factory()
    learner = Learner(name="crop-child", grade_level="primary-3")
    session.add(learner)
    session.commit()
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
    try:
        yield tmp_path, db, f"sqlite:///{db}", session, learner, scan
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------- AC5 迁移 v17


def _make_v16_db(db) -> None:
    """v16 形状的库：表都在、`scan_question` 还没有 `crop_path`、版本钉在 16。

    当前模型建库后把 v17 加的那列摘掉、版本退回 16——这样存量库是「v16 老库」，
    而不是被 create_all 提前修好的库。SQLite ≥3.35 支持 DROP COLUMN。
    """
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    engine.dispose()
    con = sqlite3.connect(str(db))
    try:
        con.execute("ALTER TABLE scan_question DROP COLUMN crop_path")
        con.execute("UPDATE schema_version SET version = 16 WHERE id = 1")
        con.commit()
    finally:
        con.close()


def _columns(db, table: str) -> set[str]:
    con = sqlite3.connect(str(db))
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _scalar(db, sql: str):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def test_v17_adds_crop_path_without_touching_data(tmp_path):
    """AC5：v16 老库升级 → 版本 17、列在、存量切题行一条不动且 crop_path 为 NULL。"""
    db = tmp_path / "v17.db"
    _make_v16_db(db)
    assert "crop_path" not in _columns(db, "scan_question")  # 前提：确实缺列

    con = sqlite3.connect(str(db))
    try:
        con.execute(
            """
            INSERT INTO learner (name, grade_level, created_at)
            VALUES ('老库孩子', 'primary', '2026-10-01 08:00:00')
            """
        )
        con.execute(
            """
            INSERT INTO scan (learner_id, device_id, device_name, width, height,
                              captured_at, path, source)
            VALUES (1, 'upload', '浏览器上传', 1000, 1400, '2026-10-01 08:00:00',
                    'scans/2026/10/old.jpg', 'upload')
            """
        )
        con.execute(
            """
            INSERT INTO scan_question (scan_id, no, bbox_json, stem_text, is_wrong, created_at)
            VALUES (1, 3, '{"bbox": [10, 20, 310, 120], "page": [1000, 1400], "angle": 0}',
                    '3. 存量题：7 + 8 =', 1, '2026-10-01 08:00:00')
            """
        )
        con.commit()
    finally:
        con.close()

    engine = make_engine(f"sqlite:///{db}")
    try:
        assert apply_migrations(engine) == SCHEMA_VERSION
        assert apply_migrations(engine) == SCHEMA_VERSION  # 幂等：再跑一次不报 duplicate column
    finally:
        engine.dispose()

    assert "crop_path" in _columns(db, "scan_question")
    assert _scalar(db, "select version from schema_version where id = 1") == SCHEMA_VERSION
    # 纯加列：存量行逐字段不变，新列全 NULL
    assert _scalar(db, "select count(*) from scan_question") == 1
    assert _scalar(db, "select stem_text from scan_question where no = 3") == "3. 存量题：7 + 8 ="
    assert _scalar(db, "select is_wrong from scan_question where no = 3") == 1
    assert _scalar(db, "select count(*) from scan_question where crop_path is null") == 1


# ---------------------------------------------------------------- AC2 裁图尺寸


def test_crop_matches_bbox_size_and_location(env):
    """AC2：裁图尺寸 == bbox 宽高；落在照片同级的 crops/，命名 <scan_id>-<no>.jpg。"""
    tmp_path, _db, db_url, session, _learner, scan = env
    page = _open_page(tmp_path)
    bbox = (20, 40, 480, 70)

    rows = scan_service.store_questions(
        session, scan.id, [_block(1, bbox)], page_width=WIDTH, page_height=HEIGHT,
        scan=scan, page_image=page, db_url=db_url,
    )

    assert len(rows) == 1
    expected = tmp_path / "scans" / "2026" / "11" / "crops" / f"{scan.id}-1.jpg"
    assert expected.is_file()
    assert expected.parent.name == "crops"
    assert expected.parent.parent == (tmp_path / "scans" / "2026" / "11")  # 与照片同级
    assert rows[0].crop_path == f"scans/2026/11/crops/{scan.id}-1.jpg"

    with Image.open(expected) as crop:
        assert crop.size == (bbox[2] - bbox[0], bbox[3] - bbox[1]) == (460, 30)


def test_crop_uses_oriented_page_coordinates(env):
    """AC2：裁的是**摆正后**的页图——横过来的页面按摆正后的坐标圈块。"""
    tmp_path, _db, db_url, session, _learner, scan = env
    # 原图 1000×1400 躺倒，摆正后 1400×1000：题框落在摆正后的坐标系里
    page = Image.new("RGB", (1400, 1000), "white")
    scan_service.store_questions(
        session, scan.id, [_block(7, (100, 200, 900, 260))],
        page_width=1400, page_height=1000, angle=90,
        scan=scan, page_image=page, db_url=db_url,
    )
    target = tmp_path / "scans" / "2026" / "11" / "crops" / f"{scan.id}-7.jpg"
    with Image.open(target) as crop:
        assert crop.size == (800, 60)


def test_crop_is_idempotent(env):
    """AC5：同一批切题再跑一遍不重写已存在的裁图（哨兵字节原样还在）。"""
    tmp_path, _db, db_url, session, _learner, scan = env
    page = _open_page(tmp_path)
    blocks = [_block(1, (20, 40, 480, 70)), _block(2, (20, 80, 480, 110))]
    scan_service.store_questions(
        session, scan.id, blocks, page_width=WIDTH, page_height=HEIGHT,
        scan=scan, page_image=page, db_url=db_url,
    )
    target = tmp_path / "scans" / "2026" / "11" / "crops" / f"{scan.id}-1.jpg"
    assert target.is_file()
    target.write_bytes(b"sentinel-not-a-jpeg")

    # 重切一次（题号全都有，行不新增）；裁图必须跳过，不覆盖哨兵
    again = scan_service.store_questions(
        session, scan.id, blocks, page_width=WIDTH, page_height=HEIGHT,
        scan=scan, page_image=page, db_url=db_url,
    )

    assert len(again) == 2
    assert target.read_bytes() == b"sentinel-not-a-jpeg"
    assert {row.crop_path for row in again} == {
        f"scans/2026/11/crops/{scan.id}-1.jpg",
        f"scans/2026/11/crops/{scan.id}-2.jpg",
    }


def test_crop_clips_partially_outside_bbox(env):
    """bbox 只差几像素出界（OCR 常见）→ 夹进页面照裁，不丢题。"""
    tmp_path, _db, db_url, session, _learner, scan = env
    page = _open_page(tmp_path)
    scan_service.store_questions(
        session, scan.id, [_block(4, (-50, -50, 100, 60))],
        page_width=WIDTH, page_height=HEIGHT,
        scan=scan, page_image=page, db_url=db_url,
    )
    target = tmp_path / "scans" / "2026" / "11" / "crops" / f"{scan.id}-4.jpg"
    with Image.open(target) as crop:
        assert crop.size == (100, 60)


# ---------------------------------------------------------------- AC4 失败不挡收进去


def test_crop_failure_keeps_row_and_logs_warning(env, caplog):
    """AC4：坐标整块在页外不抛异常——行照样落库，只有一行 warning。"""
    _tmp_path, _db, db_url, session, _learner, scan = env
    page = Image.new("RGB", (WIDTH, HEIGHT), "white")

    with caplog.at_level(logging.WARNING, logger="cairn.scan_service"):
        rows = scan_service.store_questions(
            session, scan.id, [_block(5, (2000, 2000, 2100, 2100))],
            page_width=WIDTH, page_height=HEIGHT,
            scan=scan, page_image=page, db_url=db_url,
        )

    assert [row.no for row in rows] == [5]  # 行落库了
    assert rows[0].crop_path is None  # 但没裁出图
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "裁图失败" in warnings[0].getMessage()


def test_missing_page_image_keeps_row_and_logs_warning(env, caplog):
    """AC4：页图不可用（图解不开/照片没了）同样只警告，不影响收进去。"""
    _tmp_path, _db, db_url, session, _learner, scan = env

    with caplog.at_level(logging.WARNING, logger="cairn.scan_service"):
        rows = scan_service.store_questions(
            session, scan.id, [_block(6, (20, 40, 200, 80))],
            page_width=WIDTH, page_height=HEIGHT,
            scan=scan, page_image=None, db_url=db_url,
        )

    assert [row.no for row in rows] == [6]
    assert rows[0].crop_path is None
    assert any("裁图失败" in rec.getMessage() for rec in caplog.records)


def test_crop_without_scan_or_db_url_still_stores_rows(session, learner):
    """不给 scan/db_url 的老调用方（CLI/测试）行为不变：只落行，不裁图。"""
    scan = Scan(
        learner_id=learner.id, device_id="upload", device_name="浏览器上传",
        width=WIDTH, height=HEIGHT, captured_at=utcnow(), path=SHOT, source="upload",
    )
    session.add(scan)
    session.commit()

    rows = scan_service.store_questions(
        session, scan.id, [_block(1, (20, 40, 480, 70))],
        page_width=WIDTH, page_height=HEIGHT,
    )

    assert [row.no for row in rows] == [1]
    assert rows[0].crop_path is None


# ---------------------------------------------------------------- AC3 详情页


@pytest.fixture()
def web(tmp_path):
    """页面库：一张真图 + 一道切题行；返回 (client, db_path, root)。"""
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as client:
        yield client, db, tmp_path


def _seed_detail(db, root, *, crop: bool = True) -> tuple[int, int]:
    """落一张照片 + 一道题（收进去）+ 可选的裁图；返回 (scan_id, question_id)。"""
    target = root / SHOT
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (WIDTH, HEIGHT), "white").save(target, format="JPEG", quality=95)
    with Image.open(target) as page:
        engine = make_engine(f"sqlite:///{db}")
        factory = make_session_factory(engine)
        try:
            with factory() as session:
                learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
                scan = Scan(
                    learner_id=learner.id, device_id="upload", device_name="浏览器上传",
                    width=WIDTH, height=HEIGHT, captured_at=utcnow(), path=SHOT,
                    source="upload",
                )
                session.add(scan)
                session.commit()
                rows = scan_service.store_questions(
                    session, scan.id,
                    [_block(2, (20, 80, 480, 140), "2. 小明有 8 个苹果，吃了 3 个，还剩几个？")],
                    page_width=WIDTH, page_height=HEIGHT,
                    scan=scan, page_image=page if crop else None,
                    db_url=f"sqlite:///{db}",
                )
                result = scan_service.import_wrong_questions(
                    session, learner.id, scan, [2], subject="数学", question_type="计算",
                )
                assert result.created == (2,)
                question_id = session.scalar(
                    select(ScanQuestion.question_id).where(ScanQuestion.no == 2)
                )
                return scan.id, int(question_id)
        finally:
            engine.dispose()


def test_detail_page_shows_crop(web):
    """AC3：有裁图 → 页面出 <img> 指向裁图路由，路由本身回 200 + JPEG。"""
    client, db, root = web
    _scan_id, question_id = _seed_detail(db, root)
    assert question_id

    page = client.get(f"/questions/{question_id}")

    assert page.status_code == 200
    assert f"/questions/{question_id}/crop" in page.text
    assert "暂无原图" not in page.text

    image = client.get(f"/questions/{question_id}/crop")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert image.content[:2] == b"\xff\xd8"  # JPEG 头


def test_detail_page_without_crop_says_no_image(web):
    """AC3：裁图文件被删 → 页面 200 且显示"暂无原图"，绝不 500。"""
    client, db, root = web
    scan_id, question_id = _seed_detail(db, root)
    crop_file = root / "scans" / "2026" / "11" / "crops" / f"{scan_id}-2.jpg"
    assert crop_file.is_file()
    crop_file.unlink()

    page = client.get(f"/questions/{question_id}")

    assert page.status_code == 200
    assert "暂无原图" in page.text
    assert client.get(f"/questions/{question_id}/crop").status_code == 404


def test_detail_page_without_any_crop_row(web):
    """AC3：老数据（这一行压根没裁过图）→ 200 + "暂无原图"。"""
    client, db, root = web
    _scan_id, question_id = _seed_detail(db, root, crop=False)
    assert client.get(f"/questions/{question_id}").status_code == 200
    assert "暂无原图" in client.get(f"/questions/{question_id}").text


def test_detail_page_unknown_question_is_404(web):
    client, _db, _root = web
    assert client.get("/questions/999").status_code == 404


def test_crop_route_rejects_path_outside_db(web):
    """库里 crop_path 被改成越界路径 → 404，不许读学会了目录外的文件。"""
    client, db, root = web
    _scan_id, question_id = _seed_detail(db, root)
    con = sqlite3.connect(str(db))
    try:
        con.execute("UPDATE scan_question SET crop_path = '../../etc/passwd'")
        con.commit()
    finally:
        con.close()

    assert client.get(f"/questions/{question_id}/crop").status_code == 404
    assert client.get(f"/questions/{question_id}").status_code == 200
