"""Cut-correction dataset: table v20, the three endpoints, four rates.

Spec: `docs/CUT-FEEDBACK-SPEC.md`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from cairn.models import CutFeedback, Scan
from cairn.web import create_app

IMAGE_SHA = "a" * 64
META = {
    "image_sha256": IMAGE_SHA,
    "image_w": 1275,
    "image_h": 952,
    "recipe": "scan-entry",
    "params_hash": "params-v1",
}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "feedback.db"


@pytest.fixture()
def client(db_path):
    app = create_app(f"sqlite:///{db_path}")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def scan_id(client) -> int:
    """One uploaded page owned by the default learner."""
    with client.app.state.session_factory() as session:
        scan = Scan(
            learner_id=client.app.state.learner_id,
            device_id="fake:uvc-pro",
            device_name="高拍仪",
            width=META["image_w"],
            height=META["image_h"],
            path="scans/2026/11/a.jpg",
            source="upload",
        )
        session.add(scan)
        session.commit()
        return scan.id


def _frame(key: str, no, action: str, *, auto=None, human=None, source="anchor_digit"):
    return {
        "frame_key": key,
        "no": no,
        "source": source,
        "auto_bbox": auto,
        "human_bbox": human,
        "selected": human is not None,
        "action": action,
    }


def _post(client, scan_id: int, frames, **overrides):
    body = {"scan_id": scan_id, "frames": frames, **META, **overrides}
    return client.post("/api/cut-feedback", json=body)


def _rows(client) -> list[CutFeedback]:
    with client.app.state.session_factory() as session:
        return list(session.scalars(select(CutFeedback).order_by(CutFeedback.id)))


def _row_count(client) -> int:
    with client.app.state.session_factory() as session:
        return int(session.scalar(select(func.count(CutFeedback.id))))


def _auto_frames(total: int) -> list[dict]:
    return [
        _frame(f"a{i}", i, "accept", auto=[10, 20, 600, 90], human=[10, 20, 600, 90])
        for i in range(1, total + 1)
    ]


# ---------------------------------------------------------------- AC3 覆盖写入


def test_post_writes_twelve_rows(client, scan_id):
    frames = _auto_frames(10) + [
        _frame("a11", 11, "delete", auto=[10, 100, 600, 200]),
        _frame("m1", None, "add", human=[10, 300, 600, 400], source="manual"),
    ]
    response = _post(client, scan_id, frames)

    assert response.status_code == 201
    assert response.json() == {"saved": 12}
    assert _row_count(client) == 12


def test_resubmit_overwrites_instead_of_appending(client, scan_id):
    frames = _auto_frames(10) + [
        _frame("a11", 11, "delete", auto=[10, 100, 600, 200]),
        _frame("m1", None, "add", human=[10, 300, 600, 400], source="manual"),
    ]
    assert _post(client, scan_id, frames).status_code == 201

    changed = _auto_frames(10) + [
        _frame("a11", 11, "redraw", auto=[10, 100, 600, 200], human=[10, 110, 600, 210]),
        _frame("m1", None, "add", human=[10, 300, 600, 400], source="manual"),
    ]
    changed[0] = _frame("a1", 1, "redraw", auto=[10, 20, 600, 90], human=[12, 22, 610, 95])
    assert _post(client, scan_id, changed).status_code == 201

    rows = _rows(client)
    assert len(rows) == 12
    by_key = {row.frame_key: row for row in rows}
    assert by_key["a1"].action == "redraw"
    assert by_key["a1"].human_bbox == "12,22,610,95"
    assert by_key["a11"].action == "redraw"
    assert by_key["a11"].human_bbox == "10,110,600,210"


def test_db_rejects_duplicate_scan_frame_key(client, scan_id):
    """The "overwrite, never append" rule also holds at the DB level.

    The service finds and updates the existing row, but a concurrent writer can
    only be stopped by the unique index `(scan_id, frame_key)` — so a raw double
    insert must raise instead of silently creating a second snapshot row.
    """
    with client.app.state.session_factory() as session:
        for _ in range(2):
            session.add(
                CutFeedback(
                    scan_id=scan_id,
                    frame_key="a1",
                    action="accept",
                    source="manual",
                    image_sha256=IMAGE_SHA,
                    image_w=META["image_w"],
                    image_h=META["image_h"],
                    recipe="scan-entry",
                    params_hash="params-v1",
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


def test_repeated_frame_key_inside_one_payload_keeps_last(client, scan_id):
    frames = [
        _frame("dup", 1, "delete", auto=[0, 0, 100, 50]),
        _frame("dup", 1, "accept", auto=[0, 0, 100, 50], human=[0, 0, 100, 50]),
    ]
    assert _post(client, scan_id, frames).json() == {"saved": 1}
    row = _rows(client)[0]
    assert row.action == "accept"
    assert row.human_bbox == "0,0,100,50"


# ---------------------------------------------------------------- AC4 四个率


def _rate_fixture() -> list[dict]:
    """20 automatic frames + 2 manual adds: 18 accept, 1 delete, 1 redraw."""
    frames = _auto_frames(18)
    frames.append(_frame("a19", 19, "delete", auto=[10, 100, 600, 200]))
    frames.append(
        _frame("a20", 20, "redraw", auto=[10, 200, 600, 300], human=[10, 200, 600, 300])
    )
    frames.append(_frame("m1", None, "add", human=[0, 400, 600, 500], source="manual"))
    frames.append(_frame("m2", None, "add", human=[0, 500, 600, 600], source="manual"))
    return frames


def test_stats_four_rates_exact(client, scan_id):
    assert _post(client, scan_id, _rate_fixture()).json() == {"saved": 22}

    body = client.get("/api/cut-feedback/stats").json()

    assert body["pages"] == 1
    assert body["auto_frames"] == 20
    assert body["accepted"] == 18
    assert body["deleted"] == 1
    assert body["redrawn"] == 1
    assert body["added"] == 2
    assert body["accept_rate"] == 0.9
    assert body["delete_rate"] == 0.05
    assert body["redraw_rate"] == 0.05
    assert body["add_rate"] == pytest.approx(2 / 22)
    assert body["mean_iou"] == 1.0
    assert "insufficient" not in body


def test_stats_recipe_filter(client, scan_id):
    assert _post(client, scan_id, _rate_fixture()).status_code == 201
    moved = _auto_frames(30)
    assert (
        _post(client, scan_id, moved, recipe="scan-enhance", params_hash="params-v2")
        .status_code
        == 201
    )

    body = client.get("/api/cut-feedback/stats", params={"recipe": "scan-enhance"}).json()

    assert body["auto_frames"] == 30
    assert body["accept_rate"] == 1.0


# ---------------------------------------------------------------- AC5 样本不足


def test_stats_insufficient_below_twenty_auto_frames(client, scan_id):
    assert _post(client, scan_id, _auto_frames(19)).status_code == 201

    body = client.get("/api/cut-feedback/stats").json()

    assert body["insufficient"] is True
    assert body["auto_frames"] == 19
    assert "accept_rate" not in body
    assert "delete_rate" not in body
    assert "redraw_rate" not in body
    assert "add_rate" not in body


# ---------------------------------------------------------------- AC6 导出


def test_export_defaults_to_no_image_and_matches_row_count(client, scan_id):
    assert _post(client, scan_id, _rate_fixture()).status_code == 201

    response = client.get("/api/cut-feedback/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [line for line in response.text.splitlines() if line]
    assert len(lines) == _row_count(client) == 22
    records = [json.loads(line) for line in lines]
    assert all("image_path" not in record for record in records)
    assert records[0]["scan_id"] == scan_id
    assert records[0]["image_sha256"] == IMAGE_SHA
    assert records[0]["human_bbox"] == [10, 20, 600, 90]


def test_export_with_image_adds_original_path_only(client, scan_id):
    assert _post(client, scan_id, _rate_fixture()).status_code == 201

    response = client.get(
        "/api/cut-feedback/export", params={"include_image": "1"}
    )

    records = [json.loads(line) for line in response.text.splitlines() if line]
    assert len(records) == 22
    assert {record["image_path"] for record in records} == {"scans/2026/11/a.jpg"}


# ---------------------------------------------------------------- AC7 原图坐标


def test_out_of_bounds_human_bbox_is_stored_verbatim(client, scan_id):
    """A box that does not fit the image must not be clipped or "corrected"."""
    frames = [
        _frame("a1", 1, "redraw", auto=[10, 20, 600, 90], human=[0, 0, 4000, 3000])
    ]

    assert _post(client, scan_id, frames).status_code == 201

    row = _rows(client)[0]
    assert row.human_bbox == "0,0,4000,3000"
    assert (row.image_w, row.image_h) == (1275, 952)
    record = json.loads(client.get("/api/cut-feedback/export").text.splitlines()[0])
    assert record["human_bbox"] == [0, 0, 4000, 3000]


def test_iou_is_only_derived_when_both_boxes_exist(client, scan_id):
    frames = [
        _frame("a1", 1, "accept", auto=[0, 0, 10, 10], human=[5, 0, 15, 10]),
        _frame("a2", 2, "delete", auto=[0, 0, 10, 10]),
        _frame("m1", None, "add", human=[0, 0, 10, 10], source="manual"),
    ]
    assert _post(client, scan_id, frames).status_code == 201

    by_key = {row.frame_key: row for row in _rows(client)}
    assert by_key["a1"].iou == pytest.approx(1 / 3)
    assert by_key["a2"].iou is None
    assert by_key["m1"].iou is None


# ---------------------------------------------------------------- 拒绝坏输入


def test_unknown_action_is_rejected(client, scan_id):
    frames = [_frame("a1", 1, "nope", auto=[0, 0, 10, 10])]

    response = _post(client, scan_id, frames)

    assert response.status_code == 400
    assert _row_count(client) == 0


def test_unknown_scan_is_404(client):
    response = _post(client, 999999, _auto_frames(1))

    assert response.status_code == 404


def test_bad_bbox_shape_is_rejected(client, scan_id):
    frames = [_frame("a1", 1, "accept", auto=[0, 0, 10])]

    response = _post(client, scan_id, frames)

    assert response.status_code == 400
    assert _row_count(client) == 0
