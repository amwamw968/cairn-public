"""家长自己框题卡（2026-11-01）：不依赖自动切题，在摆正后的整页图上自己拖框收进去。

覆盖 AC2–AC8 与 AC10/AC11（AC9 变异自证写在 docs/EVIDENCE.md）：

- AC2 入口：标错页上有「切得不对？我自己框」，点进去是 `GET /scans/{id}/frame`（200）；
- AC3 底图：`/scans/{id}/upright` 在 0° 时**原样**发原图（字节一致），侧拍时换成
  摆正后的尺寸；页面里有拖框区域；
- AC4 收进去：N 个框 → `scan_question` +N（is_wrong=1、no=1..N、裁图存在且尺寸对）+
  `question` +N（source='scan'、answer 空、learner 正确）+ `error_item` +N；
- AC5 小框：任一边 < 30px 整批退回，一条都不收进去，页面给人话；
- AC6 题干：只对框内小块 OCR；识别失败 → content 空串，照样收进去；
- AC7 重复提交：同一批框再提交不重复建题（判据是框坐标，不是题号）；
- AC8 不依赖切题：一行切题结果都没有的页，frame 模式照常可用；
- AC10/AC11 共存：已有自动切题 `no=1..K` 时，手框从 `K+1` 起连续编号；
  同批框重复提交幂等；编号被并发占用时报错而不是静默零插入。

真 OCR 一律不打：`_FakeOcr` 顶替 `cairn.ocr.recognize`，既快又能断言"只认了框内那块"。
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn import ocr, scan_service
from cairn.models import (
    ErrorItem,
    Learner,
    Question,
    Scan,
    make_engine,
    make_session_factory,
)
from cairn.models.learner import utcnow
from cairn.ocr.cut import QuestionBlock
from cairn.ocr.engine import OcrError, OcrLine
from cairn.web import create_app

WIDTH, HEIGHT = 900, 1200
SHOT = "scans/2026/11/frame.jpg"


class _FakeOcr:
    """顶替整页 OCR：记录被识别的文件，返回一段固定文字（或按开关报错）。"""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.text = "1. 计算 3 + 4 ="
        self.fail = False

    def __call__(self, path: str) -> list[OcrLine]:
        self.paths.append(str(path))
        if self.fail:
            raise OcrError("引擎没装")
        return [
            OcrLine(
                text=self.text,
                box=((0, 0), (40, 0), (40, 12), (0, 12)),
                confidence=0.95,
                y_center=6,
            )
        ]


@pytest.fixture()
def fake_ocr(monkeypatch):
    fake = _FakeOcr()
    monkeypatch.setattr(ocr, "recognize", fake)
    return fake


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """真文件库 + 一张能解码的整页图（走真实裁图路径，不碰 OCR）。

    `CAIRN_OCR_DISABLE=1` 让「没有切题行时现判方向」这一步立刻返回 0°，
    测试不会去加载 OCR 模型；框内识别由 `fake_ocr` 单独顶替。
    """
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    db = tmp_path / "frame.db"
    target = tmp_path / SHOT
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (WIDTH, HEIGHT), "white").save(target, format="JPEG", quality=95)
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as c:
        yield c, db


def _seed(db_path: Path, *, angle: int = 0, rows: bool = True) -> int:
    """在 app 的库里落一张照片（可选：先落一条自动切题行，省掉方向判定 OCR）。"""
    page_width, page_height = (HEIGHT, WIDTH) if angle else (WIDTH, HEIGHT)
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
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
            if rows:
                scan_service.store_questions(
                    session,
                    scan.id,
                    [QuestionBlock(no=1, bbox=(40, 40, 860, 200), stem_text="1. 旧题", line_indexes=[0])],
                    page_width=page_width,
                    page_height=page_height,
                    angle=angle,
                )
            return scan.id
    finally:
        engine.dispose()


def _count(db_path: Path, table: str) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return int(con.execute(f"select count(*) from {table}").fetchone()[0])
    finally:
        con.close()


def _rows(db_path: Path) -> list[tuple]:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(
            "select no, is_wrong, bbox_json, stem_text, crop_path, question_id"
            " from scan_question order by no"
        ).fetchall()
    finally:
        con.close()


def _questions(db_path: Path) -> list[tuple]:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(
            "select id, learner_id, source, content, answer from question order by id"
        ).fetchall()
    finally:
        con.close()


def _learner_id(db_path: Path) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return int(con.execute("select id from learner order by id limit 1").fetchone()[0])
    finally:
        con.close()


def _crop_path(db_path: Path, scan_id: int, no: int) -> Path:
    return db_path.parent / "scans" / "2026" / "11" / "crops" / f"{scan_id}-{no}.jpg"


# ---------------------------------------------------------------- AC2 入口


def test_questions_page_offers_manual_frame_entry(client, fake_ocr):
    """AC2：标错页有入口，点进去是 frame 页且返回 200（有拖框区域）。"""
    c, db = client
    scan_id = _seed(db)

    body = c.get(f"/scans/{scan_id}/questions").text
    assert "切得不对？我自己框" in body
    assert f'href="/scans/{scan_id}/frame"' in body

    page = c.get(f"/scans/{scan_id}/frame")
    assert page.status_code == 200
    assert 'id="stage"' in page.text
    # 拖框用原生 pointer events（不引任何前端框架/依赖）
    assert "pointerdown" in page.text


def test_frame_page_404_for_unknown_scan(client, fake_ocr):
    """别的孩子的/不存在的照片一律 404。"""
    c, _db = client
    assert c.get("/scans/9999/frame").status_code == 404
    assert c.post("/scans/9999/frame", data={"frame_0": "1,1,100,100"}).status_code == 404


# ---------------------------------------------------------------- AC3 摆正后的底图


def test_upright_endpoint_is_byte_identical_at_zero(client, fake_ocr):
    """AC3：角度 0 时原样发原图，不二次重采样/重编码（字节一致）。"""
    c, db = client
    scan_id = _seed(db)
    original = (db.parent / SHOT).read_bytes()

    resp = c.get(f"/scans/{scan_id}/upright")

    assert resp.status_code == 200
    assert resp.content == original
    # frame 页拿的就是这张图
    assert f'src="/scans/{scan_id}/upright"' in c.get(f"/scans/{scan_id}/frame").text


def test_upright_endpoint_rotates_sideways_page(client, fake_ocr):
    """AC3：侧拍（90°）时底图是摆正后的尺寸（900×1200 → 1200×900）。"""
    c, db = client
    scan_id = _seed(db, angle=90)

    resp = c.get(f"/scans/{scan_id}/upright")

    assert resp.status_code == 200
    with Image.open(io.BytesIO(resp.content)) as image:
        assert image.size == (HEIGHT, WIDTH)
    # 页面把旋转角说清楚（人话，不出现内部术语）
    assert "转了 90°" in c.get(f"/scans/{scan_id}/frame").text


# ---------------------------------------------------------------- AC4 收进去


def test_submit_frames_creates_rows_questions_and_items(client, fake_ocr):
    """AC4：两个框 → 两条切题行 + 两道题 + 两条错题记录，裁图尺寸等于框。"""
    c, db = client
    scan_id = _seed(db, rows=False)
    learner_id = _learner_id(db)

    resp = c.post(
        f"/scans/{scan_id}/frame",
        data={
            "frame_0": "60,80,540,300",
            "frame_1": "60,340,540,620",
            "subject": "数学",
            "question_type": "计算",
        },
    )

    assert resp.status_code == 200
    assert "已收好 2 题" in resp.text

    rows = _rows(db)
    assert [row[0] for row in rows] == [1, 2]
    assert all(row[1] == 1 for row in rows)  # is_wrong
    assert all(row[5] is not None for row in rows)  # 回填 question_id
    for row, expect in zip(rows, [(480, 220), (480, 280)]):
        crop = db.parent / row[4]
        assert crop.is_file()
        with Image.open(crop) as image:
            assert abs(image.width - expect[0]) <= 1
            assert abs(image.height - expect[1]) <= 1
        assert isinstance(row[3], str) and row[3]

    payload = json.loads(rows[0][2])
    assert payload["page"] == [WIDTH, HEIGHT] and payload["angle"] == 0
    assert payload["bbox"] == [60, 80, 540, 300]

    questions = _questions(db)
    assert len(questions) == 2
    assert all(q[1] == learner_id for q in questions)
    assert all(q[2] == "scan" for q in questions)
    assert all(q[4] is None for q in questions)  # answer 空
    assert all(q[3] == fake_ocr.text for q in questions)  # 题干来自框内 OCR

    assert _count(db, "error_item") == 2


def test_local_ocr_only_reads_the_frames(client, fake_ocr):
    """AC6：只对框内小块跑识别——识别的是刚裁出来的那块，不是整页照片。"""
    c, db = client
    scan_id = _seed(db, rows=False)

    c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,540,300", "frame_1": "60,340,540,620"},
    )

    crops = (db.parent / "scans" / "2026" / "11" / "crops").resolve()
    assert len(fake_ocr.paths) == 2
    for path in fake_ocr.paths:
        resolved = Path(path).resolve()
        assert resolved.parent == crops
        assert resolved.name in {f"{scan_id}-1.jpg", f"{scan_id}-2.jpg"}
    # 整页照片本身一次都没进识别器
    assert str((db.parent / SHOT).resolve()) not in [str(Path(p).resolve()) for p in fake_ocr.paths]


def test_ocr_failure_does_not_block_import(client, fake_ocr):
    """AC6：识别失败 → 题干存空串，题照样收进去，页面提示"没认出文字"。"""
    c, db = client
    scan_id = _seed(db, rows=False)
    fake_ocr.fail = True

    resp = c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,540,300"},
    )

    assert resp.status_code == 200
    assert "已收好 1 题" in resp.text
    assert "没认出文字" in resp.text

    questions = _questions(db)
    assert len(questions) == 1
    assert questions[0][3] == ""  # content 空串，不是 NULL
    assert _rows(db)[0][1] == 1


# ---------------------------------------------------------------- AC5 小框不收


def test_too_small_frame_rejects_whole_batch(client, fake_ocr):
    """AC5：一个框任一边 < 30px → 整批退回，一条都不收进去，页面说人话。"""
    c, db = client
    scan_id = _seed(db, rows=False)

    resp = c.post(
        f"/scans/{scan_id}/frame",
        data={
            "frame_0": "60,80,540,300",  # 好好的一个框
            "frame_1": "60,340,80,360",  # 20×20，太小
        },
    )

    assert resp.status_code == 200
    assert "太小" in resp.text
    assert "一道都没收" in resp.text
    assert _rows(db) == []
    assert _questions(db) == []
    assert _count(db, "error_item") == 0
    # 一个字节都不该落盘：边角框也是
    assert not _crop_path(db, scan_id, 1).exists()


def test_empty_submit_imports_nothing(client, fake_ocr):
    """一个框都没有：页面提示怎么框，不建任何东西。"""
    c, db = client
    scan_id = _seed(db, rows=False)

    resp = c.post(f"/scans/{scan_id}/frame", data={})

    assert resp.status_code == 200
    assert "一张都没框到" in resp.text
    assert _rows(db) == []


# ---------------------------------------------------------------- AC7 重复提交


def test_repeat_submit_skips_existing_rows(client, fake_ocr):
    """AC7：同一批框再提交一次 → 不重复建题，页面说"没有重复建题"。"""
    c, db = client
    scan_id = _seed(db, rows=False)
    data = {"frame_0": "60,80,540,300", "frame_1": "60,340,540,620"}

    first = c.post(f"/scans/{scan_id}/frame", data=data)
    assert "已收好 2 题" in first.text
    questions_after_first = len(_questions(db))

    second = c.post(f"/scans/{scan_id}/frame", data=data)

    assert second.status_code == 200
    assert "没有重复建题" in second.text
    assert len(_questions(db)) == questions_after_first
    assert _count(db, "error_item") == 2
    assert [row[0] for row in _rows(db)] == [1, 2]


# ---------------------------------------------------------------- AC8 不依赖切题


def test_frame_mode_works_without_any_cut_rows(client, monkeypatch):
    """AC8：自动切题一行都没落（0 题）时，frame 模式照常打开、照常收进去。

    OCR 整个禁用：方向判定用不了就按不转处理，这条入口不许被它挡住。
    """
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    c, db = client
    scan_id = _seed(db, rows=False)
    assert _count(db, "scan_question") == 0

    page = c.get(f"/scans/{scan_id}/frame")
    assert page.status_code == 200
    assert "切得不对？我自己框" in c.get(f"/scans/{scan_id}/questions").text

    resp = c.post(f"/scans/{scan_id}/frame", data={"frame_0": "60,80,540,300"})

    assert resp.status_code == 200
    assert "已收好 1 题" in resp.text
    assert len(_rows(db)) == 1
    assert len(_questions(db)) == 1


# ---------------------------------------------------------------- 服务层：全成或全不动


def test_store_manual_frames_rolls_back_and_deletes_crops(
    session, learner, tmp_path, monkeypatch
):
    """事务内全成或全不动：半路抛异常 → 行、题、错题记录、刚写的裁图全部退回。"""
    monkeypatch.setattr(ocr, "recognize", lambda path: [])
    db_url = f"sqlite:///{tmp_path / 'rollback.db'}"
    scan = Scan(
        learner_id=learner.id,
        device_id="upload",
        device_name="浏览器上传",
        width=WIDTH,
        height=HEIGHT,
        captured_at=utcnow(),
        path=f"scans/2026/11/{tmp_path.name}.jpg",
        source="upload",
    )
    session.add(scan)
    session.commit()

    page_image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    crops = tmp_path / "scans" / "2026" / "11" / "crops"

    with pytest.raises(ValueError):
        # 第一块正常，第二块整块在页外：裁第二块时抛错，第一块的兜儿必须全收回去
        scan_service.store_manual_frames(
            session,
            learner.id,
            scan,
            [(10, 10, 200, 200), (WIDTH + 50, HEIGHT + 50, WIDTH + 400, HEIGHT + 400)],
            page_width=WIDTH,
            page_height=HEIGHT,
            angle=0,
            page_image=page_image,
            db_url=db_url,
        )

    session.expire_all()
    assert scan_service.list_questions(session, scan.id) == []
    assert session.scalars(select(Question.id)).all() == []
    assert session.scalars(select(ErrorItem.id)).all() == []
    assert not (crops / f"{scan.id}-1.jpg").exists()  # 第一块的裁图被删掉了


# ---------------------------------------------------------------- AC10/AC11 与自动切题共存


def _seed_auto_rows(db_path: Path, nos: list[int]) -> int:
    """先落自动切题留下的题行（题号按 `nos`），模拟线上"每张上传都先切题"。"""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
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
            scan_service.store_questions(
                session,
                scan.id,
                [
                    QuestionBlock(
                        no=no,
                        bbox=(40, 40 + index * 200, 860, 190 + index * 200),
                        stem_text=f"{no}. 自动切题留下的题",
                        line_indexes=[index],
                    )
                    for index, no in enumerate(nos)
                ],
                page_width=WIDTH,
                page_height=HEIGHT,
            )
            return scan.id
    finally:
        engine.dispose()


def test_manual_frames_number_after_auto_rows(client, fake_ocr):
    """AC11：已有 no=1..3 时，框 2 道 → 新增 no=4,5，question +2，不再整批跳过。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 2, 3])
    assert [row[0] for row in _rows(db)] == [1, 2, 3]

    resp = c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,540,300", "frame_1": "60,340,540,620"},
    )

    assert resp.status_code == 200
    assert "已收好 2 题" in resp.text
    assert "之前已经在这张卷子里了" not in resp.text

    rows = _rows(db)
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
    for row, expect in zip(rows[3:], [(480, 220), (480, 280)]):
        assert row[1] == 1  # is_wrong
        assert row[5] is not None  # question_id 回填
        crop = db.parent / row[4]
        assert crop.is_file()
        with Image.open(crop) as image:
            assert abs(image.width - expect[0]) <= 1
            assert abs(image.height - expect[1]) <= 1

    assert len(_questions(db)) == 2
    assert _count(db, "error_item") == 2


