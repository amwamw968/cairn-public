"""前端 Phase 2-b · 家长改四角 → 按新四角**从原图**重裁（只做后端）。

这张卡钉四件事：

1. `POST /scans/{id}/crop` 收 `{"corners": [[x,y]×4]}`（原图坐标，顺时针
   tl→tr→br→bl）或 `{"skip": true}`；重裁**永远读原图**，结果写
   `<uuid>.crop.png`（无损），上传的那份原图一个字节不改。
2. 四角不合格（不是 4 点 / 越界 / 不凸 / 面积 <5% / 最短边 <长边 10%）→ 400，
   而且**裁后图与旁挂 json 都不许被改**（mtime + sha256 断）。
3. 旁挂 json 追加 `human` / `human_corners` / `detected_corners`，旧字段一字不改。
4. `read_image_path` 顺序：`.crop.png` → 旧 `.crop.jpg` → processed → original。

真图用例只用仓库里的 `docs/samples/hi-redbg.jpg`，不依赖 `CAIRN_SAMPLE_DIR`。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn import recipe, scan_service, tools
from cairn.models import Learner, Scan, make_engine, make_session_factory
from cairn.web import create_app

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "docs" / "samples"
HI_REDBG = SAMPLES / "hi-redbg.jpg"

# 自动裁那张的尺寸：裁到纸（长边 2000）再摆正 90°
AUTO_CROP_SIZE = (2000, 1493)
# 原图尺寸（`hi-redbg.jpg` 是竖着拍的）
ORIGINAL_SIZE = (1098, 2560)


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
        # Exercise the historical crop endpoint independently of workflow runs.
        from cairn.models.workflow import Workflow
        with app.state.session_factory() as session:
            session.get(Workflow, "default").active_version_id = "default:v1"
            session.commit()
        yield c


# ---------------------------------------------------------------- 素材


def upload(client: TestClient, path: Path = HI_REDBG) -> int:
    """按浏览器上传那条路交一张真图，返回 scan id。"""
    response = client.post(
        "/scans/upload",
        files={"file": (path.name, path.read_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    return int(response.url.path.rsplit("/", 1)[-1])


def load_scan(db_path: Path, scan_id: int) -> tuple[Scan, Path]:
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            scan = session.get(Scan, scan_id)
            return scan, db_path.parent / scan.path
    finally:
        engine.dispose()


def crop_png_for(stored: Path) -> Path:
    return stored.with_name(f"{stored.stem}{scan_service.CROP_FILE_MARK}.png")


def record_for(stored: Path) -> Path:
    return stored.with_suffix(scan_service.CROP_RECORD_SUFFIX)


def read_record(stored: Path) -> dict:
    return json.loads(record_for(stored).read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stamp(path: Path) -> tuple[str, int]:
    """一份文件的指纹：sha256 + mtime_ns（AC4 用它证明"没被碰过"）。"""
    return sha256_file(path), path.stat().st_mtime_ns


def read_bgr(path: Path):
    """与 `scan_service._read_bgr` 同一套读法（PIL 解码 → BGR），好逐像素对比。"""
    with Image.open(path) as image:
        image.load()
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        return cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)


def inset(corners: list[list[float]], ratio: float = 0.05) -> list[list[float]]:
    """四角各自向形心收 `ratio`（"向内收 5%"）。"""
    cx = sum(point[0] for point in corners) / 4
    cy = sum(point[1] for point in corners) / 4
    return [
        [round(x + (cx - x) * ratio, 2), round(y + (cy - y) * ratio, 2)]
        for x, y in corners
    ]


def post_corners(client: TestClient, scan_id: int, corners) -> object:
    return client.post(f"/scans/{scan_id}/crop", json={"corners": corners})


def auto_crop_size(client: TestClient, scan_id: int) -> tuple[int, int]:
    """自动裁那张的宽高（走图片入口，和前端看到的是同一张）。"""
    response = client.get(f"/scans/{scan_id}/image?source=crop")
    assert response.status_code == 200, response.text
    return _bytes_size(response.content)


def _bytes_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


# ---------------------------------------------------------------- AC2：重裁


def test_adjusted_corners_recrop_from_the_original(client, db_path) -> None:
    """AC2：向内收 5% → 新图是 PNG、尺寸与自动裁的不同、逐像素等于"从原图重算"。"""
    scan_id = upload(client)
    scan, stored = load_scan(db_path, scan_id)
    before_record = read_record(stored)
    auto_size = auto_crop_size(client, scan_id)
    assert auto_size == AUTO_CROP_SIZE
    assert _bytes_size(crop_png_for(stored).read_bytes()) == AUTO_CROP_SIZE

    corners = inset(before_record["corners"])
    response = post_corners(client, scan_id, corners)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["human"] == "adjusted"
    assert payload["crop_url"] == f"/scans/{scan_id}/image?source=crop"

    # 落的是 PNG（我们自己产生的一律无损），文件名固定 `.crop.png`
    cropped = crop_png_for(stored)
    assert cropped.is_file()
    assert cropped.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # 宽高与自动裁的不同；并且等于"拿原图 + 这组四角重新 warp"的结果
    expected = _warp_from_original(stored, corners)
    assert (payload["width"], payload["height"]) == (
        expected.shape[1],
        expected.shape[0],
    )
    assert (payload["width"], payload["height"]) != auto_size

    # 逐像素：证明裁的是**原图**（从裁后图重裁会得到另一张模糊的图）
    assert np.array_equal(read_bgr(cropped), expected)

    # 面积比是这张新图自己的
    assert payload["area_ratio"] == pytest.approx(
        _quad_area(corners) / (ORIGINAL_SIZE[0] * ORIGINAL_SIZE[1]), abs=5e-3
    )

    # 旁挂 json：三个新字段 + 旧字段一字不改
    after_record = read_record(stored)
    assert after_record["human"] == "adjusted"
    assert after_record["human_corners"] == corners
    assert after_record["detected_corners"] == before_record["corners"]
    for key in ("note", "reason", "area_ratio", "corners", "width", "height"):
        assert after_record[key] == before_record[key], key

    # 读图链路换成新裁的这张
    assert scan_service.read_image_path(scan, f"sqlite:///{db_path}") == cropped


def test_adjusted_crop_is_served_by_the_default_image_entry(client, db_path) -> None:
    """重裁之后，"默认入口"发的是新那张（`X-Cairn-Source: crop`）。"""
    scan_id = upload(client)
    _, stored = load_scan(db_path, scan_id)
    corners = inset(read_record(stored)["corners"])

    assert post_corners(client, scan_id, corners).status_code == 200

    served = client.get(f"/scans/{scan_id}/image")
    assert served.status_code == 200
    assert served.headers["x-cairn-source"] == "crop"
    assert served.headers["content-type"] == "image/png"
    assert served.content == crop_png_for(stored).read_bytes()


# ---------------------------------------------------------------- AC3：原图不动


def test_recrop_never_touches_the_original_bytes(client, db_path) -> None:
    """AC3：重裁前后原图 sha256 一致（这条是硬断言，不是口头保证）。"""
    data = HI_REDBG.read_bytes()
    uploaded_sha = hashlib.sha256(data).hexdigest()

    scan_id = upload(client)
    _, stored = load_scan(db_path, scan_id)
    assert sha256_file(stored) == uploaded_sha

    corners = inset(read_record(stored)["corners"])
    assert post_corners(client, scan_id, corners).status_code == 200

    assert sha256_file(stored) == uploaded_sha
    assert stored.read_bytes() == data


# ---------------------------------------------------------------- AC4：非法输入


@pytest.mark.parametrize(
    ("name", "corners"),
    [
        ("three_points", [[100, 500], [1000, 500], [1000, 1700]]),
        ("out_of_frame", [[96, 558], [999, 542], [1020, 1766], [-8, 1766]]),
        ("area_two_percent", [[399, 1130], [699, 1130], [699, 1430], [399, 1430]]),
    ],
)
def test_bad_corners_are_400_and_change_nothing(client, db_path, name, corners) -> None:
    """AC4：三种非法输入各 400，且裁后图与旁挂 json 都没被改（mtime + sha256）。"""
    scan_id = upload(client)
    _, stored = load_scan(db_path, scan_id)

    # 先做一次合格的重裁，让盘上确实有一张"不能被动"的裁后图和一条 sidecar
    good = inset(read_record(stored)["corners"])
    assert post_corners(client, scan_id, good).status_code == 200

    cropped = crop_png_for(stored)
    record = record_for(stored)
    cropped_before = stamp(cropped)
    record_before = stamp(record)

    response = post_corners(client, scan_id, corners)

    assert response.status_code == 400, f"{name}: {response.text}"
    assert stamp(cropped) == cropped_before
    assert stamp(record) == record_before


def test_both_fields_missing_is_400(client, db_path) -> None:
    """既没有 corners 也没有 skip → 400（不猜家长想干嘛）。"""
    scan_id = upload(client)
    _, stored = load_scan(db_path, scan_id)
    record_before = stamp(record_for(stored))

    response = client.post(f"/scans/{scan_id}/crop", json={})

    assert response.status_code == 400
    assert stamp(record_for(stored)) == record_before


# ---------------------------------------------------------------- AC5：skip


def test_skip_marks_the_sidecar_and_writes_no_image(client, db_path) -> None:
    """AC5：`skip: true` 不出新图，只标 `human="skipped"`。"""
    scan_id = upload(client)
    _, stored = load_scan(db_path, scan_id)
    cropped = crop_png_for(stored)
    before = stamp(cropped)

    response = client.post(f"/scans/{scan_id}/crop", json={"skip": True})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["human"] == "skipped"
    assert payload["crop_url"] == f"/scans/{scan_id}/image?source=crop"
    assert (payload["width"], payload["height"]) == AUTO_CROP_SIZE

    record = read_record(stored)
    assert record["human"] == "skipped"
    assert record["human_corners"] is None
    assert stamp(cropped) == before  # 图一个字节没动
    assert len(list(stored.parent.glob(f"{stored.stem}.crop.*"))) == 2  # png + json


# ---------------------------------------------------------------- AC6：老卷子


def test_legacy_jpg_crop_is_still_readable(client, db_path) -> None:
    """AC6：只有旧 `.crop.jpg`、没有 `.crop.png` 的老卷子，`read_image_path` 照样读到。"""
    folder = db_path.parent / "scans" / "2026" / "01"
    folder.mkdir(parents=True, exist_ok=True)
    legacy = folder / "legacy-only-jpg.jpg"
    legacy.write_bytes(HI_REDBG.read_bytes())
    legacy_crop = folder / "legacy-only-jpg.crop.jpg"
    legacy_crop.write_bytes(
        cv2.imencode(".jpg", read_bgr(HI_REDBG))[1].tobytes()
    )
    assert not crop_png_for(legacy).exists()

    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner_id = session.scalars(select(Learner)).first().id
            scan = Scan(
                learner_id=learner_id,
                device_id="upload",
                device_name="浏览器上传",
                width=ORIGINAL_SIZE[0],
                height=ORIGINAL_SIZE[1],
                path=legacy.relative_to(db_path.parent).as_posix(),
                source="upload",
            )
            session.add(scan)
            session.commit()
            scan_id = scan.id
    finally:
        engine.dispose()

    db_url = f"sqlite:///{db_path}"
    with make_session_factory(make_engine(db_url))() as session:
        row = session.get(Scan, scan_id)
        assert scan_service.read_image_path(row, db_url) == legacy_crop
        assert scan_service.crop_path_for_scan(row, db_url) == legacy_crop

    # 图片入口发出来的就是老那张
    served = client.get(f"/scans/{scan_id}/image")
    assert served.status_code == 200
    assert served.headers["x-cairn-source"] == "crop"
    assert served.content == legacy_crop.read_bytes()


# ---------------------------------------------------------------- 小工具


def _warp_from_original(original: Path, corners):
    """测试里独立重算一遍：原图 + 四角 → warp_page。"""
    from cairn.ocr.crop import warp_page

    return warp_page(read_bgr(original), np.asarray(corners, dtype=np.float32))


def _quad_area(corners) -> float:
    points = np.asarray(corners, dtype=np.float32)
    xs, ys = points[:, 0], points[:, 1]
    return 0.5 * abs(
        sum(
            float(xs[i] * ys[(i + 1) % 4] - xs[(i + 1) % 4] * ys[i])
            for i in range(4)
        )
    )
