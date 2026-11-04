"""Manual entry service: parent enters one wrong question end-to-end.

Single entry (create_error_item) persists QUESTION + ERROR_ITEM in a single
transaction:

    required-field validation -> verify_entry switch -> persist

Batch entry (create_error_items) is the "paste a whole day's words" path: the
caller hands over already-parsed payloads (see cairn.parse), every row lands in
ONE transaction, and an answer that this learner already has is skipped rather
than re-inserted:

    learner check -> for each payload: field check -> dedupe -> persist
                  -> one commit; any failure rolls the whole batch back

verify_entry switch (default off, off == status quo):
- off : confidence=1.0, entry_verifier is never called
- on  : entry_verifier.verify(payload) -> float in [0, 1] is stored as
        ERROR_ITEM.confidence (1.0 fully trusted, <1.0 doubtful; P1 gating
        reads this same field). A doubtful verdict still persists — gating
        is a P1 concern, not this service's.

Duplicate detection (0.2 卡6) uses ``cairn.duplicate`` (pure rules, no DB).
Single entry: an *exact* repeat (same question after normalization) raises
``DuplicateEntryError`` — nothing is written. A merely *similar* one is not
this service's call: it persists, and the caller (the web layer) decides
whether to warn first. Batch rows still skip on the existing answer key, as
before.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn import dict as dict_service
from cairn import duplicate
from cairn.duplicate import Candidate, DuplicateMatch
from cairn.models import ErrorItem, Learner, Question, REVIEW_STATUS_NEW
from cairn.models.learner import utcnow
from cairn.parse import audio_cue, detect_lang

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Raised when entry input or configuration is invalid."""


class DuplicateEntryError(ValidationError):
    """The same question is already in this learner's library — nothing written."""

    def __init__(self, match: DuplicateMatch):
        self.match = match
        super().__init__(duplicate.describe_exact(match))


@dataclass(frozen=True)
class EntryPayload:
    """The question face as entered by the parent."""

    subject: str
    question_type: str
    content: str
    answer: str
    analysis: str | None = None
    lang: str | None = None  # "en"/"zh"; None → auto-detect on persist (question.lang)
    # dictation cue read aloud before answering (never the answer); None → derived
    audio_text: str | None = None


@dataclass(frozen=True)
class BatchEntryResult:
    """Outcome of one batch: what landed, what was skipped as a duplicate."""

    created: tuple[ErrorItem, ...]
    skipped: tuple[str, ...]

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


class EntryVerifier(Protocol):
    """Pluggable entry verification (OCR/LLM later; test stubs in this batch)."""

    def verify(self, payload: EntryPayload) -> float: ...


_REQUIRED_FIELDS = ("subject", "question_type", "content", "answer")
_WHITESPACE = re.compile(r"\s+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _autofill_from_dict(payload: EntryPayload, lang: str) -> EntryPayload:
    """词典卡（ECDICT/CC-CEDICT）自动补，只在缺的时候补，用户手填的永远优先。

    按科目限定——词典是语言数据，跟数学无关：

    - ``语文``：听写朗读 cue 是拼音。用户给了 audio_text、或 content/answer 本身
      能推出拼音（``audio_cue``）时都不动；都没有才用 CC-CEDICT 补 audio_text。
    - ``英语``：content 应是中文释义。用户已写了汉字（content 含 CJK）就不动；
      否则用 ECDICT 的翻译补 content。
    - ``数学``与其他科目一律不补。
    - 词典文件缺失时查不到（None/[]），原样返回，不影响任何主流程。
    """
    if payload.subject == "英语":
        if _CJK.search(payload.content or ""):
            return payload  # 用户手填优先：已有中文释义，绝不覆盖
        hit = dict_service.lookup_en(payload.answer)
        if hit and (hit.get("translation") or "").strip():
            return replace(payload, content=hit["translation"].strip())
        return payload
    if payload.subject == "语文" and not (payload.audio_text or "").strip():
        cue = audio_cue(payload.content, payload.answer, lang)
        if cue is None:
            pinyin = dict_service.lookup_zh_pinyin(payload.answer)
            if pinyin:
                return replace(payload, audio_text=pinyin[0])
    return payload


def create_error_item(
    session: Session,
    learner_id: int,
    payload: EntryPayload,
    *,
    reject_duplicates: bool = False,
    verify_entry: bool = False,
    entry_verifier: EntryVerifier | None = None,
    commit: bool = True,
) -> ErrorItem:
    """Validate and persist one manually entered wrong question.

    *reject_duplicates* turns on the 0.2 卡6 rule: an exact repeat of a question
    this learner already has raises ``DuplicateEntryError`` before anything is
    written (a merely similar one still lands — the caller warns first). The
    entry screen passes it; the flag stays off by default because this is also
    the plain persistence primitive used by imports and fixtures.

    With ``commit=False`` the caller owns the transaction, allowing related
    records such as a scan-question link to be committed with the new item.
    """
    if learner_id is None:
        raise ValidationError("missing required field: learner_id")
    _require_fields(payload)
    if session.get(Learner, learner_id) is None:
        raise ValidationError(f"learner does not exist: learner_id={learner_id}")

    # 查重前先补全：en 条目落库时 content 会被词典释义重写（家长敲单词、系统
    # 补释义），重录用原始 payload 就对不上已存的那条（"future" vs 释义）。
    # 词典查询是确定性的，两边都用补全后的形态比较才稳；补全是幂等的，
    # _persist_one 里再跑一次是空操作。
    payload = _autofill_from_dict(
        payload,
        payload.lang if payload.lang in ("en", "zh") else detect_lang(payload.answer),
    )
    if reject_duplicates:
        match = find_entry_duplicate(session, learner_id, payload)
        if match is not None and match.is_exact:
            raise DuplicateEntryError(match)

    confidence = _confidence(payload, verify_entry, entry_verifier)
    item = _persist_one(session, learner_id, payload, confidence)
    if commit:
        session.commit()
    return item


def create_error_items(
    session: Session,
    learner_id: int,
    payloads: Sequence[EntryPayload],
    *,
    verify_entry: bool = False,
    entry_verifier: EntryVerifier | None = None,
) -> BatchEntryResult:
    """Persist a parsed paste in one transaction; duplicates are skipped.

    An answer (the word to write) this learner already has is skipped (counted in
    *skipped*), and so is one repeated inside the same batch. Dedupe keys on the
    answer, not the hint — after content/answer were aligned, content is the hint
    (pinyin / meaning) and several different words can share one hint. Any
    validation error, verifier error or database error rolls the whole batch
    back — no half-imported day.
    """
    if learner_id is None:
        raise ValidationError("missing required field: learner_id")
    if session.get(Learner, learner_id) is None:
        raise ValidationError(f"learner does not exist: learner_id={learner_id}")

    seen = _existing_answers(session, learner_id)
    created: list[ErrorItem] = []
    skipped: list[str] = []
    try:
        for payload in payloads:
            _require_fields(payload)
            key = answer_key(payload.answer)
            if key in seen:
                skipped.append(payload.content.strip())
                continue
            seen.add(key)
            confidence = _confidence(payload, verify_entry, entry_verifier)
            created.append(_persist_one(session, learner_id, payload, confidence))
        session.commit()
    except Exception:
        session.rollback()
        raise
    return BatchEntryResult(created=tuple(created), skipped=tuple(skipped))


def _require_fields(payload: EntryPayload) -> None:
    for field in _REQUIRED_FIELDS:
        value = getattr(payload, field)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"missing required field: {field}")