def test_manual_frames_repeat_after_auto_rows_adds_nothing(client, fake_ocr):
    """AC11：同一批框再提交一次 → 不新建题、不报错，页面说之前提交过。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 2, 3])
    data = {"frame_0": "60,80,540,300", "frame_1": "60,340,540,620"}
    assert "已收好 2 题" in c.post(f"/scans/{scan_id}/frame", data=data).text
    crops_before = sorted((db.parent / "scans" / "2026" / "11" / "crops").iterdir())

    second = c.post(f"/scans/{scan_id}/frame", data=data)

    assert second.status_code == 200
    assert "没有重复建题" in second.text
    assert "已收好" not in second.text
    assert [row[0] for row in _rows(db)] == [1, 2, 3, 4, 5]
    assert len(_questions(db)) == 2
    assert _count(db, "error_item") == 2
    # 幂等路径不该留下新的裁图（也没有覆盖旧图）
    assert sorted((db.parent / "scans" / "2026" / "11" / "crops").iterdir()) == crops_before


def test_manual_frames_duplicates_matched_by_box_not_submission_order(client, fake_ocr):
    """幂等判据是框坐标：同一批框换个顺序提交，仍然一块都不新建。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 2, 3])
    c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,540,300", "frame_1": "60,340,540,620"},
    )

    reversed_batch = c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,340,540,620", "frame_1": "60,80,540,300"},
    )

    assert "没有重复建题" in reversed_batch.text
    assert [row[0] for row in _rows(db)] == [1, 2, 3, 4, 5]
    assert len(_questions(db)) == 2


