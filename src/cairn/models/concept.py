"""CONCEPT + CONCEPT_ALIAS + QUESTION_CONCEPT — 知识点（规范名 + 别名）。

Why a second knowledge-point shape instead of extending ``knowledge_point``:
调研结论（docs/CONCEPTS-RESEARCH.md 第二节）要求「规范名 + 别名」两层，否则
孩子说「解方程」、家长写「一元一次方程解法」，同一件事算成两个知识点，统计永远
不准。旧表 ``knowledge_point`` 只存一个不重复的名字，没有别名也没有状态，
所以新增三张表；旧表与 ``question_knowledge_link`` 一律保留（软迁移），
历史行由 v5 迁移搬进这里，搬不动的留在原处。

Design notes:

- ``concepts.name`` 存**归一后的**名字（NFKC + 去首尾空白 + 压缩内部空白 + casefold），
  唯一约束 ``(subject, name)`` 才有意义；展示用的也是这个名字（中文不受影响，
  英文统一小写）。
- ``concept_aliases.alias`` 全局唯一：一种叫法只能指向一个知识点，这是「口语叫法
  归一」的硬约束。别名同样存归一后的形态。
- ``status`` 只有 active / merged / ignored 三态。merged/ignored 不删行、不删别名
  （历史统计要能回溯），只是不再接受新题挂载。
- ``question_concept`` 是纯关联表（复合主键），同一题可挂多个知识点——一题同时考
  「进位」和「两位数乘一位数」是常态，统计时才按题去重。
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cairn.models.base import Base

CONCEPT_STATUS_ACTIVE = "active"
CONCEPT_STATUS_MERGED = "merged"
CONCEPT_STATUS_IGNORED = "ignored"

CONCEPT_STATUSES = frozenset(
    {CONCEPT_STATUS_ACTIVE, CONCEPT_STATUS_MERGED, CONCEPT_STATUS_IGNORED}
)


class Concept(Base):
    """A canonical knowledge point; aliases point here."""

    __tablename__ = "concepts"
    __table_args__ = (
        UniqueConstraint("subject", "name", name="uq_concept_subject_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("concepts.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CONCEPT_STATUS_ACTIVE
    )

    children: Mapped[list["Concept"]] = relationship(back_populates="parent")
    parent: Mapped["Concept | None"] = relationship(
        back_populates="children", remote_side=[id]
    )
    aliases: Mapped[list["ConceptAlias"]] = relationship(
        back_populates="concept", cascade="all, delete-orphan"
    )


class ConceptAlias(Base):
    """One spoken/written name mapped onto a canonical concept."""

    __tablename__ = "concept_aliases"
    __table_args__ = (UniqueConstraint("alias", name="uq_concept_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    concept_id: Mapped[int] = mapped_column(ForeignKey("concepts.id"), nullable=False)
    alias: Mapped[str] = mapped_column(String(128), nullable=False)

    concept: Mapped[Concept] = relationship(back_populates="aliases")


# many-to-many: one question can exercise several knowledge points
question_concept = Table(
    "question_concept",
    Base.metadata,
    Column("question_id", ForeignKey("question.id"), primary_key=True),
    Column("concept_id", ForeignKey("concepts.id"), primary_key=True),
)
