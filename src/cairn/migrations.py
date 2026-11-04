"""Minimal forward-only schema migrations.

Why not Alembic: 0.1/0.2 是单家庭、单文件 SQLite 应用，需要的只是「老库启动时
补上新列」这一件事，不值得引一个依赖（0.2 卡1 硬要求：不引新依赖）。

How it works:

- `schema_version` 单表单行（id=1）记录库当前版本，另记 applied_at 便于排查。
- `MIGRATIONS` 是有序列表；只有当迁移版本 > 记录的版本时才执行，整条升级链路
  在一个事务里，失败即整体回滚（不会停在「改了一半」）。
- 全新的库由 `create_all` 直接按当前模型建表，随即被 `stamp_version` 打上最新
  版本；只有已存在的库才走迁移链。
- 加列的迁移用 inspector 判重，重复执行不报错（幂等），这样「库里有新列但没
  版本记录」的手工库也能被安全收编。

新增改表时：改 `models/`，同时在 `MIGRATIONS` 末尾追加一条，版本号连续 +1。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from cairn.concept_service import normalize_name, split_names
from cairn.models.book import Book, Unit, Word
from cairn.models.concept import Concept, ConceptAlias, question_concept
from cairn.models.cut_feedback import CutFeedback
from cairn.models.exam import Exam
from cairn.models.scan import Scan, ScanQuestion
from cairn.models.strategy import DEFAULT_STRATEGY_ROW_ID, Strategy
from cairn.models.tutor import TutorMessage, TutorSession
from cairn.parse import detect_lang

logger = logging.getLogger(__name__)

# Old batch rows are the only rows whose subject is the English code: later
# batches (and every manual entry) write the Chinese name, so the code is a
# reliable marker for "content was the word, answer was the hint".
_OLD_BATCH_SUBJECTS = {"english": "英语", "chinese": "语文"}
_CJK = re.compile(r"[\u4e00-\u9fff]")
# Tone-marked latin letters (ā/á/ǎ/à …) mark a pinyin reading; plain English
# words ("apple") carry none, so this separates the two without guessing.
_PINYIN_TONE = re.compile(r"[\u00c0-\u01ff]")

_VERSION_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER NOT NULL PRIMARY KEY,
    version INTEGER NOT NULL,
    applied_at DATETIME
)
"""


@dataclass(frozen=True)
class Migration:
    """One forward-only step. *upgrade* runs inside the caller's transaction.

    A step may return a report dict (counts, unparsed rows); it is collected in
    ``LAST_RUN_REPORT`` so the operator can see what a backfill did or skipped
    without reading logs. Returning ``None`` means "nothing to report".
    """

    version: int
    description: str
    upgrade: Callable[[Connection], dict[str, object] | None]


# Filled by the most recent ``apply_migrations`` call; empty when the DB was
# already current or brand new (no chain ran).
LAST_RUN_REPORT: dict[str, object] = {}


def _add_column_if_missing(
    conn: Connection, table: str, column: str, ddl: str
) -> None:
    """SQLite has no `ADD COLUMN IF NOT EXISTS`; ask the inspector instead.

    目标表不存在时（手搭库、半成品库只含部分表）跳过该步并记 INFO，不抛异常：
    迁移链必须整体走完才能推进版本号。表存在时行为不变。
    """
    if table not in _table_names(conn):
        logger.info("migration: skip ALTER — table %s not present", table)
        return
    if column in {c["name"] for c in inspect(conn).get_columns(table)}:
        return
    conn.exec_driver_sql(ddl)


def _add_question_audio_text(conn: Connection) -> None:
    _add_column_if_missing(
        conn, "question", "audio_text", "ALTER TABLE question ADD COLUMN audio_text TEXT"
    )


def _add_learner_daily_target(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "learner",
        "daily_target",
        "ALTER TABLE learner ADD COLUMN daily_target INTEGER NOT NULL DEFAULT 5",
    )


def _add_workflow_publication(conn: Connection) -> None:
    """v23: authoring drafts, immutable publication records, and nullable scan pin."""
    from cairn.models.workflow import Workflow, WorkflowVersion

    Workflow.__table__.create(bind=conn, checkfirst=True)
    WorkflowVersion.__table__.create(bind=conn, checkfirst=True)
    _add_column_if_missing(
        conn,
        "scan",
        "workflow_version_id",
        "ALTER TABLE scan ADD COLUMN workflow_version_id VARCHAR(96) REFERENCES workflow_version(version_id)",
    )
    if "scan" in _table_names(conn):
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_scan_workflow_version_id "
            "ON scan (workflow_version_id)"
        )


