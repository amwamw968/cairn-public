"""练习页「看原图」卡：扫描进来的题能带图练。

覆盖 AC2–AC5 / AC7：

- AC2：有 `scan_question.crop_path` 的题，练习页出现指向
  `/questions/{question_id}/crop` 的折叠块，标题「看原图（含上次作答）」，
  默认折叠（`<details>` 不带 `open`）。
- AC3：手录题（没有 scan_question / crop_path 为空）页面完全不变——
  没有图、没有原图折叠块、没有「看原图」（导航里的 `<details>` 不算，见
  `_assert_no_original_image`）。
- AC4：揭示屏同样能看原图；answer 为 NULL 时揭示区给
  「这道题还没有标准答案，请对照原图判断」，不出现空白或 `None`。
- AC5：crop_path 有值但文件被删 → 练习页仍 200（图自己的 404 由浏览器处理）。
- AC6：`PracticeItem` 结构上仍然没有 answer 字段（只有 crop_url）。

数据直接落文件库（裁图路径是相对库目录算的），页面用 TestClient。
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn.models import Learner, Question, Scan, make_engine, make_session_factory
from cairn.models.error_item import REVIEW_STATUS_NEW, ErrorItem
from cairn.models.learner import utcnow
from cairn.models.scan import ScanQuestion
from cairn.practice_service import PracticeItem
from cairn.web import create_app

CROP = "scans/2026/11/crops/1-1.jpg"
SCANNED_CONTENT = "求下面图形的面积。"
MANUAL_CONTENT = "12 + 15 = ?"
NO_ANSWER_HINT = "这道题还没有标准答案，请对照原图判断"


@pytest.fixture()
def web(tmp_path):
    """页面库：返回 (client, db_path, 库目录)。"""
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as client:
        yield client, db, tmp_path


def _session(db: Path):
    engine = make_engine(f"sqlite:///{db}")
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


def _add_question(
    db: Path, content: str, *, answer: str | None = "27"
) -> tuple[int, int]:
    """建一道题 + 一条 new 错题（手录题的样子：没有 scan_question 行）。"""
    with _session(db) as session:
        learner_id = session.scalars(select(Learner).limit(1)).first().id
        question = Question(
            learner_id=learner_id,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content=content,
            answer=answer,
        )
        session.add(question)
        session.flush()
        item = ErrorItem(
            learner_id=learner_id,
            question_id=question.id,
            review_status=REVIEW_STATUS_NEW,
            confidence=1.0,
        )
        session.add(item)
        session.commit()
        return item.id, question.id


def _attach_scan_question(
    db: Path,
    root: Path,
    question_id: int,
    *,
    crop_path: str | None = CROP,
    with_file: bool = True,
) -> None:
    """给这道题挂一条切题行（拍照扫描的来源）；`with_file=False` 只写库不落盘。"""
    if crop_path is not None and with_file:
        target = root / crop_path
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (120, 60), "white").save(target, format="JPEG")
    with _session(db) as session:
        learner_id = session.scalars(select(Learner).order_by(Learner.id.asc())).first().id
        scan = Scan(
            learner_id=learner_id,
            device_id="upload",
            device_name="浏览器上传",
            width=1000,
            height=1400,
            captured_at=utcnow(),
            path="scans/2026/11/shot.jpg",
            source="upload",
        )
        session.add(scan)
        session.commit()
        session.add(
            ScanQuestion(
                scan_id=scan.id,
                no=1,
                bbox_json='{"bbox": [0, 0, 120, 60], "page": [1000, 1400], "angle": 0}',
                stem_text=SCANNED_CONTENT,
                is_wrong=True,
                question_id=question_id,
                crop_path=crop_path,
            )
        )
        session.commit()


def _crop_link(question_id: int) -> str:
    return f"/questions/{question_id}/crop"


def _assert_no_original_image(html: str) -> None:
    """没有裁图时，页面上不许出现题目原图。

    `<details>` 本身不再是判据：导航的「更多」就是一个 `<details>`，空壳折叠块
    也是合法的结构。要守的是"图"——没有裁图路径就没有 `src="/questions/…"`；
    G2-5 起 v3 壳自带两张品牌 logo（<img class="brand__logo">），扣掉它们
    整页一张 `<img>` 都不该有，更不能有原图那一块。
    """
    assert 'src="/questions/' not in html
    assert html.count("<img") == html.count('class="brand__logo"')
    assert '<details class="original">' not in html


def test_scanned_question_ask_page_shows_folded_original(web):
    """AC2：扫描题答题面有折叠原图入口，指向 /questions/{id}/crop。"""
    client, db, root = web
    item_id, question_id = _add_question(db, SCANNED_CONTENT)
    _attach_scan_question(db, root, question_id)

    page = client.get("/practice", params={"item_id": item_id})

    assert page.status_code == 200
    assert SCANNED_CONTENT in page.text
    assert _crop_link(question_id) in page.text
    assert "看原图（含上次作答）" in page.text
    # 默认折叠：<details> 不能带 open
    assert '<details class="original">' in page.text
    assert '<details class="original" open' not in page.text
    # 给孩子看的页面上不出现内部术语（URL 里的 /crop 是机器路径，不算文案）
    assert "bbox" not in page.text


def test_manual_question_ask_page_is_unchanged(web):
    """AC3：手录题不出现图、占位或空 <details>。"""
    client, db, _root = web
    item_id, question_id = _add_question(db, MANUAL_CONTENT)

    page = client.get("/practice", params={"item_id": item_id})

    assert page.status_code == 200
    assert MANUAL_CONTENT in page.text
    assert _crop_link(question_id) not in page.text
    assert "/crop" not in page.text
    assert "看原图" not in page.text
    _assert_no_original_image(page.text)


def test_scan_question_with_empty_crop_path_has_no_image(web):
    """AC3：有 scan_question 行但 crop_path 为空（老行/裁图失败）→ 不出图。"""
    client, db, root = web
    item_id, question_id = _add_question(db, SCANNED_CONTENT)
    _attach_scan_question(db, root, question_id, crop_path=None, with_file=False)

    page = client.get("/practice", params={"item_id": item_id})

    assert page.status_code == 200
    assert _crop_link(question_id) not in page.text
    _assert_no_original_image(page.text)


def test_reveal_shows_original_for_scanned_question(web):
    """AC4：揭示屏同样能看原图。"""
    client, db, root = web
    item_id, question_id = _add_question(db, SCANNED_CONTENT)
    _attach_scan_question(db, root, question_id)

    page = client.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "24 平方厘米"}
    )

    assert page.status_code == 200
    assert _crop_link(question_id) in page.text
    assert "看原图（含上次作答）" in page.text
    assert '<details class="original">' in page.text


def test_reveal_manual_question_has_no_image(web):
    """AC3/AC4：手录题的揭示屏一个字都不多。"""
    client, db, _root = web
    item_id, question_id = _add_question(db, MANUAL_CONTENT)

    page = client.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "27"}
    )

    assert page.status_code == 200
    assert "标准答案：" in page.text
    assert _crop_link(question_id) not in page.text
    assert "看原图" not in page.text
    _assert_no_original_image(page.text)


def test_reveal_without_answer_asks_to_compare_with_original(web):
    """AC4：answer 为 NULL → 说人话，不显示空白或 None。"""
    client, db, root = web
    item_id, question_id = _add_question(db, SCANNED_CONTENT, answer=None)
    _attach_scan_question(db, root, question_id)

    page = client.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "24"}
    )

    assert page.status_code == 200
    assert NO_ANSWER_HINT in page.text
    assert "None" not in page.text
    assert _crop_link(question_id) in page.text


def test_missing_crop_file_still_renders_page(web):
    """AC5：crop_path 有、文件被删 → 练习页 200；图自己的 404 交给浏览器。"""
    client, db, root = web
    item_id, question_id = _add_question(db, SCANNED_CONTENT)
    _attach_scan_question(db, root, question_id, with_file=False)

    page = client.get("/practice", params={"item_id": item_id})
    assert page.status_code == 200
    assert _crop_link(question_id) in page.text

    image = client.get(_crop_link(question_id))
    assert image.status_code == 404


def test_practice_item_has_crop_url_and_still_no_answer():
    """AC6：结构上仍然没有 answer；新增字段只有 crop_url。"""
    names = {f.name for f in fields(PracticeItem)}
    assert "crop_url" in names
    assert "answer" not in names