def existing_candidates(session: Session, learner_id: int) -> list[Candidate]:
    """Every question this learner already has, oldest first.

    Scoped by ``Question.learner_id``: 一个家里两个孩子各有一套题库，老大的题不能
    成为老二录同一个词的「已录过」。Mutable on purpose: the batch preview appends
    the rows it has just checked, so later rows are compared against earlier ones
    of the same paste.
    """
    rows = session.execute(
        select(Question.id, Question.content, Question.answer, Question.created_at)
        .where(Question.learner_id == learner_id)
        .order_by(Question.id.asc())
    )
    return [
        Candidate(
            content=content or "",
            answer=answer or "",
            question_id=question_id,
            created_at=created_at,
        )
        for question_id, content, answer, created_at in rows
    ]


def find_entry_duplicate(
    session: Session, learner_id: int, payload: EntryPayload
) -> DuplicateMatch | None:
    """Exact or similar collision for *payload* within this learner's library."""
    return duplicate.find_duplicate(
        payload.content, payload.answer, existing_candidates(session, learner_id)
    )


def _confidence(
    payload: EntryPayload, verify_entry: bool, entry_verifier: EntryVerifier | None
) -> float:
    if not verify_entry:
        return 1.0
    if entry_verifier is None:
        raise ValidationError("verify_entry=True requires an entry_verifier")
    confidence = entry_verifier.verify(payload)
    if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise ValidationError(f"verifier confidence out of [0,1]: {confidence!r}")
    return confidence


def _persist_one(
    session: Session, learner_id: int, payload: EntryPayload, confidence: float
) -> ErrorItem:
    """Add QUESTION + ERROR_ITEM and flush; the caller owns the commit."""
    # lang is resolved here for both paths: batch already supplies en/zh,
    # single entry leaves it None and we auto-detect from the answer (the word
    # the learner writes), not from content (now the hint, e.g. pinyin).
    lang = payload.lang if payload.lang in ("en", "zh") else detect_lang(payload.answer)
    payload = _autofill_from_dict(payload, lang)
    # cue resolution shares parse.audio_cue so manual entry and paste agree;
    # a blank field falls back to the same rule (and stays None for maths).
    audio_text = (payload.audio_text or "").strip() or audio_cue(
        payload.content, payload.answer, lang
    )
    question = Question(
        learner_id=learner_id,
        subject=payload.subject,
        grade_level="primary",
        question_type=payload.question_type,
        content=payload.content,
        answer=payload.answer,
        analysis=payload.analysis,
        lang=lang,
        audio_text=audio_text,
        source="manual",
    )
    session.add(question)
    session.flush()  # assign question.id

    item = ErrorItem(
        learner_id=learner_id,
        question_id=question.id,
        wrong_count=1,
        review_status=REVIEW_STATUS_NEW,
        confidence=confidence,
        last_wrong_at=utcnow(),
    )
    session.add(item)
    session.flush()
    return item


def _existing_answers(session: Session, learner_id: int) -> set[str]:
    """Answers this learner already has (scoped by question ownership)."""
    rows = session.scalars(
        select(Question.answer).where(Question.learner_id == learner_id)
    )
    return {answer_key(answer) for answer in rows}


def answer_key(answer: str) -> str:
    """Dedupe key: whitespace-insensitive, case-insensitive.

    Public because other entry paths (教材库「已加入」标记) must use the very same
    key as the batch import — otherwise a row could look addable on screen and be
    skipped on submit.
    """
    return _WHITESPACE.sub("", answer).casefold()