def _add_workflow_runs(conn: Connection) -> None:
    """v24: durable scan workflow progress and per-step state."""
    from cairn.models.workflow_run import WorkflowRun, WorkflowRunStep

    WorkflowRun.__table__.create(bind=conn, checkfirst=True)
    WorkflowRunStep.__table__.create(bind=conn, checkfirst=True)


def _add_item_workflow_strategy(conn: Connection) -> None:
    """v25: optional per-item strategy pinned by a published workflow."""
    _add_column_if_missing(
        conn, "error_item", "workflow_strategy_id",
        "ALTER TABLE error_item ADD COLUMN workflow_strategy_id VARCHAR(32)",
    )


def _add_run_image_snapshot(conn: Connection) -> None:
    """v26: persist the image selected by a workflow run."""
    _add_column_if_missing(conn, "workflow_run", "image_path",
                           "ALTER TABLE workflow_run ADD COLUMN image_path VARCHAR(255)")
    _add_column_if_missing(conn, "workflow_run", "image_sha256",
                           "ALTER TABLE workflow_run ADD COLUMN image_sha256 VARCHAR(64)")


def _add_workflow_review_items(conn: Connection) -> None:
    """v27: durable per-question decisions at a scan review node."""
    from cairn.models.workflow_run import WorkflowReviewItem

    WorkflowReviewItem.__table__.create(bind=conn, checkfirst=True)


def _add_item_workflow_tutor(conn: Connection) -> None:
    """v28: opt in to the existing guided tutor for workflow items."""
    _add_column_if_missing(conn, "error_item", "workflow_tutor_enabled",
                           "ALTER TABLE error_item ADD COLUMN workflow_tutor_enabled INTEGER NOT NULL DEFAULT 0")


def _add_item_workflow_rejected(conn: Connection) -> None:
    """v29: preserve explicit workflow exclusions in learning-data exports."""
    _add_column_if_missing(conn, "error_item", "workflow_rejected",
                           "ALTER TABLE error_item ADD COLUMN workflow_rejected INTEGER NOT NULL DEFAULT 0")
    if ({"error_item", "workflow_review_item"} <= _table_names(conn)
            and "question_id" in _column_names(conn, "error_item")
            and {"question_id", "status"} <= _column_names(conn, "workflow_review_item")):
        conn.exec_driver_sql(
            "UPDATE error_item SET workflow_rejected = 1 WHERE EXISTS "
            "(SELECT 1 FROM workflow_review_item AS w "
            "WHERE w.question_id = error_item.question_id AND w.status = 'rejected')"
        )


def _add_workflow_recap(conn: Connection) -> None:
    """v30: opt-in text recall and durable learner attempts."""
    from cairn.models.recap import RecapAttempt

    _add_column_if_missing(conn, "error_item", "workflow_recap_enabled",
                           "ALTER TABLE error_item ADD COLUMN workflow_recap_enabled INTEGER NOT NULL DEFAULT 0")
    RecapAttempt.__table__.create(bind=conn, checkfirst=True)


def _add_learning_collections(conn: Connection) -> None:
    """v31: collections, ordered members, and immutable version snapshots."""
    from cairn.models.collection import Collection, CollectionItem, CollectionVersion, CollectionVersionQuestion

    for model in (Collection, CollectionItem, CollectionVersion, CollectionVersionQuestion):
        model.__table__.create(bind=conn, checkfirst=True)


def _add_collection_practice_items(conn: Connection) -> None:
    """v32: preserve practice copies and their versioned source."""
    from cairn.models.collection import CollectionPracticeItem

    CollectionPracticeItem.__table__.create(bind=conn, checkfirst=True)


def _add_question_lang(conn: Connection) -> None:
    _add_column_if_missing(
        conn, "question", "lang", "ALTER TABLE question ADD COLUMN lang VARCHAR(8)"
    )


def _backfill_question_lang(conn: Connection) -> None:
    """Fill historical NULL lang using the same detector manual entry uses."""
    rows = conn.exec_driver_sql(
        "SELECT id, content FROM question WHERE lang IS NULL"
    ).fetchall()
    for row_id, content in rows:
        conn.exec_driver_sql(
            "UPDATE question SET lang = ? WHERE id = ?",
            (detect_lang(content or ""), row_id),
        )


