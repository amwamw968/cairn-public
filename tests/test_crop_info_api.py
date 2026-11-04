"""前端 Phase 2-a：把裁切四角与结论留给前端（sidecar 补字段 + 只读接口）。

三件事：

1. `<uuid>.crop.json` 多两个字段：`corners`（**原图坐标**四点，没有就 null）、
   `width`/`height`（**原图**尺寸，不是裁完那张的）。已有三个字段一字不改。
2. `GET /api/scans/{id}/crop-info` 只念 sidecar，**不重跑检测**：老卷子没有
   sidecar 照样 200（四个结论字段全 null），照片行不存在才 404。
3. 三张真图的结论照旧：`hi-redbg` → ok、`user-20261101` →
   cannot_separate_background、`manual-crop` → already_full_page。

真图用例只用仓库里的 `docs/samples/*.jpg`，不依赖 `CAIRN_SAMPLE_DIR`。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
USER_PHOTO = SAMPLES / "user-20261101.jpg"
MANUAL_CROP = SAMPLES / "manual-crop.jpg"

# 卡一实测的三条结论（本卡只把它们搬到接口上，一字不改）
EXPECTED_REASON = {
    HI_REDBG: "ok",
    USER_PHOTO: "cannot_separate_background",
    MANUAL_CROP: "already_full_page",
}


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


# ---------------------------------------------------------------- 素材


def upload(client: TestClient, path: Path) -> int:
    """按浏览器上传那条路交一张真图，返回 scan id（端点 303 到 `/scans/<id>`）。"""
    response = client.post(
        "/scans/upload",
        files={"file": (path.name, path.read_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    return int(response.url.path.rsplit("/", 1)[-1])


def crop_info(client: TestClient, scan_id: int) -> dict:
    response = client.get(f"/api/scans/{scan_id}/crop-info")
    assert response.status_code == 200, response.text
    return response.json()


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


def crop_file_for(stored: Path) -> Path:
    """原图 → 它旁边那份裁好的图（`<uuid>.jpg` → `<uuid>.crop.png`）。"""
    return stored.with_name(f"{stored.stem}{scan_service.CROP_FILE_MARK}.png")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def signed_area(corners: list[list[float]]) -> float:
    """鞋带公式。图像坐标 y 向下：**结果为正 = 顺时针**（tl→tr→br→bl）。"""
    total = 0.0
    for index, (x0, y0) in enumerate(corners):
        x1, y1 = corners[(index + 1) % len(corners)]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def bbox_ratio(corners: list[list[float]], width: int, height: int) -> float:
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return (max(xs) - min(xs)) * (max(ys) - min(ys)) / (width * height)


# ---------------------------------------------------------------- AC2：三条结论


@pytest.mark.parametrize("sample", [HI_REDBG, USER_PHOTO, MANUAL_CROP], ids=lambda p: p.stem)
def test_reason_matches_the_card_for_each_sample(client, sample) -> None:
    """AC2：三张真图上传后，接口念出的 reason 与卡一实测逐字相等。"""
    scan_id = upload(client, sample)

    payload = crop_info(client, scan_id)

    assert payload["scan_id"] == scan_id
    assert payload["reason"] == EXPECTED_REASON[sample]
    assert isinstance(payload["note"], str) and payload["note"]


def test_every_sample_feeds_the_api_from_a_sidecar(client, db_path) -> None:
    """接口的数不是现算的：同一条 sidecar 里的三个旧字段照旧原样念出来。"""
    scan_id = upload(client, HI_REDBG)
    _, stored = load_scan(db_path, scan_id)

    record = json.loads(
        stored.with_suffix(scan_service.CROP_RECORD_SUFFIX).read_text(encoding="utf-8")
    )
    payload = crop_info(client, scan_id)

    assert payload["reason"] == record["reason"]
    assert payload["note"] == record["note"]
    assert payload["area_ratio"] == record["area_ratio"]


# -------------------------------------- AC2（卡 D2b）：三个亮度字段


@pytest.mark.parametrize("sample", [HI_REDBG, USER_PHOTO, MANUAL_CROP], ids=lambda p: p.stem)
def test_sidecar_records_the_three_levels(client, db_path, sample) -> None:
    """D2b：sidecar 三个亮度字段都是 int，且 `delta == paper_level - edge_level`。"""
    scan_id = upload(client, sample)
    _, stored = load_scan(db_path, scan_id)

    record = json.loads(
        stored.with_suffix(scan_service.CROP_RECORD_SUFFIX).read_text(encoding="utf-8")
    )
    for key in ("paper_level", "edge_level", "background_delta"):
        assert isinstance(record[key], int) and not isinstance(record[key], bool), (
            f"{sample.name} 的 {key} 不是 int：{record.get(key)!r}"
        )
    assert 0 <= record["paper_level"] <= 255
    assert 0 <= record["edge_level"] <= 255
    assert record["background_delta"] == record["paper_level"] - record["edge_level"]

    payload = crop_info(client, scan_id)
    assert payload["paper_level"] == record["paper_level"]
    assert payload["edge_level"] == record["edge_level"]
    assert payload["background_delta"] == record["background_delta"]


def test_the_page_facts_match_the_sidecar(client) -> None:
    """D2b：数据条上的数字与接口同值（页面不另算一份）。"""
    scan_id = upload(client, HI_REDBG)
    payload = crop_info(client, scan_id)
    html = client.get(f"/scans/{scan_id}/crop").text

    assert f"纸占 {round(payload['area_ratio'] * 100)}%" in html
    assert f"+{payload['background_delta']}" in html
    assert "4/4" in html


# ---------------------------------------------------------------- AC3：四角


def test_corners_are_original_pixels_in_clockwise_order(client) -> None:
    """AC3：四角在原图坐标里、在原图范围内、按 tl→tr→br→bl 顺时针。"""
    scan_id = upload(client, HI_REDBG)

    payload = crop_info(client, scan_id)
    corners = payload["corners"]
    width, height = payload["width"], payload["height"]

    assert (width, height) == (1098, 2560)
    assert corners is not None and len(corners) == 4
    assert all(isinstance(value, int) for point in corners for value in point)

    # 全在原图范围内
    assert all(0 <= x <= width for x, _ in corners)
    assert all(0 <= y <= height for _, y in corners)

    # 顺时针（y 向下的图像坐标里，正面积 = 顺时针）
    assert signed_area(corners) > 0

    # tl 的 x+y 最小、br 的最大：就是 tl→tr→br→bl 那个顺序
    sums = [x + y for x, y in corners]
    assert sums[0] == min(sums)
    assert sums[2] == max(sums)
    # 上边两个点在上面、下边两个点在下面
    assert corners[0][1] <= corners[3][1] + 1 and corners[1][1] <= corners[2][1] + 1


def test_corners_reproduce_the_area_ratio(client) -> None:
    """AC3：用四角算出的面积比 ≈0.445（容差 0.02），且与 sidecar 里那个数一致。"""
    scan_id = upload(client, HI_REDBG)

    payload = crop_info(client, scan_id)
    ratio = bbox_ratio(payload["corners"], payload["width"], payload["height"])

    assert ratio == pytest.approx(0.445, abs=0.02)
    assert payload["area_ratio"] == pytest.approx(ratio, abs=5e-3)


def test_corners_are_not_the_cropped_page_size(client, db_path) -> None:
    """AC3：四角配的是原图尺寸，不是 `crop.jpg`（2000x1493）那套。

    裁完再按长边缩过、还摆了 90°——拿它当参考尺寸，框会整个跑到画面外。
    """
    scan_id = upload(client, HI_REDBG)
    _, stored = load_scan(db_path, scan_id)
    with Image.open(crop_file_for(stored)) as cropped:
        crop_size = cropped.size

    payload = crop_info(client, scan_id)
    corners = payload["corners"]

    assert crop_size == (2000, 1493)
    assert (payload["width"], payload["height"]) == (1098, 2560)
    assert max(max(x for x, _ in corners), max(y for _, y in corners)) > crop_size[1]


# ---------------------------------------------------------------- AC6：原图不动


def test_upload_never_touches_the_original_bytes(client, db_path) -> None:
    """AC6：上传后落盘的原图 sha256 与上传前一致（回归守卫：本卡不碰原图）。"""
    data = HI_REDBG.read_bytes()
    before = hashlib.sha256(data).hexdigest()

    scan_id = upload(client, HI_REDBG)
    _, stored = load_scan(db_path, scan_id)

    assert sha256_file(stored) == before
    assert stored.read_bytes() == data


# ---------------------------------------------------------------- AC4：老卷子 / 404


def test_legacy_scan_without_sidecar_is_200_with_nulls(client, db_path) -> None:
    """AC4：老卷子（没有 sidecar）→ 200，四个结论字段全 null，尺寸退回库里那组。"""
    folder = db_path.parent / "scans" / "2026" / "01"
    folder.mkdir(parents=True, exist_ok=True)
    legacy = folder / "legacy-no-sidecar.jpg"
    legacy.write_bytes(MANUAL_CROP.read_bytes())

    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner_id = session.scalars(select(Learner)).first().id
            scan = Scan(
                learner_id=learner_id,
                device_id="upload",
                device_name="浏览器上传",
                width=2560,
                height=1098,
                path=legacy.relative_to(db_path.parent).as_posix(),
                source="upload",
            )
            session.add(scan)
            session.commit()
            scan_id = scan.id
    finally:
        engine.dispose()

    payload = crop_info(client, scan_id)

    assert payload["reason"] is None
    assert payload["note"] is None
    assert payload["area_ratio"] is None
    assert payload["corners"] is None
    # D2b 的三个亮度字段：老卷子没有，一律 null（不拿 0 冒充读数）
    assert payload["paper_level"] is None
    assert payload["edge_level"] is None
    assert payload["background_delta"] is None
    assert payload["width"] == 2560 and payload["height"] == 1098
    assert payload["has_crop_file"] is False
    assert payload["crop_url"] is None
    assert payload["image_url"] == f"/scans/{scan_id}/image?source=original"


def test_missing_scan_is_404(client) -> None:
    """照片行不存在 → 404（不是 200 带一堆 null）。"""
    assert client.get("/api/scans/999999/crop-info").status_code == 404


# ---------------------------------------------------------------- 文件是否存在


def test_has_crop_file_follows_the_disk(client, db_path) -> None:
    """`has_crop_file` / `crop_url` 按实际有没有那份裁好的图说话。"""
    scan_id = upload(client, HI_REDBG)
    _, stored = load_scan(db_path, scan_id)

    payload = crop_info(client, scan_id)

    assert crop_file_for(stored).is_file() is True
    assert payload["has_crop_file"] is True
    assert payload["crop_url"] == f"/scans/{scan_id}/image?source=crop"