def test_manual_frames_zero_insert_copy_is_about_duplicates(client, fake_ocr):
    """零插入时页面说的是「之前提交过」，不是编号撞车。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 2, 3])
    data = {"frame_0": "60,80,540,300"}
    c.post(f"/scans/{scan_id}/frame", data=data)

    resp = c.post(f"/scans/{scan_id}/frame", data=data)

    assert "之前已经在这张卷子里了" in resp.text
    assert "没有重复建题" in resp.text
    assert "撞" not in resp.text
    assert 'class="error"' not in resp.text


def test_manual_frames_partial_overlap_creates_only_new_box(client, fake_ocr):
    """旧框 + 新框一起提交：只给新框建题，编号接着上一次的最大号。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 2, 3])
    c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,540,300", "frame_1": "60,340,540,620"},
    )

    resp = c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,540,300", "frame_1": "60,660,540,900"},
    )

    assert "已收好 1 题" in resp.text
    assert "另有 1 块之前已经在这张卷子里了" in resp.text
    assert [row[0] for row in _rows(db)] == [1, 2, 3, 4, 5, 6]
    assert len(_questions(db)) == 3


def test_manual_frames_dedupes_same_box_twice_in_one_batch(client, fake_ocr):
    """同一批里同一块框画了两遍：只算一道题，编号不跳号。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 2, 3])

    resp = c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,540,300", "frame_1": "60,80,540,300"},
    )

    assert "已收好 1 题" in resp.text
    assert "另有 1 块之前已经在这张卷子里了" in resp.text
    assert [row[0] for row in _rows(db)] == [1, 2, 3, 4]
    assert len(_questions(db)) == 1


def test_manual_frames_collision_is_loud_not_silent(client, fake_ocr, monkeypatch):
    """编号被另一批框占了：报错整批退回，不许伪装成「之前提交过」。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 2, 3])
    # 模拟并发：本次读到的是空表，但库里 1..3 已经被占了
    monkeypatch.setattr(scan_service, "list_questions", lambda session, scan_id: [])

    resp = c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,540,300", "frame_1": "60,340,540,620"},
    )

    assert resp.status_code == 200
    assert "撞上了" in resp.text
    assert "没有重复建题" not in resp.text
    assert [row[0] for row in _rows(db)] == [1, 2, 3]
    assert _questions(db) == []
    assert not _crop_path(db, scan_id, 1).exists()
    assert not _crop_path(db, scan_id, 2).exists()


