"""同类题检索（0.2 卡8，B 线：按知识点标签捞同类）。

产品口径（docs/CONCEPTS-RESEARCH.md 第四节）：数学的「同类型题」先走标签检索 ——
给入库题打知识点标签，出「同类题」= 共享至少一个知识点 + 排除原题 + 按到期排序。
Anki 的 Filtered Deck 就是这个机制，最省事，也不需要第二套调度。

三条硬约束：

- **排除原题自身**：重练的价值是换一道题，不是重做一遍原题。
- **只认 active 知识点**：merged/ignored 的知识点已经不代表现在的分类，
  拿它当同类依据会把两批题混在一起；这类题在页面上走「去补标」。
- **只查库**：不 import 调度器、不调模型、不发网络请求。

排序（`find_similar_questions`）：共享知识点数降序 → 从未练过优先 →
`next_due_at` 升序（空值视为最晚）→ `question_id`（保证结果稳定）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.concept_service import attach_to_question, concepts_for_question, normalize_name
from cairn.entry_service import ValidationError
from cairn.models import (
    CONCEPT_STATUS_ACTIVE,
    REVIEW_STATUS_ARCHIVED,
    Concept,
    ErrorItem,
    Question,
    Review,
    question_concept,
)

DEFAULT_LIMIT = 5


@dataclass(frozen=True)
class SimilarQuestion:
    """One candidate for「再来一道同类的」.

    ``error_item_id`` is what the practice flow takes (reveal/grade 按错题 id 走)；
    ``content`` is the prompt the child sees; ``concept_names`` are all active
    tags on that question; ``shared_count`` is how many of them the source
    question also carries (that is the sort key).
    """

    question_id: int
    error_item_id: int
    content: str
    concept_names: tuple[str, ...]
    shared_count: int
    practice_count: int
    next_due_at: datetime | None

    @property
    def practiced(self) -> bool:
        return self.practice_count > 0

    @property
    def concept_label(self) -> str:
        return "、".join(self.concept_names)


@dataclass(frozen=True)
class ConceptOption:
    """One row of the 补标 dropdown (plain data — the session is gone by render)."""

    concept_id: int
    name: str


@dataclass(frozen=True)
class SimilarView:
    """Everything the similar page needs, computed here so the template only renders."""

    error_item_id: int
    question_id: int
    subject: str
    content: str
    concept_names: tuple[str, ...]
    untagged: bool
    items: tuple[SimilarQuestion, ...]
    concept_options: tuple[ConceptOption, ...]
    limit: int


def _load_source(
    session: Session, learner_id: int, error_item_id: int
) -> tuple[ErrorItem, Question]:
    """Resolve the错题 being practised; another learner's item is a validation error."""
    item = session.get(ErrorItem, error_item_id)
    if item is None:
        raise ValidationError(f"error_item does not exist: error_item_id={error_item_id}")
    if item.learner_id != learner_id:
        raise ValidationError(f"error_item {error_item_id} 不属于当前 learner")
    question = session.get(Question, item.question_id)
    if question is None:
        raise ValidationError(f"question does not exist: question_id={item.question_id}")
    if question.learner_id != learner_id:
        raise ValidationError(f"question {question.id} 不属于当前 learner")
    return item, question


def _active_concepts(session: Session, question_id: int) -> list[Concept]:
    return [
        concept
        for concept in concepts_for_question(session, question_id)
        if concept.status == CONCEPT_STATUS_ACTIVE
    ]


def _due_key(due: datetime | None) -> float:
    """Comparable due key: SQLite hands back naive UTC; empty due sorts last."""
    if due is None:
        return float("inf")
    return due.timestamp()


def _pick_item(rows: list[Any]) -> Any:
    """One practice entry per question: open first, then earliest due, then id."""
    open_rows = [row for row in rows if row.review_status != REVIEW_STATUS_ARCHIVED]
    return min(open_rows or rows, key=lambda row: (_due_key(row.next_due_at), row.id))


