"""错题列表 → 题块详情入口（卡：错题列表 → 题块详情入口）。

上一张卡（题块裁图 v17）做完了 `GET /questions/{id}` 与 `/questions/{id}/crop`，
但没有任何模板链到它，只能手敲 URL。本文件钉住三件事：

- AC2：复习队列每一行（待复核 + 缺变式卡住）都有「看原图」链接，指向
  `/questions/{question_id}`；没有 `crop_path` 的题链接照常在，点进去是
  200 + 「暂无原图」，不是 500。
- AC3：列表页 HTML 里 `/questions/{id}` 链接数量 == 该页显示的题目数
  （含 `?subject=` 过滤后的页）。
- 有裁图的题：这个链接真的能走到图（详情页出 `<img>`，`/crop` 回 JPEG）。

数据用服务层建（`create_error_item` + 一条 `scan_question`），页面用 TestClient。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn.entry_service import EntryPayload, create_error_item
from cairn.models import (
    REVIEW_STATUS_NEEDS_VARIANT,
    ErrorItem,
    Learner,
    Scan,
    make_engine,
    make_session_factory,
)
from cairn.models.learner import utcnow
from cairn.models.scan import ScanQuestion
from cairn.web import create_app

CROP = "scans/2026/11/crops/1-1.jpg"


class _StubVerifier:
    """固定置信度 0.5 → 进待复核队列（< 0.9 且没复核过）。"""

    def verify(self, payload: EntryPayload) -> float:
        return 0.5


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


def _payload(content: str, *, subject: str = "数学") -> EntryPayload:
    return EntryPayload(
        subject=subject, question_type="计算", content=content, answer="27"
    )


def _add_doubtful(db: Path, content: str, *, subject: str = "数学") -> tuple[int, int]:
    """建一条低置信度错题（进待复核队列）；返回 (error_item_id, question_id)。"""
    with _session(db) as session:
        learner_id = session.scalars(select(Learner).limit(1)).first().id
        item = create_error_item(
            session,
            learner_id,
            _payload(content, subject=subject),
            verify_entry=True,
            entry_verifier=_StubVerifier(),
        )
        return item.id, item.question_id


def _park_needs_variant(db: Path, content: str) -> tuple[int, int]:
    """建一条错题并挂进 needs_variant（缺变式卡住那一段）。

    顺手写上 `last_reviewed_at`：needs_variant 的本义是「已经复习过几轮」，
    待复核队列只收没复习过的，不写这一笔同一道题会在两段里各出现一次。
    """
    item_id, question_id = _add_doubtful(db, content)
    with _session(db) as session:
        item = session.get(ErrorItem, item_id)
        item.review_status = REVIEW_STATUS_NEEDS_VARIANT
        item.last_reviewed_at = utcnow()
        session.commit()
    return item_id, question_id


def _attach_crop(db: Path, root: Path, question_id: int, *, with_file: bool = True) -> None:
    """给题挂一条带裁图的切题行；`with_file=False` 只写库不落文件（模拟图丢了）。"""
    target = root / CROP
    target.parent.mkdir(parents=True, exist_ok=True)
    if with_file:
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
                stem_text="",
                is_wrong=True,
                question_id=question_id,
                crop_path=CROP,
            )
        )
        session.commit()


def _link(question_id: int) -> str:
    return f'href="/questions/{question_id}"'


def test_queue_links_every_question_once(web):
    """G2-4 升级：一屏一题——焦点题两条链接（改一下/看原图），还在等的各一条看原图。

    旧断言锁「每题恰好一条链接」；一屏一题后排队的题只列题干不带链接
    （确认完一道自然升上来），可达性不减、入口不重复。
    """
    client, db, _root = web
    pending = [_add_doubtful(db, f"pending-{i}") for i in range(3)]
    blocked = [_park_needs_variant(db, "blocked-1")]

    html = client.get("/review-queue").text

    focus_question_id = pending[0][1]
    assert _link(focus_question_id) in html, "焦点题要有「看原图」"
    assert f'href="/questions/{focus_question_id}/edit?back=queue"' in html
    # 排队中的第二三题只列题干；还在等的那条有一条看原图
    assert _link(pending[1][1]) not in html
    assert _link(pending[2][1]) not in html
    assert _link(blocked[0][1]) in html
    # 链接总数 = 焦点 2（改一下 + 看原图）+ 还在等的 1
    assert html.count('href="/questions/') == 3


def test_subject_filter_keeps_link_count_in_step(web):
    """`?subject=` 过滤后，链接跟着页面上剩下的题目走。"""
    client, db, _root = web
    _add_doubtful(db, "数学题一", subject="数学")
    english_item_id, english_question_id = _add_doubtful(db, "english-one", subject="英语")

    html = client.get("/review-queue", params={"subject": "英语"}).text

    assert f'href="/questions/{english_question_id}/edit?back=queue"' in html
    assert _link(english_question_id) in html
    assert html.count('href="/questions/') == 2
    assert "数学题一" not in html
    # 过滤只截页面，不删数据
    with _session(db) as session:
        assert session.get(ErrorItem, english_item_id) is not None


def test_question_without_crop_keeps_link_and_says_no_image(web):
    """AC2：没有 crop_path 的题链接照常在，点进去 200 + 「暂无原图」，不是 500。"""
    client, db, _root = web
    _item_id, question_id = _add_doubtful(db, "没有原图的题")

    html = client.get("/review-queue").text
    assert _link(question_id) in html

    page = client.get(f"/questions/{question_id}")
    assert page.status_code == 200
    assert "暂无原图" in page.text
    assert f"/questions/{question_id}/crop" not in page.text


def test_question_with_lost_crop_file_still_renders(web):
    """crop_path 有、文件不在了（老数据/被删）：链接在，详情页照样 200。"""
    client, db, root = web
    _item_id, question_id = _add_doubtful(db, "图丢了的题")
    _attach_crop(db, root, question_id, with_file=False)

    assert _link(question_id) in client.get("/review-queue").text

    page = client.get(f"/questions/{question_id}")
    assert page.status_code == 200
    assert "暂无原图" in page.text


def test_link_leads_to_the_original_crop(web):
    """有裁图的题：列表链接 → 详情页出 <img>，`/crop` 回 JPEG。"""
    client, db, root = web
    _item_id, question_id = _add_doubtful(db, "有原图的题")
    _attach_crop(db, root, question_id)

    assert _link(question_id) in client.get("/review-queue").text

    page = client.get(f"/questions/{question_id}")
    assert page.status_code == 200
    assert f"/questions/{question_id}/crop" in page.text
    assert "暂无原图" not in page.text

    image = client.get(f"/questions/{question_id}/crop")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert image.content[:2] == b"\xff\xd8"


def test_every_queue_link_resolves(web):
    """列表页上的每个链接逐一点开都是 200（不是 404，也不是 500）。"""
    client, db, _root = web
    ids = [_add_doubtful(db, f"q-{i}")[1] for i in range(2)]

    html = client.get("/review-queue").text
    assert html.count('href="/questions/') == len(ids)

    for question_id in ids:
        assert client.get(f"/questions/{question_id}").status_code == 200


def test_queue_rows_do_not_leak_answers(web):
    """详情页入口不改变答题面：列表 HTML 里只有题干，没有答案列。"""
    client, db, _root = web
    _item_id, question_id = _add_doubtful(db, "三加二十四等于多少？")

    html = client.get("/review-queue").text

    assert "三加二十四等于多少？" in html
    assert _link(question_id) in html
    # 答案 27 只该出现在详情页，不该在列表页
    assert ">27<" not in html
