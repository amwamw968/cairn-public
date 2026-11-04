"""上传通道卡（迁移 v15）：浏览器上传 → 质量指标入库 → 人工确认建题。

前端质量门（canvas 指标）不在这测——纯 JS，浏览器才有 canvas；这里测的是
服务端契约：multipart 收图、硬校验（含短边下限）、指标 JSON 原样入库、确认建题走
entry_service、同一张图确认两次不重复建题、迁移 v15 防御式升级。

测试图用纯 zlib 手写 PNG（不依赖 PIL 编码）；默认尺寸放在短边下限之上，
小图用例显式传尺寸。
"""

from __future__ import annotations

import io
import json
import sqlite3
import struct
import zlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from cairn import scan_service
from cairn.migrations import SCHEMA_VERSION, apply_migrations
from cairn.models import Learner, Question, Scan, make_engine, make_session_factory
from cairn.web import create_app

QUALITY = {
    "brightness": 203.5,
    "dark_ratio": 0.041,
    "overexposure_ratio": 0.002,
    "blur_var": 312.7,
    "edge_density": 0.183,
}

# 默认上传图：短边 1200（> 下限 1000），老用例不用每题都写尺寸
DEFAULT_W, DEFAULT_H = 1200, 1600


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/web.db")
    with TestClient(app) as c:
        # These cases exercise the historical, single-question upload contract.
        from cairn.models.workflow import Workflow
        with app.state.session_factory() as session:
            session.get(Workflow, "default").active_version_id = "default:v1"
            session.commit()
        yield c, tmp_path / "web.db"


def make_png(width: int = DEFAULT_W, height: int = DEFAULT_H) -> bytes:
    """最小合法 PNG：单色 RGBA，全部用标准库手写（尺寸写进 IHDR，PIL 能读出来）。"""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff" * (width * 4) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _session(db_path):
    engine = make_engine(f"sqlite:///{db_path}")
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


def _upload(c, tmp_file=None, *, quality=None, filename="photo.png", png=None):
    data = {"quality": quality} if quality is not None else {}
    files = {"file": (filename, make_png() if png is None else png, "image/png")}
    return c.post(
        "/scans/upload", files=files, data=data, follow_redirects=False
    )


# ------------------------------------------------------- 上传成功与 400 三类


def test_upload_success_writes_file_and_row(client):
    c, db = client
    response = _upload(c, quality=json.dumps(QUALITY))
    assert response.status_code == 303
    scan_id = response.headers["location"].rsplit("/", 1)[-1]

    with _session(db) as session:
        scan = session.get(Scan, int(scan_id))
        assert scan.source == "upload"
        # quality_json 与前端提交的字符串逐字一致
        assert json.loads(scan.quality_json) == QUALITY
        assert (scan.width, scan.height) == (0, 0)  # 指标里没带分辨率，按 0 存
        # 落盘：相对库目录、按年月分目录、uuid 文件名，字节与上传一致
        import re

        stamp = datetime.now().astimezone()
        assert re.fullmatch(
            rf"scans/{stamp:%Y}/{stamp:%m}/[0-9a-f]{{32}}\.jpg", scan.path
        ), scan.path
        written = db.parent / scan.path
        assert written.is_file()
        assert written.read_bytes() == make_png()
        assert scan.device_id == "upload"
        assert scan.learner_id == session.scalars(select(Learner.id)).first()


def test_upload_quality_json_carries_dimensions(client):
    c, db = client
    quality = dict(QUALITY, width=4032, height=3024)
    response = _upload(c, quality=json.dumps(quality))
    assert response.status_code == 303
    with _session(db) as session:
        scan = session.scalars(select(Scan)).one()
        assert (scan.width, scan.height) == (4032, 3024)


def test_upload_non_image_rejected(client):
    c, db = client
    response = c.post(
        "/scans/upload",
        files={"file": ("notes.txt", b"just some text, no magic bytes", "text/plain")},
    )
    assert response.status_code == 400
    with _session(db) as session:
        assert session.scalars(select(Scan)).all() == []