def test_manual_frames_leave_auto_rows_untouched(client, fake_ocr):
    """手框只往后追加：自动切题那 1..3 行原样不动（框、题干、归属都不改）。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 2, 3])
    before = _rows(db)

    c.post(f"/scans/{scan_id}/frame", data={"frame_0": "60,80,540,300"})

    after = _rows(db)
    assert after[:3] == before
    assert [row[0] for row in after] == [1, 2, 3, 4]


def test_manual_frames_numbering_starts_after_max_with_gaps(client, fake_ocr):
    """已有 no=1,3（有空洞）也不回填 2：新号取 max+1，避免和旧号混淆。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 3])

    resp = c.post(f"/scans/{scan_id}/frame", data={"frame_0": "60,80,540,300"})

    assert "已收好 1 题" in resp.text
    assert [row[0] for row in _rows(db)] == [1, 3, 4]


def test_manual_frames_second_batch_continues_numbering(client, fake_ocr):
    """第二批新框接着上一批手框的号往下编（4,5 → 6），不回到 K+1 重来。"""
    c, db = client
    scan_id = _seed_auto_rows(db, [1, 2, 3])
    c.post(
        f"/scans/{scan_id}/frame",
        data={"frame_0": "60,80,540,300", "frame_1": "60,340,540,620"},
    )

    resp = c.post(f"/scans/{scan_id}/frame", data={"frame_0": "60,660,540,900"})

    assert "已收好 1 题" in resp.text
    assert [row[0] for row in _rows(db)] == [1, 2, 3, 4, 5, 6]
    assert len(_questions(db)) == 3