def _resync_question_lang_from_answer(conn: Connection) -> None:
    """Normalise old batch rows and re-judge every row's lang from its answer.

    content/answer were aligned so that content is the hint and answer is the
    word the learner writes. Two classes of historical rows need different care:

    - **old batch rows** — the only rows whose ``subject`` is the English code
      ``english`` / ``chinese``. Under the old parser their ``content`` was the
      word to write and their ``answer`` was the hint (pinyin / Chinese meaning),
      so swap the two, rewrite the subject to its Chinese name, and judge lang
      from the swapped answer (now the word). Judging from the unswapped answer
      would read a pinyin/Chinese hint and flip the language.
    - **every other row** — manual entry and already-aligned batches. Their
      content/answer semantics were always right, so they are left untouched
      (not even reordered); lang is judged from ``answer``. A row whose answer
      is pinyin (tone marks) beside a Chinese content is still Chinese, so it is
      forced to ``zh`` instead of being mis-read as ``en`` because pinyin is latin.
    """
    rows = conn.exec_driver_sql(
        "SELECT id, subject, content, answer, lang FROM question"
    ).fetchall()
    for row_id, subject, content, answer, lang in rows:
        new_subject, new_content, new_answer = subject, content, answer
        if subject in _OLD_BATCH_SUBJECTS:
            new_content, new_answer = answer, content
            new_subject = _OLD_BATCH_SUBJECTS[subject]
            new_lang = detect_lang(new_answer or "")
        elif answer and _PINYIN_TONE.search(answer) and _CJK.search(content or ""):
            new_lang = "zh"
        else:
            new_lang = detect_lang(answer or "")
        if (new_subject, new_content, new_answer, new_lang) != (
            subject,
            content,
            answer,
            lang,
        ):
            conn.exec_driver_sql(
                "UPDATE question SET subject = ?, content = ?, answer = ?, lang = ? "
                "WHERE id = ?",
                (new_subject, new_content, new_answer, new_lang, row_id),
            )