def test_upload_oversize_rejected(client):
    c, db = client
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * (12 * 1024 * 1024)  # 魔数合法但超 12MB
    response = c.post(
        "/scans/upload", files={"file": ("big.png", payload, "image/png")}
    )
    assert response.status_code == 400
    with _session(db) as session:
        assert session.scalars(select(Scan)).all() == []


def test_upload_bad_quality_json_still_saved_without_it(client):
    """坏 JSON 不许 500：指标被忽略，图照常入库、quality_json 为空。"""
    c, db = client
    response = _upload(c, quality="{not json at all")
    assert response.status_code == 303
    with _session(db) as session:
        scan = session.scalars(select(Scan)).one()
        assert scan.quality_json is None
        assert scan.source == "upload"


def test_upload_quality_non_object_ignored(client):
    c, db = client
    response = _upload(c, quality='["not","an","object"]')
    assert response.status_code == 303
    with _session(db) as session:
        assert session.scalars(select(Scan)).one().quality_json is None


# --------------------------------------------- 低分辨率拦截（短边下限，默认 1000）

# 卡面原文：549×1280 是 Lead 实测的压缩图尺寸（只切出 7 题，原图 18 题）
TOO_SMALL_549 = (
    "这张图太小（549×1280），切题会漏。"
    "请用手机原图或文件方式重新发送（短边至少 1000 像素）。"
)


def test_upload_short_side_rejected_before_write(client):
    """压缩图 549×1280：400 + 页面提示区给原因；落盘和入库一件都不许发生。"""
    c, db = client
    response = _upload(c, png=make_png(549, 1280))
    assert response.status_code == 400
    assert TOO_SMALL_549 in response.text
    # 原因回归到上传页的警示条（新外壳：.notice notice--danger；浏览器不会替我们渲染 JSON detail）
    assert '<p class="notice notice--danger">' in response.text
    assert 'class="reject"' not in response.text
    assert not list(db.parent.rglob("*.jpg"))  # 校验在 write_bytes 之前
    with _session(db) as session:
        assert session.scalars(select(Scan)).all() == []


def test_upload_short_side_999_rejected(client):
    """边界下侧：短边 999 仍然拒（差 1 像素也不放行）。"""
    c, db = client
    response = _upload(c, png=make_png(999, 1280))
    assert response.status_code == 400
    assert "这张图太小（999×1280）" in response.text
    assert not list(db.parent.rglob("*.jpg"))
    with _session(db) as session:
        assert session.scalars(select(Scan)).all() == []


def test_upload_short_side_1000_accepted(client):
    """边界上侧：短边 1000 照常入库（落盘 + 一行 scan）。"""
    c, db = client
    response = _upload(c, png=make_png(1000, 1280))
    assert response.status_code == 303
    with _session(db) as session:
        scan = session.scalars(select(Scan)).one()
        assert (db.parent / scan.path).is_file()
        assert (db.parent / scan.path).read_bytes() == make_png(1000, 1280)


def test_min_side_env_override_moves_the_line(client, monkeypatch):
    """CAIRN_MIN_IMAGE_SIDE=2000：短边 1500 被拒（默认值下它会通过）。"""
    c, db = client
    monkeypatch.setenv(scan_service.MIN_IMAGE_SIDE_ENV, "2000")

    refused = _upload(c, png=make_png(1500, 2000))
    assert refused.status_code == 400
    assert "短边至少 2000 像素" in refused.text

    accepted = _upload(c, png=make_png(2000, 2600))
    assert accepted.status_code == 303
    with _session(db) as session:
        assert len(session.scalars(select(Scan)).all()) == 1  # 只进了通过的那张