def _service_scan_with_auto_rows(session, learner):
    scan = Scan(
        learner_id=learner.id,
        device_id="upload",
        device_name="浏览器上传",
        width=WIDTH,
        height=HEIGHT,
        captured_at=utcnow(),
        path="scans/2026/11/service-frame.jpg",
        source="upload",
    )
    session.add(scan)
    session.commit()
    scan_service.store_questions(
        session,
        scan.id,
        [
            QuestionBlock(
                no=no,
                bbox=(40, 40 + index * 200, 860, 190 + index * 200),
                stem_text=f"{no}. 自动切题留下的题",
                line_indexes=[index],
            )
            for index, no in enumerate((1, 2, 3))
        ],
        page_width=WIDTH,
        page_height=HEIGHT,
    )
    return scan


def test_store_manual_frames_reports_numbers_after_auto_rows(
    session, learner, tmp_path, fake_ocr
):
    """服务层直接调用也守同一条：created=(4,5)，不是从 1 重编。"""
    db_url = f"sqlite:///{tmp_path / 'service.db'}"
    scan = _service_scan_with_auto_rows(session, learner)
    page_image = Image.new("RGB", (WIDTH, HEIGHT), "white")

    result = scan_service.store_manual_frames(
        session,
        learner.id,
        scan,
        [(60, 80, 540, 300), (60, 340, 540, 620)],
        page_width=WIDTH,
        page_height=HEIGHT,
        angle=0,
        page_image=page_image,
        db_url=db_url,
    )

    assert result.created == (4, 5)
    assert result.skipped == ()
    assert [row.no for row in scan_service.list_questions(session, scan.id)] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert len(session.scalars(select(Question.id)).all()) == 2