def _table_names(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


def _column_names(conn: Connection, table: str) -> set[str]:
    if table not in _table_names(conn):
        return set()
    return {column["name"] for column in inspect(conn).get_columns(table)}


def _create_concept_tables(conn: Connection) -> None:
    """Create the three concept tables exactly as the models declare them.

    ``create_all`` normally got there first (it runs before the migration
    chain), but ``checkfirst`` keeps this step safe when someone calls
    ``apply_migrations`` directly on an untouched old file.
    """
    for table in (Concept.__table__, ConceptAlias.__table__, question_concept):
        table.create(bind=conn, checkfirst=True)


def _add_retention_columns(conn: Connection) -> None:
    """learner 加两个分科目标保留率；DEFAULT 让存量行拿到同一组值。"""
    _add_column_if_missing(
        conn,
        "learner",
        "desired_retention_math",
        "ALTER TABLE learner ADD COLUMN desired_retention_math FLOAT NOT NULL DEFAULT 0.85",
    )
    _add_column_if_missing(
        conn,
        "learner",
        "desired_retention_other",
        "ALTER TABLE learner ADD COLUMN desired_retention_other FLOAT NOT NULL DEFAULT 0.9",
    )


def _find_concept(conn: Connection, subject: str, name: str) -> int | None:
    """Alias first (one spoken form → one concept), then canonical name."""
    row = conn.exec_driver_sql(
        "SELECT c.id FROM concept_aliases a JOIN concepts c ON c.id = a.concept_id "
        "WHERE a.alias = ?",
        (name,),
    ).fetchone()
    if row is not None:
        return int(row[0])
    row = conn.exec_driver_sql(
        "SELECT id FROM concepts WHERE subject = ? AND name = ?", (subject, name)
    ).fetchone()
    return None if row is None else int(row[0])


def _ensure_concept(
    conn: Connection, subject: str, name: str, report: dict[str, object]
) -> int:
    """Get-or-create one concept; returns its id (idempotent by unique key)."""
    existing = _find_concept(conn, subject, name)
    if existing is not None:
        return existing
    cursor = conn.exec_driver_sql(
        "INSERT INTO concepts (name, subject, parent_id, status) "
        "VALUES (?, ?, NULL, 'active')",
        (name, subject),
    )
    report["concepts_created"] = int(report["concepts_created"]) + 1
    return int(cursor.lastrowid)


def _link_question(conn: Connection, question_id: int, concept_id: int) -> int:
    """Insert one question↔concept link; returns 1 when it was new."""
    cursor = conn.exec_driver_sql(
        "INSERT OR IGNORE INTO question_concept (question_id, concept_id) VALUES (?, ?)",
        (question_id, concept_id),
    )
    return 1 if cursor.rowcount else 0


def _empty_concept_report() -> dict[str, object]:
    return {
        "concepts_created": 0,
        "links_created": 0,
        "knowledge_point_rows": 0,
        "question_knowledge_point_rows": 0,
        "migrated": [],
        "skipped": [],
    }


def _backfill_from_knowledge_point(
    conn: Connection, report: dict[str, object]
) -> dict[int, int]:
    """把旧 ``knowledge_point`` 表搬进 ``concepts``，旧表一行不动。

    Rows with a blank name or subject cannot become a concept: they are left
    where they are and listed in the report (count + original text). Parent
    links need a second pass because SQLite does not guarantee parent-before-
    child ordering inside the same table.
    """
    tables = _table_names(conn)
    if "knowledge_point" not in tables:
        return {}
    rows = conn.exec_driver_sql(
        "SELECT id, name, subject, parent_id FROM knowledge_point ORDER BY id"
    ).fetchall()
    old_ids: dict[int, int] = {}
    parents: list[tuple[int, int | None]] = []
    for old_id, name, subject, parent_id in rows:
        report["knowledge_point_rows"] = int(report["knowledge_point_rows"]) + 1
        subject_key, name_key = normalize_name(subject or ""), normalize_name(name or "")
        if not subject_key or not name_key:
            report["skipped"].append(
                f"knowledge_point#{old_id}: name={name!r} subject={subject!r}"
            )
            continue
        concept_id = _ensure_concept(conn, subject_key, name_key, report)
        old_ids[int(old_id)] = concept_id
        parents.append((concept_id, parent_id))
        report["migrated"].append(
            f"knowledge_point#{old_id} {name!r} -> concept#{concept_id}"
        )

    for concept_id, old_parent in parents:
        if old_parent is None:
            continue
        parent_concept = old_ids.get(int(old_parent))
        if parent_concept is None or parent_concept == concept_id:
            report["skipped"].append(
                f"concept#{concept_id}: parent knowledge_point#{old_parent} 未迁移"
            )
            continue
        conn.exec_driver_sql(
            "UPDATE concepts SET parent_id = ? WHERE id = ? AND parent_id IS NULL",
            (parent_concept, concept_id),
        )

    if "question_knowledge_link" in tables:
        links = conn.exec_driver_sql(
            "SELECT question_id, knowledge_point_id FROM question_knowledge_link "
            "ORDER BY question_id, knowledge_point_id"
        ).fetchall()
        for question_id, old_id in links:
            concept_id = old_ids.get(int(old_id))
            if concept_id is None:
                report["skipped"].append(
                    f"question_knowledge_link({question_id},{old_id}): 知识点未迁移"
                )
                continue
            report["links_created"] = int(report["links_created"]) + _link_question(
                conn, int(question_id), concept_id
            )
    return old_ids


def _backfill_from_question_field(
    conn: Connection, report: dict[str, object]
) -> None:
    """兼容老库里可能存在的 ``question.knowledge_point`` 文本列。

    本仓库的模型没有这一列（存量知识点在 knowledge_point 两表里），但历史库
    可能有：按顿号/逗号/分号/竖线/斜杠切分，每个名字取或建一个 concept 并挂题。
    该列**不删不改**，解析不了的（空串、无 subject）留在原处并计入报告。
    """
    if "knowledge_point" not in _column_names(conn, "question"):
        return
    rows = conn.exec_driver_sql(
        "SELECT id, subject, knowledge_point FROM question "
        "WHERE knowledge_point IS NOT NULL ORDER BY id"
    ).fetchall()
    for question_id, subject, raw in rows:
        report["question_knowledge_point_rows"] = (
            int(report["question_knowledge_point_rows"]) + 1
        )
        subject_key = normalize_name(subject or "")
        names = split_names(raw or "")
        if not subject_key or not names:
            report["skipped"].append(
                f"question#{question_id}.knowledge_point={raw!r}: 无法解析"
            )
            continue
        for name in names:
            concept_id = _ensure_concept(conn, subject_key, name, report)
            report["links_created"] = int(report["links_created"]) + _link_question(
                conn, int(question_id), concept_id
            )
        report["migrated"].append(
            f"question#{question_id} {raw!r} -> {len(names)} concept(s)"
        )


def _add_concepts_and_retention(conn: Connection) -> dict[str, object]:
    """v5：建三张知识点表 + learner 分科保留率 + 存量回填（全在一个事务里）。"""
    _create_concept_tables(conn)
    _add_retention_columns(conn)
    report = _empty_concept_report()
    _backfill_from_knowledge_point(conn, report)
    _backfill_from_question_field(conn, report)
    report["skipped_count"] = len(report["skipped"])
    return report


def _create_book_tables(conn: Connection) -> None:
    """v6：教材基本库三表（book / unit / word）。纯加表，不动存量行。

    ``create_all`` 通常已经先把它们建好了（它只跳过已存在的表），这里用
    ``checkfirst`` 兜住直接调 ``apply_migrations`` 的场景。回滚 = DROP 这三张表。
    """
    for table in (Book.__table__, Unit.__table__, Word.__table__):
        table.create(bind=conn, checkfirst=True)


def _create_exam_table(conn: Connection) -> None:
    """v7：考试倒计时一张表（exam）。纯加表，无回填、不动存量行。

    0.2 卡10 的需求写「迁移 v6」，但 v6 已给教材三表占用；版本链必须连续，
    所以这里落在 7。回滚 = DROP TABLE exam。
    """
    Exam.__table__.create(bind=conn, checkfirst=True)


def _create_scan_table(conn: Connection) -> None:
    """v8：拍照记录一张表（scan）。纯加表，无回填、不动存量行。

    0.2 卡11 的需求同样写「迁移 v7」，但 v7 已被卡10 的 exam 占用；版本链连续，
    落在 8。回滚 = DROP TABLE scan（已落盘的图片属数据，不随迁移回滚）。
    """
    Scan.__table__.create(bind=conn, checkfirst=True)


def _ensure_owner_for_backfill(conn: Connection) -> int:
    """Migration owner = 最小 id 的 learner（生产库里就是 1 号）。

    老库里可能有题/照片但一条 learner 都没有（手工库、早期半成品）。没有归属目标
    就没法回填，只能先建一条占位 learner —— 与 web 启动时补 default 的口径一致。
    """
    owner = conn.exec_driver_sql("SELECT MIN(id) FROM learner").scalar_one()
    if owner is not None:
        return int(owner)
    cursor = conn.exec_driver_sql(
        "INSERT INTO learner (name, grade_level, created_at) VALUES (?, ?, ?)",
        ("default", "primary", datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    return int(cursor.lastrowid)


def _add_learner_ownership(conn: Connection) -> dict[str, object]:
    """v9：question / scan 加 learner_id，存量行全部归给最小 id 的 learner。

    SQLite 的 ``ADD COLUMN`` 加不上「NOT NULL 且无默认值」的列（表非空时会直接报错），
    所以老库这一列在 SQL 层是可空的；新库由 ``create_all`` 按模型建成 NOT NULL。
    写入路径（录入 / 抓拍 / 导入）一律必填，可空只留给迁移期间的中间态。

    幂等：列已存在则跳过，只剩 NULL 行需要回填；二次执行两表都没有 NULL 行，
    报告全 0。整条链在调用方的事务里，失败会连版本号一起回滚。
    """
    _add_column_if_missing(
        conn,
        "question",
        "learner_id",
        "ALTER TABLE question ADD COLUMN learner_id INTEGER REFERENCES learner(id)",
    )
    _add_column_if_missing(
        conn,
        "scan",
        "learner_id",
        "ALTER TABLE scan ADD COLUMN learner_id INTEGER REFERENCES learner(id)",
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_question_learner_id ON question (learner_id)"
    )
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_scan_learner_id ON scan (learner_id)")

    pending_questions = int(
        conn.exec_driver_sql("SELECT COUNT(*) FROM question WHERE learner_id IS NULL").scalar_one()
    )
    pending_scans = int(
        conn.exec_driver_sql("SELECT COUNT(*) FROM scan WHERE learner_id IS NULL").scalar_one()
    )
    if not pending_questions and not pending_scans:
        return {"owner_id": None, "questions_owned": 0, "scans_owned": 0}

    owner = _ensure_owner_for_backfill(conn)
    conn.exec_driver_sql(
        "UPDATE question SET learner_id = ? WHERE learner_id IS NULL", (owner,)
    )
    conn.exec_driver_sql("UPDATE scan SET learner_id = ? WHERE learner_id IS NULL", (owner,))
    return {
        "owner_id": owner,
        "questions_owned": pending_questions,
        "scans_owned": pending_scans,
    }


def _add_book_imported_at(conn: Connection) -> None:
    """v10：book.imported_at —— 词表第一次导入的时间（可空；空书/存量行 NULL）。"""
    _add_column_if_missing(
        conn,
        "book",
        "imported_at",
        "ALTER TABLE book ADD COLUMN imported_at DATETIME",
    )


def _add_question_variant_of(conn: Connection) -> None:
    """v11：question.variant_of —— 变式题指向同知识点的原题（可空自引用）。"""
    _add_column_if_missing(
        conn,
        "question",
        "variant_of",
        "ALTER TABLE question ADD COLUMN variant_of INTEGER REFERENCES question(id)",
    )


def ensure_default_strategy_row(conn: Connection) -> None:
    """strategy 目录里种入默认行 fsrs（幂等；v12 迁移与 init_db 新库共用）。

    排期行为不依赖这张表（策略实现在代码层 registry），种行只是给后续
    界面选择留一份目录。created_at 用 UTC ISO 字符串，与 learner 同口径。
    """
    conn.exec_driver_sql(
        "INSERT OR IGNORE INTO strategy (id, name, params_json, created_at) "
        "VALUES (?, ?, NULL, ?)",
        (
            DEFAULT_STRATEGY_ROW_ID,
            "FSRS-6 自适应间隔",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def _add_strategy_tables(conn: Connection) -> None:
    """v12：strategy 定义表 + learner.strategy_id（可空，空 = 默认 fsrs）。

    老库升级后所有既有数据仍按 FSRS 走：排期在代码层 registry（默认 fsrs），
    与这张表的内容无关，所以到期时间逐条不变。防御沿用 _add_column_if_missing：
    learner 表不存在时跳过加列并记 INFO，不抛异常；strategy 表由本步自建
    （checkfirst），缺它的半残库也能一次走完。
    """
    Strategy.__table__.create(bind=conn, checkfirst=True)
    ensure_default_strategy_row(conn)
    _add_column_if_missing(
        conn,
        "learner",
        "strategy_id",
        "ALTER TABLE learner ADD COLUMN strategy_id VARCHAR(32) REFERENCES strategy(id)",
    )


def _create_tutor_session_table(conn: Connection) -> None:
    """v13：讲解会话表（tutor_session）。纯加表，不动存量行。

    防御式：checkfirst 兜住「表已存在但版本号没记上」的半残库；learner /
    question 缺表时 create 会抛错并让整条链回滚（那两种表缺失属于更底层的
    损坏，不该静默跳过装作成功）。回滚 = DROP TABLE tutor_session。
    """
    TutorSession.__table__.create(bind=conn, checkfirst=True)


def _create_tutor_message_table(conn: Connection) -> None:
    """v14：讲解消息表（tutor_message）。纯加表，不动存量行。

    与 v13 分两步：版本链必须连续，讲解卡占 v13/v14 两号。回滚 =
    DROP TABLE tutor_message。
    """
    TutorMessage.__table__.create(bind=conn, checkfirst=True)


def _add_scan_upload_columns(conn: Connection) -> dict[str, object] | None:
    """v15：上传通道——scan 加 source / quality_json / question_id。

    防御式沿用 _add_column_if_missing：列已存在或 scan 表缺失时跳过该列。
    source 用 NOT NULL DEFAULT 'device'，SQLite 的 ADD COLUMN 允许常量默认值，
    存量行立刻读到 'device'，无需逐行回填；下面的 UPDATE 只是兜住理论上
    存在的 NULL（例如半残库手工加过可空列），存量行为不变。
    回滚 = 逐列 SQLite 不支持 DROP（3.35 前），整列回滚 = 恢复备份。
    """
    _add_column_if_missing(
        conn,
        "scan",
        "source",
        "ALTER TABLE scan ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'device'",
    )
    _add_column_if_missing(
        conn, "scan", "quality_json", "ALTER TABLE scan ADD COLUMN quality_json TEXT"
    )
    _add_column_if_missing(
        conn,
        "scan",
        "question_id",
        "ALTER TABLE scan ADD COLUMN question_id INTEGER REFERENCES question(id)",
    )
    if "scan" not in _table_names(conn):
        return None  # 半残库没有 scan 表：加列已跳过，回填同样无事可做
    result = conn.exec_driver_sql(
        "UPDATE scan SET source = 'device' WHERE source IS NULL"
    )
    if result.rowcount:
        return {"backfilled_source": result.rowcount}
    return None


def _create_scan_question_table(conn: Connection) -> None:
    """v16：切题结果表（scan_question）。纯加表，不动存量行。

    一页切成 N 块后每块一行，`(scan_id, no)` 唯一保证重复切题不重复插行。
    checkfirst 兜住「表已存在但版本号没记上」的半残库；scan / question 缺表时
    create 会抛错并让整条链回滚（缺这两张属于更底层的损坏，不静默跳过）。
    回滚 = DROP TABLE scan_question（切题结果可重算，丢了不伤存量数据）。
    """
    ScanQuestion.__table__.create(bind=conn, checkfirst=True)


def _add_scan_question_crop_path(conn: Connection) -> None:
    """v17：切题行加 `crop_path`（题块裁图路径）。纯加列，不动存量行。

    防御式沿用 `_add_column_if_missing`：列已存在或 scan_question 表缺失
    （半残库/被手工摘表的库）时跳过，迁移链照样走完并推进版本号。
    列可空：老行没有裁图，页面按"暂无原图"处理。
    回滚 = 恢复备份（SQLite 3.35 前不支持 DROP COLUMN）。
    """
    _add_column_if_missing(
        conn,
        "scan_question",
        "crop_path",
        "ALTER TABLE scan_question ADD COLUMN crop_path VARCHAR(255)",
    )


def _add_scan_question_inferred_number(conn: Connection) -> None:
    """v18：切题行加 `inferred_number`（题号是推测的）。纯加列，不动存量行。

    切题时卷面题号认不出来会按跳号补一个（`QuestionBlock.inferred_number`），
    这个标记要落库给标错页提示家长，否则「第 21 题」会被当成确定无疑。

    NOT NULL DEFAULT 0：SQLite 的 ADD COLUMN 允许常量默认值，存量行立刻读到 0
    （= 题号是认出来的），无需逐行回填、一条数据也不动。
    防御式沿用 `_add_column_if_missing`：列已存在或 scan_question 表缺失时跳过，
    迁移链照样走完并推进版本号。
    回滚 = 恢复备份（SQLite 3.35 前不支持 DROP COLUMN）。
    """
    _add_column_if_missing(
        conn,
        "scan_question",
        "inferred_number",
        "ALTER TABLE scan_question ADD COLUMN inferred_number "
        "BOOLEAN NOT NULL DEFAULT 0",
    )


def _add_scan_processed_path(conn: Connection) -> None:
    """v19：scan 加 `processed_path`（自动预处理另存的那张图）。纯加列，不动存量行。

    上传后服务端会试着把图"扫描化"（找纸边 → 透视矫正 → 去阴影），结果另存为
    `<同目录>/<uuid>.processed.jpg`，相对路径记在这里。**原图 `path` 不动**。

    列可空是有意的：老行没处理过；新行找不到纸张边界时也留空——读图链路按
    "空 / 文件不在 = 用原图"回落，不需要额外的状态位。
    防御式沿用 `_add_column_if_missing`：列已存在或 scan 表缺失时跳过，迁移链
    照样走完并推进版本号。
    回滚 = 恢复备份（SQLite 3.35 前不支持 DROP COLUMN）。
    """
    _add_column_if_missing(
        conn,
        "scan",
        "processed_path",
        "ALTER TABLE scan ADD COLUMN processed_path VARCHAR(255)",
    )


def _create_cut_feedback_table(conn: Connection) -> None:
    """v20: cut_feedback — one snapshot per page of manual cut corrections.

    Pure new table, no backfill, existing rows untouched. The unique index
    ``(scan_id, frame_key)`` is what makes a resubmission an overwrite instead
    of an append; the three lookup indexes mirror `docs/CUT-FEEDBACK-SPEC.md`.

    Idempotent by construction: ``checkfirst`` skips the whole table (and its
    indexes) when it already exists, so a second run leaves the schema byte-for-
    byte identical. Rollback = DROP TABLE cut_feedback (the labels are the only
    copy, so back up first).
    """
    CutFeedback.__table__.create(bind=conn, checkfirst=True)


def _add_question_edit_trace(conn: Connection) -> None:
    """v21: question.edited_at / edited_by — the trace a human edit leaves.

    Pure column add, existing rows untouched (both NULL = "never edited by
    hand"). ``edited_at`` is an ISO8601 string with a +08:00 offset, kept as
    TEXT so the page can print it verbatim instead of re-deriving a timezone;
    ``edited_by`` is 'human' today ('model' reserved for the model pass).

    Idempotent through ``_add_column_if_missing``: a second run sees both
    columns and does nothing, so the schema stays byte-for-byte identical.
    回滚 = 恢复备份（SQLite 3.35 前不支持 DROP COLUMN）。
    """
    _add_column_if_missing(
        conn, "question", "edited_at", "ALTER TABLE question ADD COLUMN edited_at TEXT"
    )
    _add_column_if_missing(
        conn, "question", "edited_by", "ALTER TABLE question ADD COLUMN edited_by TEXT"
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "question.audio_text：朗读 cue（只念 cue，不念 answer）", _add_question_audio_text),
    Migration(2, "question.lang：题面语言（en/zh）", _add_question_lang),
    Migration(3, "question.lang：回填历史 NULL（按题面语种，默认 zh）", _backfill_question_lang),
    Migration(
        4,
        "旧批量行 content↔answer 对调 + 语种按 answer 重判（单条行不动）",
        _resync_question_lang_from_answer,
    ),
    Migration(
        5,
        "concepts/concept_aliases/question_concept + learner 分科保留率 + 知识点回填",
        _add_concepts_and_retention,
    ),
    Migration(6, "教材基本库三表（book / unit / word）", _create_book_tables),
    Migration(7, "exam 考试表（考试倒计时）", _create_exam_table),
    Migration(8, "scan 拍照记录表（摄像头/高拍仪采集）", _create_scan_table),
    Migration(
        9,
        "question/scan 加 learner_id：每个孩子一套题库，存量行归最小 id 的 learner",
        _add_learner_ownership,
    ),
    Migration(10, "book.imported_at：词表导入时间（纯加列，不动存量行）", _add_book_imported_at),
    Migration(
        11,
        "question.variant_of：变式题指针（0.3 卡2 出库双条件，纯加列，不动存量行）",
        _add_question_variant_of,
    ),
    Migration(
        12,
        "strategy 策略目录表 + learner.strategy_id（可空，空 = 默认 fsrs；纯加表加列，不动存量行）",
        _add_strategy_tables,
    ),
    Migration(13, "tutor_session 讲解会话表（四段式讲解，纯加表，不动存量行）", _create_tutor_session_table),
    Migration(14, "tutor_message 讲解消息表（纯加表，不动存量行）", _create_tutor_message_table),
    Migration(
        15,
        "scan 加 source/quality_json/question_id（上传通道，存量行回填 device）",
        _add_scan_upload_columns,
    ),
    Migration(
        16,
        "scan_question 切题结果表（题号/题框/题干/标错，纯加表，不动存量行）",
        _create_scan_question_table,
    ),
    Migration(
        17,
        "scan_question.crop_path：题块裁图路径（纯加列，不动存量行）",
        _add_scan_question_crop_path,
    ),
    Migration(
        18,
        "scan_question.inferred_number：题号是推测的（纯加列，存量行默认 0）",
        _add_scan_question_inferred_number,
    ),
    Migration(
        19,
        "scan.processed_path：自动预处理的另存图（纯加列，存量行留空 = 用原图）",
        _add_scan_processed_path,
    ),
    Migration(
        20,
        "cut_feedback 圈选修正数据集（一页一次快照，纯加表，不动存量行）",
        _create_cut_feedback_table,
    ),
    Migration(
        21,
        "question.edited_at / edited_by：人工编辑痕迹（纯加列，存量行留空）",
        _add_question_edit_trace,
    ),
    Migration(
        22,
        "learner.daily_target：每天练几题（纯加列，存量行默认 5）",
        _add_learner_daily_target,
    ),
    Migration(
        23,
        "workflow 草稿/不可变发布版本 + scan.workflow_version_id（存量 scan 留空）",
        _add_workflow_publication,
    ),
    Migration(24, "workflow run and step state", _add_workflow_runs),
    Migration(25, "optional workflow strategy per learning item", _add_item_workflow_strategy),
    Migration(26, "workflow run selected image snapshot", _add_run_image_snapshot),
    Migration(27, "workflow review decisions", _add_workflow_review_items),
    Migration(28, "optional workflow guided tutor", _add_item_workflow_tutor),
    Migration(29, "preserve workflow exclusions", _add_item_workflow_rejected),
    Migration(30, "workflow text recall attempts", _add_workflow_recap),
    Migration(31, "learning collection assets", _add_learning_collections),
    Migration(32, "collection practice provenance", _add_collection_practice_items),
)

SCHEMA_VERSION: int = MIGRATIONS[-1].version


def _read_version(conn: Connection) -> int | None:
    row = conn.exec_driver_sql(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()
    return None if row is None else int(row[0])


def _write_version(conn: Connection, version: int) -> None:
    conn.exec_driver_sql(
        "INSERT INTO schema_version (id, version, applied_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
        "applied_at = excluded.applied_at",
        (version, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def apply_migrations(engine: Engine) -> int:
    """Bring *engine*'s DB up to SCHEMA_VERSION; return the resulting version.

    Whatever the executed steps returned is left in ``LAST_RUN_REPORT`` under
    ``v<version>`` keys, so a backfill that had to skip rows cannot be silent.
    """
    report: dict[str, object] = {}
    with engine.begin() as conn:
        conn.exec_driver_sql(_VERSION_TABLE_DDL)
        current = _read_version(conn)
        if current is None:
            current = 0
            _write_version(conn, current)
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            details = migration.upgrade(conn)
            current = migration.version
            _write_version(conn, current)
            if details:
                report[f"v{current}"] = details
    LAST_RUN_REPORT.clear()
    LAST_RUN_REPORT.update(report)
    return current


def stamp_version(engine: Engine, version: int = SCHEMA_VERSION) -> None:
    """Record *version* without running any migration (fresh DB already there)."""
    with engine.begin() as conn:
        conn.exec_driver_sql(_VERSION_TABLE_DDL)
        _write_version(conn, version)