def test_min_side_falls_back_to_default_when_env_is_not_positive_int(monkeypatch):
    """环境变量坏值/非正数 → 回落 1000，不把上传入口关死也不放开。"""
    monkeypatch.delenv(scan_service.MIN_IMAGE_SIDE_ENV, raising=False)
    assert scan_service.min_image_side() == scan_service.MIN_IMAGE_SIDE == 1000
    for raw in ("abc", "", "0", "-1", "12.5"):
        monkeypatch.setenv(scan_service.MIN_IMAGE_SIDE_ENV, raw)
        assert scan_service.min_image_side() == 1000, raw


def test_image_dimensions_reads_header_and_tolerates_garbage():
    """量尺寸只读文件头：合法 PNG 给真尺寸；读不出来返回 None（=尺寸未知，不拒）。"""
    assert scan_service.image_dimensions(make_png(549, 1280)) == (549, 1280)
    assert scan_service.image_dimensions(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) is None
    assert scan_service.image_dimensions(b"") is None


def test_corrupt_image_with_valid_magic_still_saved(client):
    """魔数合法但解不开：沿用既有行为（照常入库），尺寸未知不许变成新拒绝理由。"""
    c, db = client
    response = c.post(
        "/scans/upload",
        files={"file": ("broken.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 256, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _session(db) as session:
        assert session.scalars(select(Scan)).one().source == "upload"


def test_image_dimensions_covers_the_magic_whitelist():
    """白名单里的常见格式都能量出尺寸；HEIC 容器 PIL 不开 → None（尺寸未知，不拦）。"""
    from PIL import Image

    for fmt, ext in (("JPEG", "jpg"), ("PNG", "png"), ("WEBP", "webp")):
        buffer = io.BytesIO()
        Image.new("RGB", (600, 1400), "white").save(buffer, format=fmt)
        data = buffer.getvalue()
        assert scan_service.looks_like_image(data), fmt
        assert scan_service.image_dimensions(data) == (600, 1400), fmt

    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 2048
    assert scan_service.looks_like_image(heic)
    assert scan_service.image_dimensions(heic) is None


def test_upload_page_carries_min_side_for_prompt(client):
    """上传页把下限下发给前端（提示区先拦一道），无错误时不显示拒绝块。"""
    c, _db = client
    html = c.get("/upload").text
    assert "const MIN_SIDE = 1000;" in html
    assert 'class="reject"' not in html


def test_upload_page_short_side_hint_uses_configured_limit(client, monkeypatch):
    """页面下发的下限跟着环境变量走，不会出现前端 1000 / 后端 2000 的错位。"""
    c, _db = client
    monkeypatch.setenv(scan_service.MIN_IMAGE_SIDE_ENV, "1500")
    assert "const MIN_SIDE = 1500;" in c.get("/upload").text


# --------------------------------------------------------------- 人工确认入库


def _confirm(c, scan_id, **overrides):
    form = {
        "subject": "数学",
        "question_type": "计算",
        "content": "三加五等于多少？",
        "answer": "八",
        **overrides,
    }
    return c.post(
        f"/scans/{scan_id}/confirm", data=form, follow_redirects=False
    )


def test_confirm_creates_question_and_links_scan(client):
    c, db = client
    scan_id = _upload(c).headers["location"].rsplit("/", 1)[-1]
    response = _confirm(c, scan_id)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/practice")

    with _session(db) as session:
        questions = session.scalars(select(Question)).all()
        assert len(questions) == 1
        question = questions[0]
        assert question.subject == "数学"
        assert question.question_type == "计算"
        assert question.content == "三加五等于多少？"
        assert question.answer == "八"
        scan = session.scalars(select(Scan)).one()
        assert scan.question_id == question.id
        # 走的是 entry_service 既有路径：题入库必须带 error_item（复习排期靠它）
        item = session.execute(
            text("select count(*) from error_item where question_id = :qid"),
            {"qid": question.id},
        ).scalar_one()
        assert item == 1


def test_confirm_twice_reuses_question(client):
    """同一张图确认两次：题只有一条，scan.question_id 不变、不指向新题。"""
    c, db = client
    scan_id = _upload(c).headers["location"].rsplit("/", 1)[-1]
    first = _confirm(c, scan_id)
    assert first.status_code == 303
    second = _confirm(c, scan_id, content="故意的不同内容")
    assert second.status_code == 303

    with _session(db) as session:
        questions = session.scalars(select(Question)).all()
        assert len(questions) == 1  # 变异点：去掉复用判断这条会红
        scan = session.scalars(select(Scan)).one()
        assert scan.question_id == questions[0].id


def test_confirm_missing_field_rejected_no_write(client):
    c, db = client
    scan_id = _upload(c).headers["location"].rsplit("/", 1)[-1]
    response = _confirm(c, scan_id, answer="  ")  # 空答案 → ValidationError
    assert response.status_code == 200
    with _session(db) as session:
        assert session.scalars(select(Question)).all() == []
        assert session.scalars(select(Scan)).one().question_id is None


def test_confirm_other_learners_scan_404(client):
    c, db = client
    response = c.post("/scans/999/confirm", data={"subject": "数学"})
    assert response.status_code == 404


# --------------------------------------------------------------- 迁移 v15


def _make_v14_db(db_path) -> None:
    """v14 形状的库：scan 还是旧八列（无 source/quality_json/question_id），带一行老数据。"""
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE learner (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(64) NOT NULL,
                grade_level VARCHAR(16) NOT NULL,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE scan (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                learner_id INTEGER REFERENCES learner(id),
                device_id VARCHAR(128) NOT NULL,
                device_name VARCHAR(128) NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                captured_at DATETIME NOT NULL,
                path VARCHAR(255) NOT NULL UNIQUE,
                note TEXT
            );
            INSERT INTO learner (id, name, grade_level, created_at)
                VALUES (1, 'old-child', 'primary', '2026-10-01');
            INSERT INTO scan (id, learner_id, device_id, device_name, width, height,
                              captured_at, path, note)
                VALUES (7, 1, 'fake:uvc-pro', '假高拍仪', 320, 240,
                        '2026-10-20 08:00:00', 'scans/2026/10/abc.png', '老照片');
            CREATE TABLE schema_version (
                id INTEGER NOT NULL PRIMARY KEY, version INTEGER NOT NULL,
                applied_at DATETIME
            );
            """
        )
        con.execute("INSERT INTO schema_version (id, version) VALUES (1, 14)")
        con.commit()
    finally:
        con.close()


def _columns(db_path, table):
    con = sqlite3.connect(db_path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _row(db_path, sql):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql).fetchone()
    finally:
        con.close()


def test_migration_v15_adds_columns_and_backfills_device(tmp_path):
    db = tmp_path / "v15.db"
    _make_v14_db(db)

    engine = make_engine(f"sqlite:///{db}")
    try:
        assert apply_migrations(engine) == SCHEMA_VERSION
    finally:
        engine.dispose()

    columns = _columns(db, "scan")
    assert {"source", "quality_json", "question_id"} <= columns
    # 老行：source 回填 device，两个新列可空为 NULL，其余字段逐个不变
    row = _row(
        db,
        "SELECT id, device_id, device_name, width, height, captured_at, path, note, "
        "source, quality_json, question_id FROM scan WHERE id = 7",
    )
    assert row[:8] == (
        7, "fake:uvc-pro", "假高拍仪", 320, 240,
        "2026-10-20 08:00:00", "scans/2026/10/abc.png", "老照片",
    )
    assert row[8] == "device"
    assert row[9] is None and row[10] is None
    assert _row(db, "SELECT version FROM schema_version WHERE id = 1")[0] == SCHEMA_VERSION


def test_migration_v15_is_idempotent(tmp_path):
    db = tmp_path / "v15_twice.db"
    _make_v14_db(db)
    engine = make_engine(f"sqlite:///{db}")
    try:
        apply_migrations(engine)
        apply_migrations(engine)
    finally:
        engine.dispose()
    assert _row(db, "SELECT count(*) FROM scan")[0] == 1
