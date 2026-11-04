"""卡 W1：一张卷子在整条路上的三个状态（`scan_service.stage`）——只看判定这一层。

这张卡要解决的是"四座孤岛"：裁边页、圈选页、题干页都做好了，但从卷子详情页
没有任何入口能进去。状态只有一个来源（`stage_service` 那个函数），页面只管画按钮。

- **AC2**：没裁过 → `crop`；裁好了没收进去 → `select`；收进去 3 道 → `ready`。
- **AC3**：`human="adjusted"` 但 `reason != "ok"` 算 `select`——人工调整过就往下走，
  不再回头裁。这条最容易写错（把 `reason != "ok"` 当成唯一判据就会退回 crop），
  所以单独一条测试钉住；`human="skipped"` 反例也在这里钉死，别把两个值混成一个。
- **AC5**：卷子不存在（或不是这个孩子的）→ `None`，不拿 `"crop"` 兜底。

页面那一层（三个按钮、列表页「下一步」列、390 宽）在 `test_pages_visual.py`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from cairn import scan_service
from cairn.models import Learner, Question, Scan, init_db, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.models.scan import ScanQuestion

WIDTH, HEIGHT = 900, 1200
SHOT = "scans/2026/11/stage.jpg"


class _Store:
    """一个临时库 + 一张真 JPEG；要哪种状态的卷子就叫一次 `new_scan`。"""

    def __init__(self, db_url: str, factory, root: Path) -> None:
        self.db_url = db_url
        self.factory = factory
        self.root = root

    def _learner(self, session) -> Learner:
        learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
        if learner is None:
            learner = Learner(name="小石")
            session.add(learner)
            session.commit()
        return learner

    def new_scan(
        self,
        *,
        reason: str | None = None,
        human: str | None = None,
        questions: int = 0,
        confirm: bool = False,
    ) -> int:
        """造一张卷子。

        `reason` / `human` 给了就写一条 sidecar（`reason` 不给 = 老照片，没有记录）；
        `questions` 是圈选页收进去的题数（`scan_question.question_id`）；
        `confirm` 是详情页那个"人工确认收好"表单回填的 `scan.question_id`。
        """
        with self.factory() as session:
            learner = self._learner(session)
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
            if reason is not None or human is not None:
                payload: dict[str, object] = {}
                if reason is not None:
                    payload["reason"] = reason
                if human is not None:
                    payload["human"] = human
                payload["area_ratio"] = 0.44
                record = (self.root / SHOT).with_suffix(".crop.json")
                record.write_text(json.dumps(payload), encoding="utf-8")
            for number in range(1, questions + 1):
                question = Question(
                    learner_id=learner.id,
                    subject="数学",
                    grade_level="primary-3",
                    question_type="计算",
                    content=f"{number}. 计算 {number} + {number} =",
                    answer=str(number * 2),
                )
                session.add(question)
                session.commit()
                session.add(
                    ScanQuestion(
                        scan_id=scan.id,
                        no=number,
                        bbox_json=json.dumps(
                            {"bbox": [0, 0, 10, 10], "page": [WIDTH, HEIGHT], "angle": 0}
                        ),
                        stem_text=f"{number}. 计算 {number} + {number} =",
                        is_wrong=True,
                        question_id=question.id,
                    )
                )
            if confirm:
                question = Question(
                    learner_id=learner.id,
                    subject="数学",
                    grade_level="primary-3",
                    question_type="计算",
                    content="人工确认的那一道",
                    answer="1",
                )
                session.add(question)
                session.commit()
                scan.question_id = question.id
            session.commit()
            return scan.id

    def stage(self, scan_id: int, learner_id: int | None = None) -> str | None:
        with self.factory() as session:
            learner = self._learner(session)
            return scan_service.stage(
                session,
                learner.id if learner_id is None else learner_id,
                scan_id,
                self.db_url,
            )

    def new_learner(self) -> int:
        with self.factory() as session:
            learner = Learner(name="另一个孩子")
            session.add(learner)
            session.commit()
            return learner.id

    def count(self, scan_id: int) -> int:
        with self.factory() as session:
            scan = session.get(Scan, scan_id)
            return scan_service.stored_question_count(session, scan)


@pytest.fixture()
def store(tmp_path: Path) -> _Store:
    db_url = f"sqlite:///{tmp_path / 'stage.db'}"
    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)
    image = tmp_path / SHOT
    image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (WIDTH, HEIGHT), "white").save(image, format="JPEG", quality=90)
    try:
        yield _Store(db_url, factory, tmp_path)
    finally:
        engine.dispose()


# ------------------------------------------------------------------ AC2 三种状态


def test_no_crop_record_is_crop(store: _Store):
    """没裁过的老照片（旁挂 json 不存在）→ crop。"""
    scan_id = store.new_scan()

    assert store.stage(scan_id) == scan_service.STAGE_CROP


def test_a_good_crop_without_questions_is_select(store: _Store):
    """机器判 ok、但一道题都没收进去 → select（该去选题了）。"""
    scan_id = store.new_scan(reason="ok")

    assert store.stage(scan_id) == scan_service.STAGE_SELECT


def test_three_questions_means_ready(store: _Store):
    """收进去 3 道 → ready，而且数出来的就是 3。"""
    scan_id = store.new_scan(reason="ok", questions=3)

    assert store.stage(scan_id) == scan_service.STAGE_READY
    assert store.count(scan_id) == 3


def test_a_failed_gate_stays_on_crop(store: _Store):
    """机器判"没裁成"、家长也没拖过四角 → crop（先确认纸边）。"""
    scan_id = store.new_scan(reason="no_paper")

    assert store.stage(scan_id) == scan_service.STAGE_CROP


# ------------------------------------------------------- AC3 人工调整过就往下走


def test_human_adjusted_beats_a_failed_gate(store: _Store):
    """AC3：`human="adjusted"` 但 `reason != "ok"` → select，不再回头裁。

    家长已经按自己的四角重裁过，机器当初那句"没找到纸边"就不再是结论了。
    把 `human == "adjusted"` 这半条判断删掉，这条立刻变红（AC7 变异 ①）。
    """
    scan_id = store.new_scan(reason="no_paper", human="adjusted")

    assert store.stage(scan_id) == scan_service.STAGE_SELECT


def test_human_adjusted_then_stored_questions_is_ready(store: _Store):
    """人工重裁过又收进去了 → ready（不是"人工调整过就永远停在 select"）。"""
    scan_id = store.new_scan(reason="no_paper", human="adjusted", questions=2)

    assert store.stage(scan_id) == scan_service.STAGE_READY


def test_human_skipped_is_not_adjusted(store: _Store):
    """`skipped`（家长说"就这样"）不等于 `adjusted`：机器没裁成仍然是 crop。

    两个值都是家长点出来的，但语义相反——这条钉住别把 `human is not None` 当成判据。
    """
    scan_id = store.new_scan(reason="no_paper", human="skipped")

    assert store.stage(scan_id) == scan_service.STAGE_CROP


# --------------------------------------------------- 两条收进去路都算"已收好"


def test_confirmed_from_the_detail_page_counts_as_stored(store: _Store):
    """详情页那个确认表单只回填 `scan.question_id`，也算一道收进去。

    不数它的话，从详情页确认过的卷子会一直显示"待选题"。
    """
    scan_id = store.new_scan(reason="ok", confirm=True)

    assert store.count(scan_id) == 1
    assert store.stage(scan_id) == scan_service.STAGE_READY


# ------------------------------------------------------------------ AC5 不存在


def test_a_missing_scan_has_no_stage(store: _Store):
    """AC5：卷子不存在 → None，不拿 "crop" 兜底（页面该给 404）。"""
    assert store.stage(999_999) is None


def test_another_learners_scan_has_no_stage(store: _Store):
    """AC5 的另一半：别的孩子的卷子同样当作不存在。"""
    scan_id = store.new_scan(reason="ok")
    other = store.new_learner()

    assert store.stage(scan_id, learner_id=other) is None