def test_store_manual_frames_second_call_reports_duplicates(
    session, learner, tmp_path, fake_ocr
):
    """服务层第二次调用：created 为空、skipped 是框在提交顺序里的位置（1,2）。"""
    db_url = f"sqlite:///{tmp_path / 'service-second.db'}"
    scan = _service_scan_with_auto_rows(session, learner)
    frames = [(60, 80, 540, 300), (60, 340, 540, 620)]
    page_image = Image.new("RGB", (WIDTH, HEIGHT), "white")

    def submit():
        return scan_service.store_manual_frames(
            session,
            learner.id,
            scan,
            frames,
            page_width=WIDTH,
            page_height=HEIGHT,
            angle=0,
            page_image=page_image,
            db_url=db_url,
        )

    assert submit().created == (4, 5)
    second = submit()

    assert second.created == ()
    assert second.skipped == (1, 2)
    assert [row.no for row in scan_service.list_questions(session, scan.id)] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert len(session.scalars(select(Question.id)).all()) == 2


def test_store_manual_frames_dedupes_frame_clipped_at_page_edge(
    session, learner, tmp_path, fake_ocr
):
    """越界几像素的框，第二次提交必须零新增（幂等两侧同一把尺子）。

    首交存的是**原始** bbox（右边超出页面 5 像素），重交时新框算的是 clip 后
    四元组；若 taken 只拿原始 bbox 比，两者永远对不上 → 判成新框、重复建题。
    这里锁住：切题行、题目、裁图文件数量第二次都不变。
    """
    db_url = f"sqlite:///{tmp_path / 'edge.db'}"
    scan = _service_scan_with_auto_rows(session, learner)
    page_image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    crops = tmp_path / "scans" / "2026" / "11" / "crops"

    def submit():
        return scan_service.store_manual_frames(
            session,
            learner.id,
            scan,
            [(60, 80, WIDTH + 5, 300)],  # 右边越界 5 像素
            page_width=WIDTH,
            page_height=HEIGHT,
            angle=0,
            page_image=page_image,
            db_url=db_url,
        )

    assert submit().created == (4,)
    crop_files_before = sorted(path.name for path in crops.iterdir())
    second = submit()

    assert second.created == ()
    assert second.skipped == (1,)
    assert [row.no for row in scan_service.list_questions(session, scan.id)] == [
        1,
        2,
        3,
        4,
    ]
    assert len(session.scalars(select(Question.id)).all()) == 1
    assert sorted(path.name for path in crops.iterdir()) == crop_files_before
