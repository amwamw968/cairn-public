"""教材基本库：导入词表 + 页面视图 + 勾选加入错题库。

三科（英语/语文/数学）共用 book → unit → word 一套结构，学科只是 book.subject
的一个值（ROADMAP-0.2 追加需求第 1 条）。

三件事，都在这里，页面与路由不做业务判断：

1. ``import_book(session, book_dict)`` —— 幂等导入。认书靠自然键（学科+出版社+
   年级+学期+书名），认单元靠 (book, unit_no)，认条目靠 (unit, answer)。
   重导同一份词表：新增 0 条、跳过 N 条，行数不变。
2. ``build_books_view(...)`` —— 一次取全页面要的东西：左边书列表（带词数）、
   选中书、按单元分块的条目（每条带「已在错题库」标记）、搜索命中数、空书提示。
3. ``add_words_to_library(...)`` —— 勾选的条目交给 ``entry_service.create_error_items``
   批量入库（subject 取书上的学科，lang 由服务按 answer 判定，全部从 new 起步）。
4. ``generate_dictation(...)`` —— 把一个单元的英语单词整批变成听写题（只按单元，
   不做整本）：题面是中文提示、答案是英文词。同一 learner 下 content 与 answer
   都相同的题已存在就跳过，所以连点两次不会把同样的词录两遍。
   ``unit_dictation_state(...)`` 给页面回「这一单元已生成几条」，判据与生成同一套。

「已加入」标记用 ``entry_service.answer_key`` —— 和批量入库同一套去重键，所以
屏幕上标了「已加入」的条目，提交时一定被跳过，不会出现「看着能加、点了没加」。

不编造内容：语文/数学只建空书（``ensure_default_books``），条目等真实词表导入。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from cairn import dict as dict_service
from cairn import entry_service
from cairn.entry_service import BatchEntryResult, EntryPayload, ValidationError
from cairn.models import (
    REVIEW_STATUS_NEW,
    Book,
    ErrorItem,
    Learner,
    Question,
    Unit,
    Word,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.parse import detect_lang

logger = logging.getLogger(__name__)

__all__ = [
    "BookDataError",
    "BooksView",
    "BookOption",
    "ImportResult",
    "UnitDictationState",
    "UnitSection",
    "WordRow",
    "add_words_to_library",
    "build_books_view",
    "ensure_default_books",
    "generate_dictation",
    "grade_label",
    "import_book",
    "library_answer_keys",
    "unit_dictation_state",
]

# 学科 → 题型：沿用录入页的中文口径（英语=单词、语文=词语），数学条目是知识点
QUESTION_TYPE_BY_SUBJECT = {"英语": "单词", "语文": "词语", "数学": "知识点"}
_DEFAULT_QUESTION_TYPE = "条目"

# 教材 → 听写题：题面是中文提示，答案是英文词；朗读留空（英文词默认念它自己）。
DICTATION_SUBJECT = "英语"
DICTATION_QUESTION_TYPE = "听写"
DICTATION_LANG = "en"

# 只建书、不建条目：书是真实存在的（人教版五年级上册语文/数学），词表等 9 哥给真数据。
DEFAULT_BOOKS: tuple[dict[str, object], ...] = (
    {
        "subject": "语文",
        "publisher": "人民教育出版社",
        "grade": 5,
        "semester": "上",
        "name": "人教版 五年级上册 语文",
        "source_note": "还没有导入词表：等真实的生字词表，不编造内容",
    },
    {
        "subject": "数学",
        "publisher": "人民教育出版社",
        "grade": 5,
        "semester": "上",
        "name": "人教版 五年级上册 数学",
        "source_note": "还没有导入词表：等真实的单元目录与知识点，不编造内容",
    },
)


class BookDataError(ValidationError):
    """词表数据不合规（缺字段、空答案、单元号不是数字…）。"""


@dataclass(frozen=True)
class ImportResult:
    """One import run: how many rows landed, how many were already there.

    ``needs_manual``（词典卡）：缺 cue 且词典也补不到的条目（只记 answer，
    不落库、不加库字段），交给家长人工补全。
    """

    book_id: int
    book_name: str
    created: int
    skipped: int
    units_created: int
    needs_manual: tuple[str, ...] = ()

    def summary(self) -> str:
        text = f"新增 {self.created} 条、跳过 {self.skipped} 条"
        if self.needs_manual:
            text += f"、待人工补全 {len(self.needs_manual)} 条"
        return text


@dataclass(frozen=True)
class WordRow:
    """One entry as the page shows it."""

    id: int
    index: int  # 复选框在整页里的下标（跨单元连续）
    position: int
    cue: str
    answer: str
    extra: str | None
    added: bool  # 这个答案已经在错题库里


@dataclass(frozen=True)
class UnitSection:
    unit_no: int
    title: str | None
    words: tuple[WordRow, ...]


@dataclass(frozen=True)
class UnitDictationState:
    """一个单元在页面上要显示的两件事：单元 id（按钮往哪 POST）与已生成条数。"""

    unit_no: int
    unit_id: int
    generated: int


@dataclass(frozen=True)
class BookOption:
    """Left-hand book list row."""

    id: int
    subject: str
    name: str
    grade_label: str
    word_count: int
    selected: bool


@dataclass(frozen=True)
class SelectedBook:
    id: int
    subject: str
    name: str
    grade_label: str
    source_note: str | None


@dataclass(frozen=True)
class BooksView:
    books: tuple[BookOption, ...]
    selected: SelectedBook | None
    units: tuple[UnitSection, ...]
    query: str
    matched: int
    # 一整本书没有词（空书 / 搜不到是两件事，文案不同）
    empty_message: str | None = None
    no_match_message: str | None = None


def grade_label(grade: int, semester: str) -> str:
    """5 / 上 → 五年级上。"""
    numerals = "〇一二三四五六七八九十"
    if 0 <= grade <= 10:
        number = numerals[grade]
    else:
        number = "十" + numerals[grade - 10]
    return f"{number}年级{semester}"


# --------------------------------------------------------------------------- 导入


def import_book(session: Session, book_dict: dict) -> ImportResult:
    """幂等导入一本书；返回新增/跳过条数。

    结构见 ``docs/WORDBANK-FORMAT.md``。相同的书（学科+出版社+年级+学期+书名）
    重复导入不会产生第二本书、第二个单元或第二条同答案条目：已存在的按「跳过」计数。
    整个导入一个事务，中途任何一条数据不合规就整批回滚（不会留下半本书）。
    """
    identity = _book_identity(book_dict)
    book = session.scalars(
        select(Book).where(
            Book.subject == identity["subject"],
            Book.publisher == identity["publisher"],
            Book.grade == identity["grade"],
            Book.semester == identity["semester"],
            Book.name == identity["name"],
        )
    ).first()
    units_created = 0
    created = 0
    skipped = 0
    needs_manual: list[str] = []
    try:
        if book is None:
            book = Book(
                **identity,
                source_note=_optional_text(book_dict.get("source_note")),
                imported_at=datetime.now(timezone.utc),
            )
            session.add(book)
            session.flush()
        elif not book.source_note:
            book.source_note = _optional_text(book_dict.get("source_note"))

        for raw_unit in _units(book_dict):
            unit_no, title, raw_words = _unit_fields(raw_unit)
            unit = session.scalars(
                select(Unit).where(Unit.book_id == book.id, Unit.unit_no == unit_no)
            ).first()
            if unit is None:
                unit = Unit(book_id=book.id, unit_no=unit_no, title=title)
                session.add(unit)
                session.flush()
                units_created += 1
            elif title and not unit.title:
                unit.title = title

            seen = _unit_answer_keys(session, unit.id)
            for index, raw_word in enumerate(raw_words, start=1):
                cue, answer, extra, position = _word_fields(raw_word, index)
                if not cue:
                    cue = _autofill_cue(book.subject, answer)
                    if cue is None:
                        # 词典卡：缺 cue 补不到 → needs_manual（内存标记 + 日志），
                        # 该行不落库（word.cue 非空约束），缺 answer 仍整批报错。
                        logger.warning(
                            "needs_manual: %s 第 %d 条 %r 缺 cue 且词典补不到",
                            book.name,
                            index,
                            answer,
                        )
                        needs_manual.append(answer)
                        continue
                key = entry_service.answer_key(answer)
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                session.add(
                    Word(
                        unit_id=unit.id,
                        position=position,
                        cue=cue,
                        answer=answer,
                        extra=extra,
                    )
                )
                created += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ImportResult(
        book_id=book.id,
        book_name=book.name,
        created=created,
        skipped=skipped,
        units_created=units_created,
        needs_manual=tuple(needs_manual),
    )


def ensure_default_books(session: Session) -> int:
    """建两本空书（人教版五上语文/数学）；已存在就什么也不做。返回新建本数。"""
    created = 0
    for data in DEFAULT_BOOKS:
        identity = _book_identity(data)
        exists = session.scalars(
            select(Book).where(
                Book.subject == identity["subject"],
                Book.publisher == identity["publisher"],
                Book.grade == identity["grade"],
                Book.semester == identity["semester"],
                Book.name == identity["name"],
            )
        ).first()
        if exists is not None:
            continue
        session.add(Book(**identity, source_note=_optional_text(data.get("source_note"))))
        created += 1
    if created:
        session.commit()
    return created


def _book_identity(data: dict) -> dict[str, object]:
    subject = _required_text(data, "subject")
    publisher = _required_text(data, "publisher")
    name = _required_text(data, "name")
    semester = _required_text(data, "semester")
    grade = data.get("grade")
    if isinstance(grade, str) and grade.strip().isdigit():
        grade = int(grade.strip())
    if not isinstance(grade, int) or isinstance(grade, bool) or not 1 <= grade <= 12:
        raise BookDataError(f"grade 必须是 1–12 的整数，收到 {grade!r}")
    if semester not in ("上", "下"):
        raise BookDataError(f"semester 只能是 上/下，收到 {semester!r}")
    return {
        "subject": subject,
        "publisher": publisher,
        "grade": grade,
        "semester": semester,
        "name": name,
    }


def _units(book_dict: dict) -> list:
    units = book_dict.get("units")
    if units is None:
        return []
    if not isinstance(units, list):
        raise BookDataError("units 必须是数组")
    return units


def _unit_fields(raw_unit: dict) -> tuple[int, str | None, list]:
    if not isinstance(raw_unit, dict):
        raise BookDataError(f"单元必须是一个对象，收到 {type(raw_unit).__name__}")
    unit_no = raw_unit.get("unit_no", raw_unit.get("unit"))
    if isinstance(unit_no, str) and unit_no.strip().isdigit():
        unit_no = int(unit_no.strip())
    if not isinstance(unit_no, int) or isinstance(unit_no, bool) or unit_no < 1:
        raise BookDataError(f"unit_no 必须是正整数，收到 {unit_no!r}")
    title = _optional_text(raw_unit.get("title"))
    words = raw_unit.get("words", [])
    if not isinstance(words, list):
        raise BookDataError(f"单元 {unit_no} 的 words 必须是数组")
    return unit_no, title, words


def _word_fields(raw_word: dict, index: int) -> tuple[str | None, str, str | None, int]:
    """One entry: (cue, answer, extra, position). 兼容三色卡字段名（word/ipa/cn）。

    缺 cue 不再在这里报错（词典卡）：cue 可为 None，由 ``import_book`` 先试
    词典自动补（``_autofill_cue``），补不到记 needs_manual；缺 answer 仍然报错。
    """
    if not isinstance(raw_word, dict):
        raise BookDataError(f"词条必须是一个对象，收到 {type(raw_word).__name__}")
    cue = _optional_text(raw_word.get("cue", raw_word.get("cn")))
    answer = _optional_text(raw_word.get("answer", raw_word.get("word")))
    if not answer:
        raise BookDataError(f"第 {index} 条缺 answer（要写的）: {raw_word!r}")
    extra = _optional_text(raw_word.get("extra", raw_word.get("ipa")))
    position = raw_word.get("position", index)
    if not isinstance(position, int) or isinstance(position, bool):
        position = index
    return cue, answer, extra, position


def _autofill_cue(subject: str, answer: str) -> str | None:
    """词典自动补 cue：语文补拼音、英语补中文释义；补不到（或词典缺失）返回 None。"""
    if subject == "语文":
        pinyin = dict_service.lookup_zh_pinyin(answer)
        return pinyin[0] if pinyin else None
    if subject == "英语":
        hit = dict_service.lookup_en(answer)
        if hit and (hit.get("translation") or "").strip():
            return hit["translation"].strip()
    return None


def _unit_answer_keys(session: Session, unit_id: int) -> set[str]:
    rows = session.scalars(select(Word.answer).where(Word.unit_id == unit_id))
    return {entry_service.answer_key(answer) for answer in rows}


def _required_text(data: dict, field: str) -> str:
    value = _optional_text(data.get(field))
    if not value:
        raise BookDataError(f"缺字段: {field}")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --------------------------------------------------------------------------- 视图


def build_books_view(
    session: Session,
    learner_id: int,
    book_id: int | None = None,
    query: str | None = None,
) -> BooksView:
    """页面一次取全：书列表 / 选中书 / 按单元分块的条目 / 搜索与空书文案。"""
    books = session.scalars(select(Book).order_by(Book.subject.asc(), Book.name.asc())).all()
    counts = _word_counts(session)
    added_keys = library_answer_keys(session, learner_id)
    selected_book = next((book for book in books if book.id == book_id), None)
    if selected_book is None:
        # 没指定就落到第一本**有词**的书：空书留在左栏，不要一进门就是空页面
        selected_book = next(
            (book for book in books if counts.get(book.id, 0) > 0), None
        ) or (books[0] if books else None)

    options = tuple(
        BookOption(
            id=book.id,
            subject=book.subject,
            name=book.name,
            grade_label=grade_label(book.grade, book.semester),
            word_count=counts.get(book.id, 0),
            selected=selected_book is not None and book.id == selected_book.id,
        )
        for book in books
    )
    if selected_book is None:
        return BooksView(
            books=options,
            selected=None,
            units=(),
            query="",
            matched=0,
            empty_message="还没有任何教材，先用导入器加一本",
        )

    selected = SelectedBook(
        id=selected_book.id,
        subject=selected_book.subject,
        name=selected_book.name,
        grade_label=grade_label(selected_book.grade, selected_book.semester),
        source_note=selected_book.source_note,
    )
    text = (query or "").strip()
    total = counts.get(selected_book.id, 0)
    if total == 0:
        return BooksView(
            books=options,
            selected=selected,
            units=(),
            query=text,
            matched=0,
            empty_message="这本书还没有词表，等导入",
        )

    units = session.scalars(
        select(Unit).where(Unit.book_id == selected_book.id).order_by(Unit.unit_no.asc())
    ).all()
    sections: list[UnitSection] = []
    index = 0
    matched = 0
    for unit in units:
        words = _unit_words(session, unit.id, text)
        rows: list[WordRow] = []
        for word in words:
            rows.append(
                WordRow(
                    id=word.id,
                    index=index,
                    position=word.position,
                    cue=word.cue,
                    answer=word.answer,
                    extra=word.extra,
                    added=entry_service.answer_key(word.answer) in added_keys,
                )
            )
            index += 1
            matched += 1
        if rows:
            sections.append(UnitSection(unit_no=unit.unit_no, title=unit.title, words=tuple(rows)))

    no_match = None
    if text and matched == 0:
        no_match = f"没有找到和「{text}」相关的词条"
    return BooksView(
        books=options,
        selected=selected,
        units=tuple(sections),
        query=text,
        matched=matched,
        no_match_message=no_match,
    )


def _unit_words(session: Session, unit_id: int, query: str) -> list[Word]:
    statement = select(Word).where(Word.unit_id == unit_id)
    if query:
        like = f"%{query.casefold()}%"
        statement = statement.where(
            or_(
                func.lower(Word.cue).like(like),
                func.lower(Word.answer).like(like),
                func.lower(func.coalesce(Word.extra, "")).like(like),
            )
        )
    return list(session.scalars(statement.order_by(Word.position.asc(), Word.id.asc())))


def _word_counts(session: Session) -> dict[int, int]:
    rows = session.execute(
        select(Unit.book_id, func.count(Word.id))
        .select_from(Unit)
        .outerjoin(Word, Word.unit_id == Unit.id)
        .group_by(Unit.book_id)
    )
    return {int(book_id): int(count) for book_id, count in rows}


# --------------------------------------------------------------------------- 入库


def library_answer_keys(session: Session, learner_id: int) -> set[str]:
    """这个 learner 错题库里已有的答案键（与批量入库同一套键）。"""
    rows = session.scalars(
        select(Question.answer)
        .join(ErrorItem, ErrorItem.question_id == Question.id)
        .where(ErrorItem.learner_id == learner_id)
    )
    return {entry_service.answer_key(answer) for answer in rows if answer}


def add_words_to_library(
    session: Session, learner_id: int, word_ids: list[int]
) -> BatchEntryResult:
    """把勾选的条目交给 entry 服务批量入库（去重、事务、计数都复用那一套）。"""
    ids = list(dict.fromkeys(word_ids))  # 同一条勾两次只算一次，保持勾选顺序
    if not ids:
        raise BookDataError("没有选中的条目")
    rows = session.execute(
        select(Word, Book)
        .join(Unit, Unit.id == Word.unit_id)
        .join(Book, Book.id == Unit.book_id)
        .where(Word.id.in_(ids))
    ).all()
    by_id = {word.id: (word, book) for word, book in rows}
    payloads: list[EntryPayload] = []
    for word_id in ids:
        found = by_id.get(word_id)
        if found is None:
            raise BookDataError(f"词条不存在: word_id={word_id}")
        word, book = found
        payloads.append(_payload_for(word, book))
    return entry_service.create_error_items(session, learner_id, payloads)


def _payload_for(word: Word, book: Book) -> EntryPayload:
    """一个教材条目 → 录入 payload。

    lang 留空：由 entry 服务按 answer 判定（英文词 → en，汉字 → zh）。
    audio_text 只给中文词（拼音）；英文词留空，服务层会把单词本身当朗读 cue。
    """
    lang = detect_lang(word.answer)
    extra = (word.extra or "").strip()
    return EntryPayload(
        subject=book.subject,
        question_type=QUESTION_TYPE_BY_SUBJECT.get(book.subject, _DEFAULT_QUESTION_TYPE),
        content=word.cue,
        answer=word.answer,
        lang=None,
        audio_text=extra if lang == "zh" and extra else None,
    )


# --------------------------------------------------------------------------- 听写出题


def generate_dictation(
    session: Session, learner_id: int, unit_id: int
) -> tuple[int, int]:
    """把一个单元的单词整批变成听写题，返回 ``(created, skipped)``。

    只按单元出（不做整本）：按 ``Word.position`` 顺序，每条建一条 QUESTION
    （subject=英语、question_type=听写、content=中文提示、answer=英文词、
    lang=en、audio_text 留空——英文词默认念单词本身）+ 一条从 new 起步的
    ERROR_ITEM，题才进得了 /practice。

    幂等判据：同一 learner 下 content 与 answer 都相同的题已经存在 → 跳过并计数。
    判据不按题型收窄（卡片写的就是"同 content 同 answer"），所以家长之前手录过
    的同一个词也算已生成，不会再录一遍。整个单元一个事务，中途出错整批回滚。
    """
    if learner_id is None:
        raise BookDataError("missing required field: learner_id")
    if session.get(Learner, learner_id) is None:
        raise BookDataError(f"learner does not exist: learner_id={learner_id}")
    unit = session.get(Unit, unit_id)
    if unit is None:
        raise BookDataError(f"单元不存在: unit_id={unit_id}")

    words = session.scalars(
        select(Word)
        .where(Word.unit_id == unit_id)
        .order_by(Word.position.asc(), Word.id.asc())
    ).all()
    existing = _existing_question_pairs(session, learner_id)
    created = 0
    skipped = 0
    try:
        for word in words:
            key = _dictation_key(word.cue, word.answer)
            if key in existing:
                skipped += 1
                continue
            existing.add(key)  # 同一单元里重复的词也只录一条
            question = Question(
                learner_id=learner_id,
                subject=DICTATION_SUBJECT,
                grade_level="primary",
                question_type=DICTATION_QUESTION_TYPE,
                content=word.cue,
                answer=word.answer,
                lang=DICTATION_LANG,
                audio_text=None,
                source="wordbank",
            )
            session.add(question)
            session.flush()  # assign question.id
            session.add(
                ErrorItem(
                    learner_id=learner_id,
                    question_id=question.id,
                    wrong_count=1,
                    review_status=REVIEW_STATUS_NEW,
                    confidence=1.0,
                )
            )
            created += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    return created, skipped


def unit_dictation_state(
    session: Session, learner_id: int, book_id: int
) -> tuple[UnitDictationState, ...]:
    """这本书每个单元「已生成几条」——判据与 ``generate_dictation`` 完全一致。

    页面用 unit_no 索引（模板循环里只有 unit_no），顺手带上 unit_id 给按钮拼 POST 地址。
    """
    units = session.scalars(
        select(Unit).where(Unit.book_id == book_id).order_by(Unit.unit_no.asc())
    ).all()
    if not units:
        return ()
    existing = _existing_question_pairs(session, learner_id)
    counts: dict[int, int] = {}
    rows = session.execute(
        select(Word.unit_id, Word.cue, Word.answer).where(
            Word.unit_id.in_([unit.id for unit in units])
        )
    )
    for unit_id, cue, answer in rows:
        if _dictation_key(cue, answer) in existing:
            counts[unit_id] = counts.get(unit_id, 0) + 1
    return tuple(
        UnitDictationState(
            unit_no=unit.unit_no, unit_id=unit.id, generated=counts.get(unit.id, 0)
        )
        for unit in units
    )


def _dictation_key(content: str | None, answer: str | None) -> tuple[str, str]:
    """幂等键：content 与 answer 都相同才算同一条（两边去首尾空白）。"""
    return ((content or "").strip(), (answer or "").strip())


def _existing_question_pairs(session: Session, learner_id: int) -> set[tuple[str, str]]:
    """这个 learner 已经有的 (content, answer) 组合。"""
    rows = session.execute(
        select(Question.content, Question.answer).where(
            Question.learner_id == learner_id
        )
    )
    return {_dictation_key(content, answer) for content, answer in rows}


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m cairn.book_service <json> [--subject 英语] [--db URL]``"""
    parser = argparse.ArgumentParser(
        prog="python -m cairn.book_service",
        description="把一份教材词表 JSON 导入 cairn（幂等：重导只增加新条目）。",
    )
    parser.add_argument("json_path", help="词表 JSON 路径（结构见 docs/WORDBANK-FORMAT.md）")
    parser.add_argument(
        "--subject",
        default=None,
        help="文件里没有 subject 时用它补（如 英语）；有则覆盖",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="数据库 URL；不给就读 CAIRN_DB_URL，再退到 ./cairn.db",
    )
    args = parser.parse_args(argv)

    path = Path(args.json_path).expanduser()
    if not path.is_file():
        print(f"找不到词表文件: {path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读不懂这个词表文件: {exc}", file=sys.stderr)
        return 2
    if args.subject:
        data["subject"] = args.subject

    db_url = args.db or os.environ.get("CAIRN_DB_URL") or "sqlite:///./cairn.db"
    engine = make_engine(db_url)
    try:
        init_db(engine)
        with make_session_factory(engine)() as session:
            try:
                result = import_book(session, data)
            except BookDataError as exc:
                print(f"导入失败：{exc}", file=sys.stderr)
                return 1
    finally:
        engine.dispose()
    print(f"{result.book_name}：{result.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