def _similar_for(
    session: Session,
    learner_id: int,
    source_question: Question,
    limit: int,
) -> list[SimilarQuestion]:
    if limit <= 0:
        return []
    source_ids = {concept.id for concept in _active_concepts(session, source_question.id)}
    if not source_ids:
        # 没标签就无从谈同类：页面走「去补标」，这里不猜、不按题型凑数。
        return []

    rows = session.execute(
        select(
            ErrorItem.id,
            ErrorItem.question_id,
            ErrorItem.review_status,
            ErrorItem.next_due_at,
        )
        .join(Question, Question.id == ErrorItem.question_id)
        .where(
            ErrorItem.learner_id == learner_id,
            Question.learner_id == learner_id,
            ErrorItem.question_id != source_question.id,
        )
    ).all()
    if not rows:
        return []

    items_by_question: dict[int, list] = {}
    for row in rows:
        items_by_question.setdefault(row.question_id, []).append(row)

    # 全部出库的题不再往回捞：孩子已经掌握，重练它只会挤掉真正要练的题。
    candidates = {
        question_id: question_rows
        for question_id, question_rows in items_by_question.items()
        if any(row.review_status != REVIEW_STATUS_ARCHIVED for row in question_rows)
    }
    if not candidates:
        return []

    links: dict[int, list[int]] = {}
    for question_id, concept_id in session.execute(
        select(question_concept.c.question_id, question_concept.c.concept_id).where(
            question_concept.c.question_id.in_(list(candidates))
        )
    ):
        links.setdefault(question_id, []).append(concept_id)

    shared = {
        question_id: len(source_ids.intersection(concept_ids))
        for question_id, concept_ids in links.items()
    }
    shared = {question_id: count for question_id, count in shared.items() if count > 0}
    if not shared:
        return []

    all_concept_ids = {cid for qid in shared for cid in links[qid]}
    names = {
        concept.id: concept.name
        for concept in session.scalars(
            select(Concept).where(
                Concept.id.in_(all_concept_ids),
                Concept.status == CONCEPT_STATUS_ACTIVE,
            )
        )
    }

    item_ids = [row.id for qid in shared for row in candidates[qid]]
    review_counts = dict(
        session.execute(
            select(Review.error_item_id, func.count())
            .where(Review.error_item_id.in_(item_ids))
            .group_by(Review.error_item_id)
        ).all()
    )

    picked: list[tuple[int, Any, int, int, tuple[str, ...]]] = []
    for question_id, shared_count in shared.items():
        item = _pick_item(candidates[question_id])
        practice_count = sum(review_counts.get(row.id, 0) for row in candidates[question_id])
        concept_names = tuple(
            names[cid] for cid in sorted(links[question_id]) if cid in names
        )
        picked.append((question_id, item, shared_count, practice_count, concept_names))

    picked.sort(
        key=lambda entry: (
            -entry[2],
            0 if entry[3] == 0 else 1,
            _due_key(entry[1].next_due_at),
            entry[0],
        )
    )
    picked = picked[:limit]

    questions = {
        question.id: question
        for question in session.scalars(
            select(Question).where(Question.id.in_([entry[0] for entry in picked]))
        )
    }
    return [
        SimilarQuestion(
            question_id=question_id,
            error_item_id=item.id,
            content=questions[question_id].content,
            concept_names=concept_names,
            shared_count=shared_count,
            practice_count=practice_count,
            next_due_at=item.next_due_at,
        )
        for question_id, item, shared_count, practice_count, concept_names in picked
    ]


def find_similar_questions(
    session: Session,
    learner_id: int,
    error_item_id: int,
    limit: int = DEFAULT_LIMIT,
) -> list[SimilarQuestion]:
    """Other questions of the same learner that share at least one active concept."""
    _, question = _load_source(session, learner_id, error_item_id)
    return _similar_for(session, learner_id, question, limit)


def build_similar_view(
    session: Session,
    learner_id: int,
    error_item_id: int,
    limit: int = DEFAULT_LIMIT,
) -> SimilarView:
    """One call for the page: source facts + untagged flag + candidates + 补标选项."""
    item, question = _load_source(session, learner_id, error_item_id)
    concepts = _active_concepts(session, question.id)
    untagged = not concepts
    options: tuple[ConceptOption, ...] = ()
    if untagged:
        options = tuple(
            ConceptOption(concept_id=concept.id, name=concept.name)
            for concept in session.scalars(
                select(Concept)
                .where(
                    Concept.subject == normalize_name(question.subject),
                    Concept.status == CONCEPT_STATUS_ACTIVE,
                )
                .order_by(Concept.name)
            )
        )
    return SimilarView(
        error_item_id=item.id,
        question_id=question.id,
        subject=question.subject,
        content=question.content,
        concept_names=tuple(concept.name for concept in concepts),
        untagged=untagged,
        items=tuple(_similar_for(session, learner_id, question, limit)),
        concept_options=options,
        limit=limit,
    )


def tag_source(
    session: Session,
    learner_id: int,
    error_item_id: int,
    concept_id: int | None = None,
    new_name: str | None = None,
) -> Concept:
    """补标入口：把已有知识点（*concept_id*）或新名字挂到这道题上。

    写库只经 ``concept_service.attach_to_question``，路由不直接碰关联表。
    """
    _, question = _load_source(session, learner_id, error_item_id)
    concept = attach_to_question(
        session, question, concept_id=concept_id, new_name=new_name
    )
    session.commit()
    return concept
