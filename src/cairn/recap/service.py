"""Transactional text recall with one follow-up and idempotent submission."""

from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from cairn import review_service
from cairn.models.error_item import ErrorItem, REVIEW_STATUS_ARCHIVED
from cairn.models.learner import utcnow
from cairn.models.question import Question
from cairn.models.recap import RecapAttempt
from cairn.recap import assessment
from cairn.workflow.review import ready_for_practice

_KEY = re.compile(r"^[0-9a-f]{32}$")
MAX_TEXT = 2000


class RecapError(ValueError):
    """Invalid, stale, or inaccessible recall action."""


def _item(session: Session, learner_id: int, item_id: int) -> tuple[ErrorItem, Question]:
    item = session.get(ErrorItem, item_id)
    if item is None or item.learner_id != learner_id:
        raise RecapError("题目不存在")
    question = session.get(Question, item.question_id)
    if (question is None or question.learner_id != learner_id or not item.workflow_recap_enabled
            or item.review_status == REVIEW_STATUS_ARCHIVED
            or not session.scalar(select(ready_for_practice(item.question_id)))):
        raise RecapError("这道题暂不能进行复述")
    return item, question


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise RecapError("请填写不超过 2000 字的想法")
    return value.strip()


def _reference(question: Question) -> str:
    raw = json.dumps([question.content, question.answer, question.analysis], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_attempt(session: Session, learner_id: int, attempt_id: int) -> RecapAttempt:
    attempt = session.get(RecapAttempt, attempt_id)
    if attempt is None or attempt.learner_id != learner_id:
        raise RecapError("复述记录不存在")
    _item(session, learner_id, attempt.error_item_id)
    return attempt


def start(session: Session, learner_id: int, item_id: int, request_key: str,
          text: object, *, unsure: bool = False) -> RecapAttempt:
    """Persist one attempt; a repeated key cannot create another review."""
    if not isinstance(request_key, str) or not _KEY.fullmatch(request_key):
        raise RecapError("提交标识无效，请刷新重试")
    initial = "" if unsure else _text(text)
    item, question = _item(session, learner_id, item_id)
    previous = session.scalar(select(RecapAttempt).where(
        RecapAttempt.learner_id == learner_id, RecapAttempt.request_key == request_key))
    if previous is not None:
        if (previous.error_item_id != item_id or previous.initial_text != initial
                or (previous.status == "unsure") != unsure):
            raise RecapError("这次提交的内容与之前不同")
        return previous
    reference_hash = _reference(question)
    suggestion, focus = ("unavailable", None) if unsure else assessment.assess(question, initial)
    status = "unsure" if unsure else ("needs_followup" if suggestion == "needs_more" else
                                      "completed" if suggestion == "adequate" else "unavailable")
    result = session.execute(sqlite_insert(RecapAttempt).values(
        learner_id=learner_id, error_item_id=item_id, request_key=request_key,
        initial_text=initial, status=status, assessment=suggestion, focus=focus,
        reference_hash=reference_hash, revision=0, created_at=utcnow(), updated_at=utcnow(),
    ).on_conflict_do_nothing(index_elements=[RecapAttempt.learner_id, RecapAttempt.request_key]))
    attempt = session.scalar(select(RecapAttempt).where(
        RecapAttempt.learner_id == learner_id, RecapAttempt.request_key == request_key))
    if attempt is None:
        raise RecapError("无法保存复述，请重试")
    if result.rowcount != 1:
        if (attempt.error_item_id != item_id or attempt.initial_text != initial
                or (attempt.status == "unsure") != unsure):
            raise RecapError("这次提交的内容与之前不同")
        return attempt
    session.refresh(question)
    if _reference(question) != reference_hash:
        raise RecapError("题目刚刚被修改，请刷新后重试")
    if unsure:
        review = review_service.apply_review(session, item, "again", commit=False)
        attempt.review_id = review.id
    session.flush()
    return attempt


def follow_up(session: Session, learner_id: int, attempt_id: int, text: object) -> RecapAttempt:
    """Accept at most one follow-up; identical retries return the same result."""
    response = _text(text)
    attempt = get_attempt(session, learner_id, attempt_id)
    if attempt.followup_text is not None:
        if attempt.followup_text == response:
            return attempt
        raise RecapError("这次追问已经回答过")
    if attempt.status != "needs_followup":
        raise RecapError("当前不需要追问")
    _, question = _item(session, learner_id, attempt.error_item_id)
    if _reference(question) != attempt.reference_hash:
        raise RecapError("题目已被修改，请重新开始复述")
    suggestion, focus = assessment.assess(question, attempt.initial_text, response)
    changed = session.execute(update(RecapAttempt).where(
        RecapAttempt.id == attempt.id, RecapAttempt.revision == attempt.revision,
        RecapAttempt.status == "needs_followup",
    ).values(followup_text=response, assessment=suggestion, focus=focus,
             status="unavailable" if suggestion == "unavailable" else "completed",
             revision=attempt.revision + 1, updated_at=utcnow()).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        session.expire_all()
        current = get_attempt(session, learner_id, attempt_id)
        if current.followup_text == response:
            return current
        raise RecapError("复述已在其他设备更新，请刷新")
    session.refresh(question)
    if _reference(question) != attempt.reference_hash:
        raise RecapError("题目已被修改，请重新开始复述")
    session.refresh(attempt)
    return attempt
