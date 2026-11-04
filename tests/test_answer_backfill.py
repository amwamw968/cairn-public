"""没有标准答案的题怎么走（2026-11-01 卡）：AC2–AC7。

扫描入库的题 `answer` 是 NULL。旧行为：判题层回「无法自动判定」→ confidence
压到 0.5 → 掉进 `/review-queue`；而那个队列只审「这条题录得对不对」，按钮只有
确认/驳回、不落评分，于是扫描题永远拿不到评分、永远不出库。

本文件锁死新路径：

- AC2：空答案 → `submit_and_judge` 回 `needs_manual=True`，**不写 review、
  不改 confidence、不进 pending 队列**（NULL / 空串 / 纯空白同一条路）。
- AC3：揭示页给「这道题还没有标准答案，你看到的是原题。请对照原图判断孩子
  做对没做对。」+ 两键（做对了 / 没做对）→ good / again，`last_reviewed_at`
  与 `next_due_at` 都有值（和自动判题一样排期）。
- AC4/AC5：详情页补答案（去首尾空格、可清空、超长拒收、别人的题 404）；
  填了答案后同一道题回到自动判题。
- AC6：以上每条一个用例。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn.graduation_service import pending_review_queue
from cairn.models import (
    REVIEW_STATUS_NEW,
    ErrorItem,
    Learner,
    Question,
    make_engine,
    make_session_factory,
)
from cairn.models.review import Review
from cairn.practice_service import submit_and_judge
from cairn.web import ANSWER_MAX_CHARS, create_app

SCANNED_CONTENT = "求下面图形的面积。"
MANUAL_HINT = "这道题还没有标准答案，你看到的是原题。请对照原图判断孩子做对没做对。"

# 页面上的评分按钮：文案 → value。测试按「家长看到的按钮」提交，而不是硬编码
# good/again：模板里把两键写反时，这里必须红（AC7 变异自证）。
_RATING_BUTTON = re.compile(
    r'<button[^>]*name="rating"[^>]*value="([^"]+)"[^>]*>([^<]+)</button>'
)


def _rating_buttons(page: str) -> dict[str, str]:
    return {label.strip(): value for value, label in _RATING_BUTTON.findall(page)}


# --- 页面库（answer 为空 = 扫描题的样子） -----------------------------------


@pytest.fixture()
def web(tmp_path):
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as client:
        yield client, db


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
    db: Path,
    *,
    answer: str | None = None,
    content: str = SCANNED_CONTENT,
    learner_id: int | None = None,
) -> tuple[int, int]:
    """建一道题 + 一条 new 错题（扫描题的样子：没有标准答案、confidence=1.0）。"""
    with _session(db) as session:
        if learner_id is None:
            learner_id = session.scalars(
                select(Learner).order_by(Learner.id.asc()).limit(1)
            ).first().id
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


def _other_learner_id(db: Path) -> int:
    with _session(db) as session:
        other = Learner(name="另一个孩子", grade_level="primary-3")
        session.add(other)
        session.commit()
        return other.id


def _item_row(db: Path, item_id: int) -> dict:
    with _session(db) as session:
        item = session.get(ErrorItem, item_id)
        return {
            "confidence": item.confidence,
            "wrong_count": item.wrong_count,
            "review_status": item.review_status,
            "last_reviewed_at": item.last_reviewed_at,
            "next_due_at": item.next_due_at,
        }


def _reviews(db: Path, item_id: int) -> list[Review]:
    with _session(db) as session:
        return list(
            session.scalars(select(Review).where(Review.error_item_id == item_id))
        )


def _answer_of(db: Path, question_id: int) -> str | None:
    with _session(db) as session:
        return session.get(Question, question_id).answer


# --- AC2：空答案不进复核队列 ------------------------------------------------


@pytest.mark.parametrize("answer", [None, "", "   ", "\n\t "], ids=["null", "empty", "spaces", "whitespace"])
def test_blank_answer_returns_manual_and_writes_nothing(session, learner, answer):
    """AC2：空答案 → needs_manual，且一行簿记都不动（不 commit、不进队列）。"""
    question = Question(
        learner_id=learner.id,
        subject="数学",
        grade_level="primary-3",
        question_type="计算",
        content=SCANNED_CONTENT,
        answer=answer,
    )
    session.add(question)
    session.flush()
    item = ErrorItem(
        learner_id=learner.id,
        question_id=question.id,
        review_status=REVIEW_STATUS_NEW,
        confidence=1.0,
    )
    session.add(item)
    session.commit()

    revealed, grade = submit_and_judge(session, learner.id, item.id, "24 平方厘米")

    assert grade.needs_manual is True          # 明确的「交人工」标志
    assert grade.needs_review is False         # 不是「低置信度复核」
    assert not (revealed.answer or "").strip()  # 揭示页拿到的是空答案本身

    session.refresh(item)
    assert item.confidence == 1.0              # 不再被压到 0.5
    assert item.review_status == REVIEW_STATUS_NEW
    assert item.last_reviewed_at is None
    assert item.next_due_at is None
    assert item.wrong_count == 1               # 没被当成错题判 again
    assert list(session.scalars(select(Review))) == []  # 没写 review
    assert item.id not in {i.id for i in pending_review_queue(session, learner.id)}


def test_filled_answer_keeps_auto_judging(session, learner, question):
    """对照组：有答案时旧行为不变（对→good，写 review 并排期）。"""
    item = ErrorItem(learner_id=learner.id, question_id=question.id, confidence=1.0)
    session.add(item)
    session.commit()

    _, grade = submit_and_judge(session, learner.id, item.id, "27")

    assert not getattr(grade, "needs_manual", False)
    assert grade.correct and not grade.needs_review
    reviews = list(session.scalars(select(Review)))
    assert len(reviews) == 1 and reviews[0].rating == "good"
    session.refresh(item)
    assert item.last_reviewed_at is not None and item.next_due_at is not None


# --- AC3：揭示页两键落 good / again ----------------------------------------


def test_reveal_page_without_answer_offers_two_keys(web):
    """AC3：无答案揭示页给指定文案 + 两键，且不出现「自动判定」。"""
    client, db = web
    item_id, _ = _add_question(db)

    page = client.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "24"}
    )

    assert page.status_code == 200
    assert MANUAL_HINT in page.text
    buttons = _rating_buttons(page.text)
    assert buttons.get("做对了") == "good"
    assert buttons.get("没做对") == "again"
    assert "自动判定" not in page.text       # 没判，就不装作判过
    assert _reviews(db, item_id) == []        # 揭示本身不落评分


def test_two_keys_land_rating_and_schedule(web):
    """AC3/AC6：点「做对了」→ good、点「没做对」→ again，都写 review 并排期。"""
    client, db = web
    good_item, _ = _add_question(db, content="求下面图形的面积。")
    again_item, _ = _add_question(db, content="求下面图形的周长。")

    def _press(item_id: int, label: str):
        page = client.post(
            "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "24"}
        )
        rating = _rating_buttons(page.text)[label]
        return client.post(
            "/practice/grade", data={"item_id": str(item_id), "rating": rating}
        )

    page = _press(good_item, "做对了")
    assert page.status_code == 200 and "判分成功" in page.text
    page = _press(again_item, "没做对")
    assert page.status_code == 200 and "判分成功" in page.text

    good_reviews = _reviews(db, good_item)
    assert [r.rating for r in good_reviews] == ["good"]
    good_row = _item_row(db, good_item)
    assert good_row["last_reviewed_at"] is not None
    assert good_row["next_due_at"] is not None
    assert good_row["wrong_count"] == 1            # 做对了不涨错
    assert good_row["review_status"] == "reviewing"

    again_reviews = _reviews(db, again_item)
    assert [r.rating for r in again_reviews] == ["again"]
    again_row = _item_row(db, again_item)
    assert again_row["last_reviewed_at"] is not None
    assert again_row["next_due_at"] is not None
    assert again_row["wrong_count"] == 2           # 没做对记一次错


# --- AC4/AC5：详情页补答案 → 同一题自动判 -----------------------------------


def test_detail_page_backfills_answer_and_switches_to_auto_grading(web):
    """AC4/AC5：详情页保存答案（去首尾空格），随后同一题走自动判题。"""
    client, db = web
    item_id, question_id = _add_question(db, content="12 + 15 = ?")

    detail = client.get(f"/questions/{question_id}")
    assert detail.status_code == 200
    # P4-a：这一页只剩一条保存路径——编辑表单；独立的「保存答案」表单已撤掉。
    # G2-6 起多一个「删掉」确认表单（破坏性操作，不是保存路径），两个目标逐字锁。
    actions = sorted(re.findall(r'<form[^>]*action="([^"]+)"', detail.text))
    assert actions == sorted(
        [f"/questions/{question_id}/edit", f"/questions/{question_id}/delete"]
    ), actions
    assert f'action="/questions/{question_id}/edit"' in detail.text
    assert f'action="/questions/{question_id}/answer"' not in detail.text
    assert 'name="answer"' in detail.text
    assert "这道题还没有标准答案" in detail.text  # 无答案时给家长一句人话

    saved = client.post(f"/questions/{question_id}/answer", data={"answer": "  27  "})
    assert saved.status_code == 200
    assert "答案已保存" in saved.text
    assert _answer_of(db, question_id) == "27"   # 首尾空格去掉

    revealed = client.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "27"}
    )
    assert revealed.status_code == 200
    # G2-5 文案：「自动判定：正确」→「机器判的 / 对」（判定逻辑不变，换了人话）
    assert "机器判的" in revealed.text
    assert re.search(r"机器判的</span>\s*<span>对", revealed.text)
    assert MANUAL_HINT not in revealed.text
    assert "做对了" not in revealed.text
    assert [r.rating for r in _reviews(db, item_id)] == ["good"]


def test_detail_page_clearing_answer_goes_back_to_manual(web):
    """AC6：清空答案 → 置 NULL，同一题回到人工两键，不自动判 again。"""
    client, db = web
    item_id, question_id = _add_question(db, answer="27", content="12 + 15 = ?")

    cleared = client.post(f"/questions/{question_id}/answer", data={"answer": "   "})
    assert cleared.status_code == 200
    assert _answer_of(db, question_id) is None

    revealed = client.post(
        "/practice/reveal", data={"item_id": str(item_id), "learner_answer": "28"}
    )
    assert revealed.status_code == 200
    assert MANUAL_HINT in revealed.text
    assert _reviews(db, item_id) == []            # 空答案不许自动判 again
    assert _item_row(db, item_id)["wrong_count"] == 1


# --- AC6：越界与超长 --------------------------------------------------------


def test_answer_for_another_learners_question_is_404(web):
    """别人的题当不存在：404，且那道题的答案一个字都没变。"""
    client, db = web
    other_id = _other_learner_id(db)
    _, other_question = _add_question(
        db, answer="42", content="别人的题", learner_id=other_id
    )

    resp = client.post(f"/questions/{other_question}/answer", data={"answer": "偷看"})

    assert resp.status_code == 404
    assert _answer_of(db, other_question) == "42"


def test_answer_longer_than_limit_is_rejected(web):
    """AC4：>2000 字拒收（400），库里原有答案不动；正好 2000 字收下。"""
    client, db = web
    _, question_id = _add_question(db, answer="27", content="12 + 15 = ?")

    too_long = "答" * (ANSWER_MAX_CHARS + 1)
    resp = client.post(f"/questions/{question_id}/answer", data={"answer": too_long})

    assert resp.status_code == 400
    assert f"答案最多 {ANSWER_MAX_CHARS} 字" in resp.text
    assert _answer_of(db, question_id) == "27"

    exact = "答" * ANSWER_MAX_CHARS
    ok = client.post(f"/questions/{question_id}/answer", data={"answer": exact})
    assert ok.status_code == 200
    assert _answer_of(db, question_id) == exact
