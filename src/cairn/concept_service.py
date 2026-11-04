"""Concept normalisation and tagging — the only place that writes the concept tables.

Three jobs, all of them small enough to keep here:

1. **归一**：``normalize_name`` 把全角/首尾空白/内部多空白/大小写抹平，
   于是「　解 方程　」「解方程」「解方程」是同一个键。
2. **取或建**：``ensure_concept`` 是幂等的（同 subject + 同规范名只会有一行），
   迁移回填和将来的录入打标都走它，不写第二套逻辑。
3. **别名与挂题**：``add_alias`` 登记语义同义叫法（「一元一次方程解法」→「解方程」），
   ``tag_question`` 把一题挂到一个或多个知识点上，重复挂不会产生第二行。

Why aliases are a separate, explicit act: no string heuristic can know that
「解方程」and「一元一次方程解法」are the same thing. Surface forms are folded
automatically; semantics is a human decision recorded once in the alias table.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.models.concept import (
    CONCEPT_STATUS_ACTIVE,
    Concept,
    ConceptAlias,
    question_concept,
)
from cairn.models.question import Question

# 旧数据里的人写分隔符：顿号、半角/全角逗号、分号、竖线、斜杠、换行
_NAME_SEPARATORS = re.compile(r"[、,，;；|/\n\r]+")
_WHITESPACE = re.compile(r"\s+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def normalize_name(raw: str) -> str:
    """Fold a spoken/written name into its lookup key.

    NFKC turns full-width forms into their ASCII equivalents; casefold makes
    English names case-insensitive; whitespace is either removed (Chinese names
    never contain a meaningful space, and parents do type「解 方程」) or
    collapsed to one (English phrases such as ``keep cool`` need it).
    """
    text = unicodedata.normalize("NFKC", raw or "")
    if _CJK.search(text):
        return _WHITESPACE.sub("", text).casefold()
    return _WHITESPACE.sub(" ", text).strip().casefold()


def split_names(raw: str) -> list[str]:
    """Split a legacy multi-value field (``解方程、一元一次方程解法``) into keys.

    Blank fragments are dropped, duplicates collapse, order is preserved so a
    migration report reads in the same order as the source string.
    """
    names: list[str] = []
    for fragment in _NAME_SEPARATORS.split(unicodedata.normalize("NFKC", raw or "")):
        name = normalize_name(fragment)
        if name and name not in names:
            names.append(name)
    return names


def get_concept(session: Session, subject: str, name: str) -> Concept | None:
    """Resolve *name* to a concept.

    Aliases are matched first and are global (one spoken form → one concept,
    regardless of subject), which is the invariant that makes aggregation
    correct. A name that is not an alias is looked up as a canonical name
    inside *subject*.
    """
    key = normalize_name(name)
    if not key:
        return None
    alias = session.scalars(
        select(ConceptAlias).where(ConceptAlias.alias == key)
    ).first()
    if alias is not None:
        return alias.concept
    return session.scalars(
        select(Concept).where(
            Concept.subject == normalize_name(subject), Concept.name == key
        )
    ).first()


def ensure_concept(session: Session, subject: str, name: str) -> Concept:
    """Get the concept for *name*, creating it if this subject has none yet.

    Idempotent by design: the migration backfill and any future tagging path
    both call this, so a repeated run cannot fork one knowledge point into two.
    """
    existing = get_concept(session, subject, name)
    if existing is not None:
        return existing
    concept = Concept(subject=normalize_name(subject), name=normalize_name(name))
    session.add(concept)
    session.flush()
    return concept


def add_alias(session: Session, concept: Concept, alias: str) -> ConceptAlias | None:
    """Register *alias* as another name for *concept*.

    Returns the existing row when the alias already points here (idempotent),
    ``None`` when the alias is just a surface variant of the canonical name
    (nothing to store). Raises ``ValueError`` when the alias already belongs to
    a different concept — silently retargeting would corrupt future statistics.
    """
    key = normalize_name(alias)
    if not key or key == concept.name:
        return None
    existing = session.scalars(
        select(ConceptAlias).where(ConceptAlias.alias == key)
    ).first()
    if existing is not None:
        if existing.concept_id != concept.id:
            raise ValueError(
                f"alias {key!r} already belongs to concept {existing.concept_id}"
            )
        return existing
    row = ConceptAlias(concept_id=concept.id, alias=key)
    session.add(row)
    session.flush()
    return row


def _link(session: Session, question: Question, concept: Concept) -> None:
    """Idempotently add one question↔concept row, without resolving by name.

    Split out of ``tag_question`` so a caller that already holds the exact
    ``Concept`` can link it directly: re-resolving the name would route it back
    through the global alias table, which can point at another subject.
    """
    linked = session.execute(
        select(question_concept.c.concept_id).where(
            question_concept.c.question_id == question.id,
            question_concept.c.concept_id == concept.id,
        )
    ).first()
    if linked is None:
        session.execute(
            question_concept.insert().values(
                question_id=question.id, concept_id=concept.id
            )
        )


def _canonical_in_subject(session: Session, subject: str, name: str) -> Concept:
    """Get-or-create the canonical concept named *name* under *subject*.

    Deliberately bypasses the global alias table: when an alias would resolve
    *name* to a concept in another subject, filing under that one would let
    similar-question retrieval cross subjects. ``(subject, name)`` is the DB
    uniqueness key (``uq_concept_subject_name``), so this cannot fork a
    duplicate inside one subject.
    """
    norm_subject = normalize_name(subject)
    norm_name = normalize_name(name)
    existing = session.scalars(
        select(Concept).where(
            Concept.subject == norm_subject, Concept.name == norm_name
        )
    ).first()
    if existing is not None:
        return existing
    concept = Concept(subject=norm_subject, name=norm_name)
    session.add(concept)
    session.flush()
    return concept


def tag_question(
    session: Session, question: Question, names: Iterable[str]
) -> list[Concept]:
    """Attach *question* to every concept named in *names*.

    Each name is resolved through the alias table first, so two callers using
    different spoken forms still land on one concept. Concepts that are no
    longer active (merged/ignored) are skipped — their rows stay for history,
    but new questions must not be filed under them.
    """
    if question.id is None:
        raise ValueError("question must be flushed (needs an id) before tagging")
    attached: list[Concept] = []
    for name in names:
        concept = ensure_concept(session, question.subject, name)
        if concept.status != CONCEPT_STATUS_ACTIVE:
            continue
        _link(session, question, concept)
        if concept not in attached:
            attached.append(concept)
    session.flush()
    return attached


def attach_to_question(
    session: Session,
    question: Question,
    *,
    concept_id: int | None = None,
    new_name: str | None = None,
) -> Concept:
    """补标入口（0.2 卡8）：把**已有**知识点或**新建**名字挂到一道题上。

    ``concept_id`` 优先；没有 id 时用 ``new_name`` 走 ``ensure_concept``，同规范名
    不会建出第二个知识点。只接受 active 且同学科的知识点：跨学科挂载会让统计和
    同类检索串科，而 merged/ignored 的知识点已经不代表现在的分类。
    ``new_name`` 若被全局别名解析到别的学科，改在本学科取/建同名概念再挂，绝不跨科；
    挂载走 ``_link`` 直接挂解析出的 concept，避免 ``tag_question`` 再按名字解析而
    被别名带回别科。重复挂载幂等（``_link`` 不写第二行）。
    """
    if question.id is None:
        raise ValueError("question must be flushed (needs an id) before tagging")
    if concept_id is not None:
        concept = session.get(Concept, concept_id)
        if concept is None:
            raise ValueError(f"concept does not exist: concept_id={concept_id}")
        if concept.status != CONCEPT_STATUS_ACTIVE:
            raise ValueError(f"知识点已停用（{concept.status}）：{concept.name}")
        if concept.subject != normalize_name(question.subject):
            raise ValueError(
                f"知识点属于「{concept.subject}」，这道题是「{question.subject}」"
            )
    else:
        name = (new_name or "").strip()
        if not name:
            raise ValueError("要么选一个已有知识点，要么新建一个名字")
        concept = ensure_concept(session, question.subject, name)
        if concept.subject != normalize_name(question.subject):
            # 别名表全局唯一，可能把同名指到别的学科：回到本学科取/建同名概念。
            concept = _canonical_in_subject(session, question.subject, name)
        if concept.status != CONCEPT_STATUS_ACTIVE:
            raise ValueError(f"知识点已停用（{concept.status}）：{concept.name}")

    _link(session, question, concept)
    session.flush()
    return concept


def concepts_for_question(session: Session, question_id: int) -> list[Concept]:
    """Every concept the question is filed under, id ascending."""
    return list(
        session.scalars(
            select(Concept)
            .join(question_concept, question_concept.c.concept_id == Concept.id)
            .where(question_concept.c.question_id == question_id)
            .order_by(Concept.id)
        )
    )
