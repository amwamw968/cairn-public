"""0.2 卡11 — 抓拍落盘、入库、迁移与页面（只用 fake 后端）。

这里不碰真实摄像头：`app.state.cameras` 在 lifespan 之后被换成假后端，
所以 CI 里没插设备、没授权也照样跑。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from cairn.capture import (
    BackendUnavailable,
    Capabilities,
    CaptureError,
    CaptureFormat,
    DeviceInfo,
    DeviceNotFound,
)
from cairn.capture.base import CameraBackend
from cairn.capture.fake import FakeCameraBackend, FakeDevice
from cairn.migrations import SCHEMA_VERSION
from cairn.models import Learner, Scan, init_db, make_engine, make_session_factory
from cairn.scan_service import (
    capture_snapshot,
    get_scan,
    image_path,
    list_scans,
    store_frame,
)
from cairn.web import create_app

FIXED = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)


@dataclass
class Env:
    tmp_path: Path
    db_url: str
    engine: Engine
    session: Session
    backend: FakeCameraBackend
    learner_id: int


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[Env]:
    db = tmp_path / "scan.db"
    db_url = f"sqlite:///{db}"
    engine = make_engine(db_url)
    init_db(engine)
    session = make_session_factory(engine)()
    owner = Learner(name="test-child")
    session.add(owner)
    session.commit()
    try:
        yield Env(
            tmp_path=tmp_path,
            db_url=db_url,
            engine=engine,
            session=session,
            backend=FakeCameraBackend(now=lambda: FIXED),
            learner_id=owner.id,
        )
    finally:
        session.close()
        engine.dispose()


@contextmanager
def _client(tmp_path: Path, backend: CameraBackend) -> Iterator[tuple[object, TestClient]]:
    app = create_app(f"sqlite:///{tmp_path}/web.db")
    with TestClient(app) as client:
        app.state.cameras = backend
        yield app, client


# -------------------------------------------------------- AC5/AC6 落盘与入库


def test_store_frame_writes_file_and_row(env: Env):
    env.backend.open("fake:uvc-pro")
    frame = env.backend.grab()
    env.backend.close()

    scan = store_frame(env.session, frame, env.learner_id, db_url=env.db_url)

    local = FIXED.astimezone()
    assert re.fullmatch(rf"scans/{local:%Y}/{local:%m}/[0-9a-f]{{32}}\.png", scan.path), scan.path
    written = env.tmp_path / scan.path
    assert written.is_file()
    assert written.read_bytes() == frame.data

    row = env.session.scalars(select(Scan)).one()
    assert row.id == scan.id
    assert row.device_id == "fake:uvc-pro"
    assert row.device_name == "假高拍仪（UVC）"
    assert (row.width, row.height) == (frame.width, frame.height) == (320, 240)
    assert row.captured_at == FIXED
    assert row.note is not None and "假帧" in row.note


def test_store_frame_keeps_explicit_note(env: Env):
    env.backend.open("fake:uvc-pro")
    frame = env.backend.grab()
    scan = store_frame(
        env.session, frame, env.learner_id, db_url=env.db_url, note="第三单元练习卷"
    )
    assert scan.note == "第三单元练习卷"


def test_two_snapshots_get_two_files(env: Env):
    first = capture_snapshot(
        env.backend, "fake:uvc-pro", env.session, env.learner_id, db_url=env.db_url
    )
    second = capture_snapshot(
        env.backend, "fake:uvc-pro", env.session, env.learner_id, db_url=env.db_url
    )
    assert first.path != second.path
    assert len(list_scans(env.session, env.learner_id)) == 2


def test_capture_snapshot_closes_device(env: Env):
    scan = capture_snapshot(
        env.backend, "fake:builtin", env.session, env.learner_id, db_url=env.db_url
    )
    assert env.backend.opened_device is None
    assert scan.device_id == "fake:builtin"


def test_capture_failure_leaves_no_file_or_row(env: Env):
    with pytest.raises(DeviceNotFound):
        capture_snapshot(
            env.backend, "fake:ghost", env.session, env.learner_id, db_url=env.db_url
        )
    assert list_scans(env.session, env.learner_id) == []
    assert not (env.tmp_path / "scans").exists()


def test_list_scans_newest_first(env: Env):
    env.backend.open("fake:uvc-pro")
    first = env.backend.grab()
    second = env.backend.grab()
    older = store_frame(
        env.session, replace(first, captured_at=FIXED), env.learner_id, db_url=env.db_url
    )
    newer = store_frame(
        env.session,
        replace(second, captured_at=FIXED + timedelta(hours=1)),
        env.learner_id,
        db_url=env.db_url,
    )
    assert [scan.id for scan in list_scans(env.session, env.learner_id)] == [
        newer.id,
        older.id,
    ]


def test_get_scan_and_image_path_round_trip(env: Env):
    scan = capture_snapshot(
        env.backend, "fake:uvc-pro", env.session, env.learner_id, db_url=env.db_url
    )
    found = get_scan(env.session, env.learner_id, scan.id)
    assert found is not None and found.path == scan.path
    assert image_path(scan, env.db_url) == (env.tmp_path / scan.path).resolve()


def test_image_path_rejects_escape(env: Env):
    bad = Scan(
        device_id="x",
        device_name="x",
        width=1,
        height=1,
        captured_at=FIXED,
        path="../outside.png",
    )
    env.session.add(bad)
    env.session.commit()
    with pytest.raises(ValueError):
        image_path(bad, env.db_url)


# --------------------------------------------------------------- AC7 迁移 v8


def test_migrations_v8_adds_scan_table(tmp_path: Path):
    db = tmp_path / "v8.db"
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    assert SCHEMA_VERSION >= 8

    factory = make_session_factory(engine)
    with factory() as session:
        session.add(Learner(name="小明", grade_level="primary-5"))
        session.commit()

    # 退回「v7 且没有 scan 表」的样子
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE scan")
        conn.exec_driver_sql("UPDATE schema_version SET version = 7 WHERE id = 1")

    init_db(engine)
    assert "scan" in set(inspect(engine).get_table_names())
    engine.dispose()

    con = sqlite3.connect(db)
    try:
        version = con.execute("select version from schema_version where id = 1").fetchone()[0]
        name = con.execute("select name from learner where id = 1").fetchone()[0]
        scan_rows = con.execute("select count(*) from scan").fetchone()[0]
    finally:
        con.close()
    assert version == SCHEMA_VERSION
    assert name == "小明"
    assert scan_rows == 0


# --------------------------------------------------------------- AC8 /devices


def test_devices_page_lists_devices_and_capabilities(tmp_path: Path):
    with _client(tmp_path, FakeCameraBackend(now=lambda: FIXED)) as (_app, client):
        response = client.get("/devices")

    assert response.status_code == 200
    body = response.text
    assert "假高拍仪（UVC）" in body
    assert "假内置摄像头" in body
    assert "320×240@30fps" in body
    assert "自动" in body
    assert "固定" in body  # 内置那台的 focus
    assert "抓一张" in body


def test_devices_page_marks_unsupported_capabilities(tmp_path: Path):
    bare = FakeDevice(
        device=DeviceInfo(stable_id="fake:bare", name="只能抓单帧的假设备"),
        capabilities=Capabilities(
            formats=(CaptureFormat(80, 60, None, "png"),),
            focus="none",
            exposure="unknown",
            white_balance="unknown",
            can_stream=False,
            can_snapshot=True,
            notes=("只支持抓单帧",),
        ),
    )
    with _client(tmp_path, FakeCameraBackend([bare])) as (_app, client):
        body = client.get("/devices").text

    assert "只能抓单帧的假设备" in body
    assert "不支持" in body  # focus=none
    assert "未知" in body  # exposure=unknown
    assert "只支持抓单帧" in body


def test_devices_page_shows_degraded_hint(tmp_path: Path):
    backend = FakeCameraBackend()
    backend.fallback_reason = "AVFoundation 不可用（pyobjc 未安装），已回退到假设备"
    with _client(tmp_path, backend) as (_app, client):
        body = client.get("/devices").text
    assert "降级中" in body
    assert "pyobjc" in body


def test_devices_page_without_devices_says_so(tmp_path: Path):
    with _client(tmp_path, FakeCameraBackend([])) as (_app, client):
        body = client.get("/devices").text
    assert "没有找到摄像头" in body


class _BrokenBackend(CameraBackend):
    """模拟「后端存在但 AVFoundation 起不来」：枚举直接抛。"""

    name = "broken"

    def enumerate_devices(self) -> list[DeviceInfo]:
        raise BackendUnavailable("pyobjc 未安装")

    def open(self, device_id: str) -> DeviceInfo:
        raise BackendUnavailable("pyobjc 未安装")

    def capabilities(self) -> Capabilities:
        raise BackendUnavailable("pyobjc 未安装")

    def grab(self):  # type: ignore[no-untyped-def]
        raise BackendUnavailable("pyobjc 未安装")

    def close(self) -> None:
        return None


def test_devices_page_survives_unavailable_backend(tmp_path: Path):
    with _client(tmp_path, _BrokenBackend()) as (_app, client):
        response = client.get("/devices")

    assert response.status_code == 200
    assert "设备枚举失败" in response.text
    assert "pyobjc 未安装" in response.text


# -------------------------------------------------- AC9 / AC10 抓拍与已拍页


def test_snapshot_post_redirects_to_detail_page(tmp_path: Path):
    backend = FakeCameraBackend(now=lambda: FIXED)
    app = create_app(f"sqlite:///{tmp_path}/web.db")
    with TestClient(app) as client:
        app.state.cameras = backend
        # This checks the historical detail page for a legacy capture.
        from cairn.models.workflow import Workflow
        with app.state.session_factory() as session:
            session.get(Workflow, "default").active_version_id = "default:v1"
            session.commit()

        response = client.post("/devices/fake:uvc-pro/snapshot", follow_redirects=False)
        assert response.status_code == 303
        location = response.headers["location"]
        assert re.fullmatch(r"/scans/\d+", location)

        detail = client.get(location)
        assert detail.status_code == 200
        assert f'src="{location}/image"' in detail.text
        assert "scans/" in detail.text
        assert "假高拍仪（UVC）" in detail.text

        image = client.get(f"{location}/image")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.content[:8] == b"\x89PNG\r\n\x1a\n"

        listing = client.get("/scans")
        assert listing.status_code == 200
        assert f'href="{location}"' in listing.text
        assert "假高拍仪（UVC）" in listing.text

    con = sqlite3.connect(tmp_path / "web.db")
    try:
        row = con.execute(
            "select device_id, device_name, width, height, path, note from scan"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "fake:uvc-pro"
    assert row[1] == "假高拍仪（UVC）"
    assert (row[2], row[3]) == (320, 240)
    assert re.fullmatch(r"scans/\d{4}/\d{2}/[0-9a-f]{32}\.png", row[4])
    assert "假帧" in row[5]


def test_snapshot_failure_stays_on_page_and_writes_nothing(tmp_path: Path):
    with _client(tmp_path, FakeCameraBackend()) as (_app, client):
        response = client.post("/devices/fake:ghost/snapshot")
        assert response.status_code == 200
        assert "抓拍失败" in response.text
        assert "还没有拍过" in client.get("/scans").text

    con = sqlite3.connect(tmp_path / "web.db")
    try:
        count = con.execute("select count(*) from scan").fetchone()[0]
    finally:
        con.close()
    assert count == 0


class _TimeoutBackend(FakeCameraBackend):
    """抓帧等不到画面（真机上 5 秒超时的样子）。"""

    def grab(self):  # type: ignore[no-untyped-def]
        raise CaptureError("没抓到画面：5 秒内没有拿到帧（设备可能被别的程序占用，或没有授权）")


def test_snapshot_timeout_shows_no_picture_hint(tmp_path: Path):
    with _client(tmp_path, _TimeoutBackend()) as (_app, client):
        response = client.post("/devices/fake:uvc-pro/snapshot")
        assert response.status_code == 200
        assert "没抓到画面" in response.text
        assert "还没有拍过" in client.get("/scans").text


def test_scans_page_empty_state(tmp_path: Path):
    with _client(tmp_path, FakeCameraBackend()) as (_app, client):
        body = client.get("/scans").text
    assert "还没有拍过" in body


def test_missing_scan_returns_404(tmp_path: Path):
    with _client(tmp_path, FakeCameraBackend()) as (_app, client):
        assert client.get("/scans/999").status_code == 404
        assert client.get("/scans/999/image").status_code == 404


def test_image_endpoint_rejects_path_escape(tmp_path: Path):
    with _client(tmp_path, FakeCameraBackend()) as (app, client):
        with app.state.session_factory() as session:
            scan = Scan(
                device_id="x",
                device_name="x",
                width=1,
                height=1,
                captured_at=FIXED,
                path="../../etc/hosts",
            )
            session.add(scan)
            session.commit()
            scan_id = scan.id
        assert client.get(f"/scans/{scan_id}/image").status_code == 404
