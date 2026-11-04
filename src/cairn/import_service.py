"""Restore a backup produced by :mod:`cairn.export_service`.

导出是契约：这个模块只读那份 JSON，不改字段、不改格式、不猜字段。

口径（0.2 卡13，Lead 已定）：

- **只导入空库**。目标库有任何业务数据就整批拒绝，并说清「当前库里有 N 道题」。
  不做合并、不做冲突解决 —— 半对半错的数据比没有数据更坏。
- **先校验后写库**：``schema_version`` 不能高于本程序支持的版本，必填字段缺一个就整批拒绝。
  校验全部在写第一行之前做完，所以「导入一半」在结构上不可能发生。
- **一次事务**：写库过程中任何失败（外键、主键冲突）整体回滚，库回到导入前的样子。
- **id 原样恢复**：``question_knowledge_link`` / ``question_concept`` 这类关联表只存 id，
  不保留 id 就对不上原来的题。

表顺序按外键依赖排：learner → knowledge_point → concepts → concept_aliases → question →
question_knowledge_link → question_concept → error_item → review（+ 当前导出不含的
book/unit/word/exam/scan，出现时按同一套规则兜住）。

自引用列（``knowledge_point.parent_id`` / ``concepts.parent_id``）分两段写：先按 NULL 落行，
再逐条 UPDATE 回去 —— SQLite 的外键是立即约束，而备份里父子的 id 大小没有保证。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Date, DateTime, Table, delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from cairn.migrations import SCHEMA_VERSION
from cairn.models import (
    Book,
    Concept,
    ConceptAlias,
    Collection,
    CollectionItem,
    CollectionPracticeItem,
    CollectionVersion,
    CollectionVersionQuestion,
    ErrorItem,
    Exam,
    KnowledgePoint,
    Learner,
    Question,
    RecapAttempt,
    Review,
    Scan,
    Unit,
    Word,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.models.concept import question_concept
from cairn.models.question import question_knowledge_link


class BackupError(Exception):
    """备份不能导入（格式不对、版本太高、目标库不空、写库失败）。文案给人看。"""


@dataclass(frozen=True)
class ImportReport:
    """各表写入了多少行。``counts`` 按写库顺序排列。"""

    schema_version: int
    exported_at: str | None
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def lines(self) -> list[tuple[str, int]]:
        """(中文表名, 条数)，只列有内容的表 —— 页面用它渲染结果表。"""
        return [(TABLES[key], count) for key, count in self.counts.items() if count]

    def summary(self) -> str:
        if not self.total:
            return "备份里没有可恢复的行"
        return "、".join(f"{name} {count} 条" for name, count in self.lines())


# 页面/命令行用的中文表名。
TABLES: dict[str, str] = {
    "learner": "学习者",
    "knowledge_point": "知识点",
    "concepts": "知识点（新表）",
    "concept_aliases": "知识点别名",
    "question": "题目",
    "question_knowledge_link": "题目-知识点关联",
    "question_concept": "题目-知识点关联（新表）",
    "error_item": "错题",
    "review": "复习记录",
    "recap_attempt": "复述记录",
    "learning_collection": "题集",
    "learning_collection_item": "题集条目",
    "learning_collection_version": "题集版本",
    "learning_collection_version_question": "题集版本关联",
    "learning_collection_practice_item": "题集练习来源",
    "book": "教材",
    "unit": "单元",
    "word": "词条",
    "exam": "考试",
    "scan": "照片",
}

# (备份里的键, 表, 自引用列)。顺序即写库顺序，必须满足外键依赖。
_TABLES: tuple[tuple[str, Table, str | None], ...] = (
    ("learner", Learner.__table__, None),
    ("knowledge_point", KnowledgePoint.__table__, "parent_id"),
    ("concepts", Concept.__table__, "parent_id"),
    ("concept_aliases", ConceptAlias.__table__, None),
    ("question", Question.__table__, "variant_of"),
    ("question_knowledge_link", question_knowledge_link, None),
    ("question_concept", question_concept, None),
    ("error_item", ErrorItem.__table__, None),
    ("review", Review.__table__, None),
    ("recap_attempt", RecapAttempt.__table__, None),
    ("learning_collection", Collection.__table__, None),
    ("learning_collection_item", CollectionItem.__table__, None),
    ("learning_collection_version", CollectionVersion.__table__, None),
    ("learning_collection_version_question", CollectionVersionQuestion.__table__, None),
    ("learning_collection_practice_item", CollectionPracticeItem.__table__, None),
    # 当前导出不含这五张表；备份里出现时照同一套规则恢复，不出现就一张都不动。
    ("book", Book.__table__, None),
    ("unit", Unit.__table__, None),
    ("word", Word.__table__, None),
    ("exam", Exam.__table__, None),
    ("scan", Scan.__table__, None),
)

_TABLE_BY_KEY: dict[str, Table] = {key: table for key, table, _ in _TABLES}

# 「空库」只判业务表。book/unit/word 不算：web 启动会自动建两本空书，
# 把它们算进去，导入就永远进不来（而备份本来就不含教材表）。
_CONTENT_KEYS: tuple[str, ...] = (
    "question",
    "knowledge_point",
    "concepts",
    "concept_aliases",
    "question_knowledge_link",
    "question_concept",
    "error_item",
    "review",
    "recap_attempt",
    "learning_collection",
    "learning_collection_item",
    "learning_collection_version",
    "learning_collection_version_question",
    "learning_collection_practice_item",
    "exam",
    "scan",
)


def _required(column: Any) -> bool:
    """必填 = NOT NULL 且没有默认值、也不是自增主键。

    有默认值的列（如 ``created_at`` / ``review_status``）缺了不算错 —— 写库时走默认值。
    """
    return (
        not column.nullable
        and not column.primary_key
        and column.default is None
        and column.server_default is None
    )


def _coerce(key: str, column: Any, value: Any) -> Any:
    """JSON 只有字符串；DateTime / Date 列要还原成 Python 对象，否则绑定器直接报错。"""
    if isinstance(value, str):
        if isinstance(column.type, DateTime):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise BackupError(
                    f"{TABLES[key]} 的时间格式无法识别：{value!r}（应为 ISO 时间）"
                ) from exc
        if isinstance(column.type, Date):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise BackupError(
                    f"{TABLES[key]} 的日期格式无法识别：{value!r}（应为 YYYY-MM-DD）"
                ) from exc
    return value


def _validate_row(
    key: str, table: Table, self_fk: str | None, index: int, row: Any
) -> dict[str, Any]:
    """一行校验 + 类型还原。不合格抛 BackupError，此时库里一个字节都还没写。"""
    if not isinstance(row, dict):
        raise BackupError(f"{TABLES[key]} 第 {index} 条不是对象。")
    columns = {column.name: column for column in table.columns}
    unknown = sorted(set(row) - set(columns))
    if unknown:
        raise BackupError(
            f"{TABLES[key]} 第 {index} 条有本程序不认识的字段：{'、'.join(unknown)}。"
            "这份备份可能来自更新的版本，请先升级程序。"
        )
    for name, column in columns.items():
        if _required(column) and row.get(name) is None:
            raise BackupError(
                f"{TABLES[key]} 第 {index} 条缺必填字段 {name}，整批不导入。"
            )
    if self_fk and row.get(self_fk) is not None and row.get("id") is None:
        raise BackupError(
            f"{TABLES[key]} 第 {index} 条有父级（{self_fk}）但缺 id，无法恢复层级。"
        )
    # None 直接省掉：可空列省掉就是 NULL，有默认值的列省掉就走默认值。
    return {
        name: _coerce(key, columns[name], value)
        for name, value in row.items()
        if value is not None
    }


def _validate(document: Any) -> tuple[int, str | None, dict[str, list[dict[str, Any]]]]:
    """整份文档的校验；返回 (schema_version, exported_at, 每表已还原的行)。"""
    if not isinstance(document, dict):
        raise BackupError("备份文件格式不对：最外层应该是一个 JSON 对象。")
    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BackupError("备份文件里没有可用的 schema_version，无法确认它能不能导入。")
    if version > SCHEMA_VERSION:
        raise BackupError(
            f"备份来自更新的版本（schema_version={version}），本程序只支持到 "
            f"{SCHEMA_VERSION}。请先升级程序，再导入这份备份。"
        )

    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for key, table, self_fk in _TABLES:
        raw = document.get(key)
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise BackupError(
                f"备份里的 {key} 应该是一个列表，实际是 {type(raw).__name__}。"
            )
        rows_by_table[key] = [
            _validate_row(key, table, self_fk, index, row)
            for index, row in enumerate(raw, start=1)
        ]

    if not rows_by_table["learner"]:
        raise BackupError("备份里没有学习者（learner）记录，恢复出来会是一个不能用的库，已拒绝。")

    _validate_learning_ownership(rows_by_table)
    _validate_collection_rows(rows_by_table)
    exported_at = document.get("exported_at")
    return version, exported_at if isinstance(exported_at, str) else None, rows_by_table


def _validate_learning_ownership(rows: dict[str, list[dict[str, Any]]]) -> None:
    """Learning records must not refer to another learner's question."""
    def by_id(records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        result = {}
        for row in records:
            key = row.get("id")
            if key is not None:
                if type(key) is not int or key <= 0 or key in result:
                    raise BackupError("学习数据编号无效或重复，整批不导入。")
                result[key] = row
        return result
    questions = by_id(rows["question"])
    items = by_id(rows["error_item"])
    reviews = by_id(rows["review"])
    for question in rows["question"]:
        variant_id = question.get("variant_of")
        if variant_id is not None:
            original = questions.get(variant_id) if type(variant_id) is int else None
            if original is None or original.get("learner_id") != question.get("learner_id"):
                raise BackupError("变式题与原题的学习者归属不一致，整批不导入。")
    for item in rows["error_item"]:
        question_id, owner = item.get("question_id"), item.get("learner_id")
        question = questions.get(question_id) if type(question_id) is int else None
        if (type(owner) is not int or question is None or type(question.get("learner_id")) is not int
                or question.get("learner_id") != owner):
            raise BackupError("错题与题目的学习者归属不一致，整批不导入。")
    for attempt in rows["recap_attempt"]:
        item_id, owner = attempt.get("error_item_id"), attempt.get("learner_id")
        item = items.get(item_id) if type(item_id) is int else None
        if type(owner) is not int or item is None or owner != item.get("learner_id"):
            raise BackupError("复述记录的学习者归属不一致，整批不导入。")
        review_id = attempt.get("review_id")
        if review_id is not None:
            review = reviews.get(review_id) if type(review_id) is int else None
            if review is None or review.get("error_item_id") != item_id:
                raise BackupError("复述记录引用了不匹配的复习记录，整批不导入。")


def _validate_collection_rows(rows: dict[str, list[dict[str, Any]]]) -> None:
    """Reject cross-owner references or damaged snapshots before writing anything."""
    from cairn import collection_package, collection_service
    import hashlib
    import re

    collections = rows["learning_collection"]
    items = rows["learning_collection_item"]
    versions = rows["learning_collection_version"]
    links = rows["learning_collection_version_question"]
    practice_items = rows["learning_collection_practice_item"]
    if not (collections or items or versions or links or practice_items):
        return

    def indexed(records: list[dict], label: str) -> dict[int, dict]:
        result = {}
        for record in records:
            key = record.get("id")
            if type(key) is not int or key <= 0 or key in result:
                raise BackupError(f"{label}的编号无效或重复，整批不导入。")
            result[key] = record
        return result

    owners = indexed(rows["learner"], "学习者")
    questions = indexed(rows["question"], "题目")
    collections_by_id = indexed(collections, "题集")
    versions_by_id = indexed(versions, "题集版本")
    try:
        for collection in collections:
            if collection.get("learner_id") not in owners:
                raise BackupError("题集缺少所属学习者，整批不导入。")
            collection_service._string(collection.get("name"), "名称", 120)
            collection_service._string(collection.get("subject"), "学科", 32)
            collection_service._string(collection.get("author"), "署名", 120)
            collection_service._string(collection.get("source"), "来源", 300)
            if collection.get("license") not in collection_service.LICENSES:
                raise BackupError("题集许可无效，整批不导入。")
            collection_service._tags(json.loads(collection.get("tags_json", "[]")))
            revision = collection.get("revision", 0)
            if type(revision) is not int or revision < 0:
                raise BackupError("题集修订号无效，整批不导入。")
        item_pairs = set()
        counts: dict[int, int] = {}
        for item in items:
            cid, qid, position = item.get("collection_id"), item.get("question_id"), item.get("position")
            collection, question = collections_by_id.get(cid), questions.get(qid)
            if (collection is None or question is None or question.get("learner_id") != collection["learner_id"]
                    or type(position) is not int or position < 0 or (cid, qid) in item_pairs):
                raise BackupError("题集条目的归属或顺序无效，整批不导入。")
            item_pairs.add((cid, qid))
            counts[cid] = counts.get(cid, 0) + 1
            if counts[cid] > collection_service.MAX_ITEMS:
                raise BackupError("题集条目过多，整批不导入。")
            collection_service._tags(json.loads(item.get("tags_json", "[]")))
            collection_service._string(item.get("source"), "题目来源", 300)
            if item.get("difficulty") is not None:
                collection_service._string(item["difficulty"], "难度", 64, required=False)
        version_questions: dict[int, set[int]] = {}
        version_pairs = set()
        for version in versions:
            cid, number = version.get("collection_id"), version.get("number")
            if (cid not in collections_by_id or type(number) is not int or number < 1
                    or (cid, number) in version_pairs):
                raise BackupError("题集版本编号无效，整批不导入。")
            version_pairs.add((cid, number))
            digest = version.get("content_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise BackupError("题集版本摘要无效，整批不导入。")
            snapshot = version.get("snapshot_json")
            if version.get("revoked_at") is not None:
                if snapshot is not None:
                    raise BackupError("已撤销的题集版本仍含内容，整批不导入。")
                version_questions[version["id"]] = set()
                continue
            if not isinstance(snapshot, str) or hashlib.sha256(snapshot.encode("utf-8")).hexdigest() != digest:
                raise BackupError("题集版本内容校验失败，整批不导入。")
            document = collection_package._load_json(snapshot.encode("utf-8"))
            if not isinstance(document, dict) or set(document) != {"manifest", "items", "question_ids"}:
                raise BackupError("题集版本格式无效，整批不导入。")
            normalized = collection_package.validate(document["manifest"], document["items"])
            question_ids = document["question_ids"]
            if (normalized != {"manifest": document["manifest"], "items": document["items"]}
                    or document["manifest"]["version"] != number
                    or not isinstance(question_ids, list) or len(question_ids) != len(document["items"])
                    or any(type(qid) is not int or qid <= 0 for qid in question_ids)
                    or len(set(question_ids)) != len(question_ids)):
                raise BackupError("题集版本内容与编号不符，整批不导入。")
            collection_package._serialize(normalized)
            version_questions[version["id"]] = set(question_ids)
        seen_links = set()
        actual_questions: dict[int, set[int]] = {}
        for link in links:
            vid, qid = link.get("version_id"), link.get("question_id")
            version, question = versions_by_id.get(vid), questions.get(qid)
            collection = collections_by_id.get(version["collection_id"]) if version else None
            if (version is None or question is None or collection is None
                    or question.get("learner_id") != collection["learner_id"] or (vid, qid) in seen_links):
                raise BackupError("题集版本引用无效或跨学习者，整批不导入。")
            seen_links.add((vid, qid))
            actual_questions.setdefault(vid, set()).add(qid)
        if any(actual_questions.get(vid, set()) != ids for vid, ids in version_questions.items()):
            raise BackupError("题集版本引用不完整，整批不导入。")
        indexed(practice_items, "题集练习来源")
        practice_pairs, copies = set(), set()
        learning_counts: dict[tuple[int, int], int] = {}
        for entry in rows["error_item"]:
            pair = (entry.get("learner_id"), entry.get("question_id"))
            learning_counts[pair] = learning_counts.get(pair, 0) + 1
        for item in practice_items:
            vid, source_id, copy_id = item.get("version_id"), item.get("source_question_id"), item.get("question_id")
            version = versions_by_id.get(vid)
            collection = collections_by_id.get(version["collection_id"]) if version else None
            source, copy = questions.get(source_id), questions.get(copy_id)
            if (collection is None or source is None or copy is None or source_id == copy_id
                    or source.get("learner_id") != collection["learner_id"]
                    or copy.get("learner_id") != collection["learner_id"]
                    or source_id not in version_questions.get(vid, set())
                    or learning_counts.get((collection["learner_id"], copy_id)) != 1
                    or (vid, source_id) in practice_pairs or copy_id in copies):
                raise BackupError("题集练习来源无效，整批不导入。")
            practice_pairs.add((vid, source_id))
            copies.add(copy_id)
    except (collection_service.CollectionError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise BackupError("题集数据格式或内容无效，整批不导入。") from exc


def _existing_counts(session: Session) -> dict[str, int]:
    return {
        key: int(session.scalar(select(func.count()).select_from(_TABLE_BY_KEY[key])) or 0)
        for key in _CONTENT_KEYS
    }


def _refuse_non_empty(session: Session) -> None:
    """目标库有业务数据就整批拒绝 —— 不做合并。"""
    counts = _existing_counts(session)
    if not any(counts.values()):
        return
    if counts["question"]:
        head = f"当前库里有 {counts['question']} 道题"
    else:
        merged: dict[str, int] = {}
        for key, count in counts.items():
            if count:
                merged[TABLES[key]] = merged.get(TABLES[key], 0) + count
        detail = "、".join(f"{name} {count} 条" for name, count in merged.items())
        head = f"当前库不是空库（{detail}）"
    raise BackupError(f"{head}，导入会覆盖，请先备份或换一个空库。")


def _replace_placeholder_learner(session: Session, *, replace_empty_ok: bool) -> None:
    """空库上 web 启动会建一条 ``default`` learner；备份里那条要原样恢复，就得让位。

    走到这里时业务表已确认为空（``error_item`` / ``exam`` 也都是 0），
    所以现有 learner 一定无人引用，删掉是安全的占位清理。
    """
    existing = list(session.scalars(select(Learner.id)))
    if not existing:
        return
    if not replace_empty_ok:
        raise BackupError(
            f"目标库里已经有学习者（id={existing}），当前调用不允许替换，请换一个空库。"
        )
    session.execute(delete(Learner))


def _write_table(
    session: Session, key: str, table: Table, self_fk: str | None, rows: list[dict[str, Any]]
) -> None:
    """写一张表。逐行写是为了报错能点到「第几条」，也允许行与行字段不完全一致。"""
    deferred: list[Any] = []
    for index, row in enumerate(rows, start=1):
        payload = dict(row)
        parent_id = payload.pop(self_fk, None) if self_fk else None
        if self_fk:
            deferred.append(parent_id)
        try:
            session.execute(table.insert().values(**payload))
        except IntegrityError as exc:
            raise BackupError(
                f"{TABLES[key]} 第 {index} 条写不进去（主键/唯一键冲突，或它引用的记录不在备份里）："
                f"{exc.orig}。整批已回滚。"
            ) from exc

    if not self_fk:
        return
    for index, (row, parent_id) in enumerate(zip(rows, deferred), start=1):
        if parent_id is None:
            continue
        try:
            session.execute(
                table.update()
                .where(table.c.id == row["id"])
                .values({self_fk: parent_id})
            )
        except IntegrityError as exc:
            raise BackupError(
                f"{TABLES[key]} 第 {index} 条的父级 #{parent_id} 不在备份里，整批已回滚：{exc.orig}"
            ) from exc


def _apply_learner_ownership(document: Any) -> Any:
    """老备份（schema_version < 9）的 question / scan 行没有 learner_id。

    必须在 ``_validate`` **之前**补：question.learner_id 是必填字段，校验阶段就会以
    「缺必填字段」整批拒绝。口径与迁移 v9 一致：归给备份里 id 最小的 learner。
    已经是 v9 备份（行里带 learner_id）时原样返回，不改一个字节。
    """
    if not isinstance(document, dict):
        return document
    learner_ids = [
        row.get("id")
        for row in document.get("learner") or []
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    ]
    if not learner_ids:
        return document
    owner = min(learner_ids)
    patched = dict(document)
    for key in ("question", "scan"):
        rows = document.get(key)
        if not isinstance(rows, list):
            continue
        patched[key] = [
            {**row, "learner_id": owner}
            if isinstance(row, dict) and row.get("learner_id") is None
            else row
            for row in rows
        ]
    return patched


def load_backup(
    session: Session, document: dict[str, Any], *, replace_empty_ok: bool = True
) -> ImportReport:
    """把 *document* 写进 *session* 指向的空库；返回各表条数。

    校验在写库之前全部做完；写库只在一个事务里，任何失败整体回滚。
    *replace_empty_ok* 为 False 时，连 web 建的那条占位 learner 也不许替换。
    """
    version, exported_at, rows_by_table = _validate(_apply_learner_ownership(document))
    counts: dict[str, int] = {}
    try:
        _refuse_non_empty(session)
        _replace_placeholder_learner(session, replace_empty_ok=replace_empty_ok)
        for key, table, self_fk in _TABLES:
            rows = rows_by_table[key]
            if rows:
                _write_table(session, key, table, self_fk, rows)
            counts[key] = len(rows)
        session.commit()
    except BackupError:
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise BackupError(f"写入时数据库报错，整批已回滚：{exc}") from exc
    except Exception:
        session.rollback()
        raise
    return ImportReport(schema_version=version, exported_at=exported_at, counts=counts)


def read_backup(path: Path) -> dict[str, Any]:
    """读一份备份文件（UTF-8）；不是 JSON 或不是 UTF-8 都给一句人话。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise BackupError(f"备份文件不是 UTF-8 编码：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise BackupError(f"备份文件不是有效的 JSON：{exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """``python -m cairn.import_service <backup.json>`` —— 在 NAS 上恢复用。"""
    parser = argparse.ArgumentParser(
        prog="python -m cairn.import_service",
        description="把 /export 导出的 JSON 备份恢复到一个空库（目标库非空会拒绝）。",
    )
    parser.add_argument("backup", help="备份 JSON 的路径")
    parser.add_argument(
        "--db-url",
        default=os.environ.get("CAIRN_DB_URL", "sqlite:///./cairn.db"),
        help="目标库（缺省 CAIRN_DB_URL，再缺省 sqlite:///./cairn.db）",
    )
    args = parser.parse_args(argv)

    path = Path(args.backup)
    if not path.is_file():
        print(f"导入失败：找不到备份文件 {path}", file=sys.stderr)
        return 1
    try:
        document = read_backup(path)
    except BackupError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1

    engine = make_engine(args.db_url)
    try:
        init_db(engine)
        with make_session_factory(engine)() as session:
            try:
                report = load_backup(session, document)
            except BackupError as exc:
                print(f"导入失败：{exc}", file=sys.stderr)
                return 1
    finally:
        engine.dispose()

    print(f"导入完成（schema_version={report.schema_version}）：{report.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
