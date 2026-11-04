"""BOOK + UNIT + WORD — 教材基本库（三科共用一套结构）。

一套结构装三科，不为学科分裂表（ROADMAP-0.2 追加需求第 1 条）：

- ``book``   一本书：学科 / 出版社 / 年级 / 学期 / 书名 / 来源说明。
- ``unit``   一本书里的一个单元：书 + 单元号 + 单元标题。
- ``word``   单元里的一个条目：提示（``cue``）/ 要写的（``answer``）/
  额外一列 ``extra``（英语音标或语文拼音，同一列，不按学科分表）。

Why the natural key on ``book``: 导入器是幂等的，重导同一份词表必须认回同一本书，
不能凭空长出一本。书名/出版社/年级/学期/学科五件套就是「同一本书」的定义，
改任何一件都算另一本书（口径写进 docs/WORDBANK-FORMAT.md）。

``word`` 的唯一键是 ``(unit_id, answer)``：同一个单元里同一个答案只该有一条；
不同单元出现同一个词是正常的（教材会复现），所以键里带 unit。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cairn.models.base import Base


class Book(Base):
    """一本教材。学科存中文名（英语/语文/数学），与 question.subject 口径一致。"""

    __tablename__ = "book"
    __table_args__ = (
        UniqueConstraint(
            "subject", "publisher", "grade", "semester", "name", name="uq_book_identity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(32), nullable=False)
    publisher: Mapped[str] = mapped_column(String(64), nullable=False)
    grade: Mapped[int] = mapped_column(nullable=False)
    semester: Mapped[str] = mapped_column(String(8), nullable=False)  # 上 / 下
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # where the words came from (URL / 纸质书页码 / 手工整理); NULL = 还没导入
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 第一次导入词表的时间（v10 起记录；占位空书 / 老库存量行保持 NULL）
    imported_at: Mapped[datetime | None] = mapped_column(nullable=True)

    units: Mapped[list["Unit"]] = relationship(
        back_populates="book",
        cascade="all, delete-orphan",
        order_by="Unit.unit_no",
    )


class Unit(Base):
    """书里的一个单元；``unit_no`` 是教材上的单元号（1、2、3…）。"""

    __tablename__ = "unit"
    __table_args__ = (UniqueConstraint("book_id", "unit_no", name="uq_unit_book_no"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id"), nullable=False)
    unit_no: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)

    book: Mapped[Book] = relationship(back_populates="units")
    words: Mapped[list["Word"]] = relationship(
        back_populates="unit",
        cascade="all, delete-orphan",
        order_by="Word.position",
    )


class Word(Base):
    """单元里的一个条目：提示 → 要写的，外加音标/拼音。

    ``cue`` 是孩子看到（或听到）的提示（中文意思、拼音）；``answer`` 是他要写出来的
    （英语单词、汉字）；``extra`` 是读音辅助（英语音标 / 语文拼音）。
    """

    __tablename__ = "word"
    __table_args__ = (UniqueConstraint("unit_id", "answer", name="uq_word_unit_answer"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("unit.id"), nullable=False)
    position: Mapped[int] = mapped_column(nullable=False, default=1)  # 单元内顺序
    cue: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)  # 音标 / 拼音

    unit: Mapped[Unit] = relationship(back_populates="words")
