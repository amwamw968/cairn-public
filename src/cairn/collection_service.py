"""Owned collections and stable text-only published versions.

Callers own transactions, including rollback on any exception. Portable
packages contain no learning history, scan references, images or database IDs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from cairn.models import Collection, CollectionItem, CollectionPracticeItem, CollectionVersion, CollectionVersionQuestion, ErrorItem, Learner, Question
from cairn.models.learner import utcnow
from cairn.workflow.review import ready_for_practice

LICENSES = frozenset({"private", "CC-BY-4.0", "CC-BY-NC-4.0"})
MAX_ITEMS = 5000


class CollectionError(ValueError):
    """Invalid, stale, or inaccessible collection action."""


def _string(value: object, name: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise CollectionError(f"{name} 格式无效")
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise CollectionError(f"{name} 长度无效")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CollectionError(f"{name} 包含无效字符") from exc
    return value


def _tags(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 30:
        raise CollectionError("标签格式无效")
    result = [_string(tag, "标签", 64) for tag in value]
    if len(set(result)) != len(result):
        raise CollectionError("标签不能重复")
    return result


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _owned(session: Session, learner_id: int, collection_id: int) -> Collection:
    collection = session.get(Collection, collection_id)
    if collection is None or collection.learner_id != learner_id:
        raise CollectionError("题集不存在")
    return collection


def create(session: Session, learner_id: int, *, name: object, subject: object, tags: object,
           license: object, author: object, source: object) -> Collection:
    if session.get(Learner, learner_id) is None:
        raise CollectionError("学习者不存在")
    if not isinstance(license, str) or license not in LICENSES:
        raise CollectionError("请选择有效许可")
    row = Collection(learner_id=learner_id, name=_string(name, "名称", 120),
                     subject=_string(subject, "学科", 32), tags_json=canonical(_tags(tags)),
                     license=license, author=_string(author, "署名", 120),
                     source=_string(source, "来源", 300), revision=0)
    session.add(row)
    session.flush()
    return row


def _claim(session: Session, collection: Collection, expected_revision: int) -> None:
    if type(expected_revision) is not int or expected_revision < 0:
        raise CollectionError("版本号无效")
    changed = session.execute(update(Collection).where(
        Collection.id == collection.id, Collection.learner_id == collection.learner_id,
        Collection.revision == expected_revision,
    ).values(revision=expected_revision + 1, updated_at=utcnow()).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise CollectionError("题集已在其他位置修改，请刷新")
    session.refresh(collection)


def update_metadata(session: Session, learner_id: int, collection_id: int, expected_revision: int,
                    *, name: object, tags: object, license: object, author: object, source: object) -> Collection:
    """Change draft metadata; older published versions stay unchanged."""
    collection = _owned(session, learner_id, collection_id)
    if not isinstance(license, str) or license not in LICENSES:
        raise CollectionError("请选择有效许可")
    values = (_string(name, "名称", 120), canonical(_tags(tags)), license,
              _string(author, "署名", 120), _string(source, "来源", 300))
    _claim(session, collection, expected_revision)
    collection.name, collection.tags_json, collection.license, collection.author, collection.source = values
    session.flush()
    return collection


def remove(session: Session, learner_id: int, collection_id: int, expected_revision: int) -> None:
    """Remove one collection and its versions without deleting the questions."""
    collection = _owned(session, learner_id, collection_id)
    _claim(session, collection, expected_revision)
    _ensure_not_used_by_live_workflow(session, collection_id)
    version_ids = select(CollectionVersion.id).where(CollectionVersion.collection_id == collection_id)
    if session.scalar(select(CollectionPracticeItem.id).where(
            CollectionPracticeItem.version_id.in_(version_ids)).limit(1)) is not None:
        raise CollectionError("题集版本已有练习副本，请先处理关联的练习题")
    session.execute(delete(CollectionVersionQuestion).where(CollectionVersionQuestion.version_id.in_(version_ids)))
    session.execute(delete(CollectionVersion).where(CollectionVersion.collection_id == collection_id))
    session.execute(delete(CollectionItem).where(CollectionItem.collection_id == collection_id))
    session.delete(collection)
    session.flush()


def _ensure_not_used_by_live_workflow(session: Session, collection_id: int) -> None:
    """Protect active workflows and scans whose pinned run has not completed."""
    from cairn.models.scan import Scan
    from cairn.models.workflow import Workflow, WorkflowVersion
    from cairn.models.workflow_run import WorkflowRun

    version_ids = set(session.scalars(select(Workflow.active_version_id).where(Workflow.active_version_id.is_not(None))))
    version_ids.update(session.scalars(select(Scan.workflow_version_id).outerjoin(
        WorkflowRun, WorkflowRun.scan_id == Scan.id).where(
        Scan.workflow_version_id.is_not(None), or_(WorkflowRun.id.is_(None), WorkflowRun.status != "completed"))))
    for version in session.scalars(select(WorkflowVersion).where(WorkflowVersion.version_id.in_(version_ids))):
        try:
            steps = json.loads(version.runtime_json)["steps"]
            if not isinstance(steps, list):
                raise ValueError
            for step in steps:
                if not isinstance(step, dict) or not isinstance(step.get("params"), dict):
                    raise ValueError
                if step.get("node_id") == "collection_append" and step["params"].get("collection_id") == collection_id:
                    raise CollectionError("活动流程或未完成的卷子正在使用题集，请先处理")
                if step.get("node_id") == "collection_source":
                    source_version = session.get(CollectionVersion, step["params"].get("version_id"))
                    if source_version is None:
                        raise CollectionError("无法检查流程依赖，暂不能删除题集")
                    if source_version.collection_id == collection_id:
                        raise CollectionError("活动流程正在使用题集，请先处理")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, CollectionError):
                raise
            raise CollectionError("无法检查流程依赖，暂不能删除题集") from exc


def ensure_question_not_in_active_source(session: Session, question_id: int) -> None:
    """Do not revoke the snapshot of an active collection workflow."""
    from cairn.models.workflow import Workflow, WorkflowVersion
    from cairn.workflow.service import COLLECTION_WORKFLOW_ID

    workflow = session.get(Workflow, COLLECTION_WORKFLOW_ID)
    if workflow is None or workflow.active_version_id is None:
        return
    version = session.get(WorkflowVersion, workflow.active_version_id)
    try:
        steps = json.loads(version.runtime_json)["steps"] if version else None
        if not isinstance(steps, list) or not steps or steps[0].get("node_id") != "collection_source":
            raise ValueError
        version_id = steps[0]["params"]["version_id"]
        if type(version_id) is not int or session.get(CollectionVersion, version_id) is None:
            raise ValueError
        if session.get(CollectionVersionQuestion, (version_id, question_id)) is not None:
            raise CollectionError("活动题集流程正在使用这道题，请先切换流程")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, CollectionError):
            raise
        raise CollectionError("无法检查流程依赖，暂不能删除题目") from exc


def replace_items(session: Session, learner_id: int, collection_id: int, expected_revision: int,
                  items: object) -> Collection:
    """Replace ordered members atomically, without changing learning records."""
    collection = _owned(session, learner_id, collection_id)
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise CollectionError("题集条目数量无效")
    parsed = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"question_id", "tags", "difficulty", "source"}:
            raise CollectionError("题集条目格式无效")
        qid = item["question_id"]
        if type(qid) is not int or qid <= 0:
            raise CollectionError("题目编号无效")
        difficulty = item["difficulty"]
        if difficulty is not None:
            difficulty = _string(difficulty, "难度", 64, required=False) or None
        parsed.append((qid, canonical(_tags(item["tags"])), difficulty,
                       _string(item["source"], "题目来源", 300)))
    ids = [row[0] for row in parsed]
    if len(set(ids)) != len(ids):
        raise CollectionError("题集不能重复收录同一道题")
    _claim(session, collection, expected_revision)
    if ids:
        questions = list(session.scalars(select(Question).where(
            Question.id.in_(ids), Question.learner_id == learner_id,
            Question.subject == collection.subject, ready_for_practice(Question.id),
        )))
        if len(questions) != len(ids):
            raise CollectionError("题目不存在、尚未核对或学科不一致")
        doubtful = session.scalar(select(ErrorItem.id).where(
            ErrorItem.learner_id == learner_id, ErrorItem.question_id.in_(ids),
            ErrorItem.last_reviewed_at.is_(None), ErrorItem.confidence < 1.0,
        ).limit(1))
        if doubtful is not None:
            raise CollectionError("题目尚待人工确认")
    session.execute(delete(CollectionItem).where(CollectionItem.collection_id == collection.id))
    session.add_all([CollectionItem(collection_id=collection.id, question_id=qid, position=index,
                                    tags_json=tags, difficulty=difficulty, source=source)
                     for index, (qid, tags, difficulty, source) in enumerate(parsed)])
    session.flush()
    return collection


def publish(session: Session, learner_id: int, collection_id: int, expected_revision: int) -> CollectionVersion:
    """Freeze the current text after obtaining the SQLite write lock."""
    collection = _owned(session, learner_id, collection_id)
    _claim(session, collection, expected_revision)
    rows = list(session.execute(select(CollectionItem, Question).join(
        Question, Question.id == CollectionItem.question_id,
    ).where(CollectionItem.collection_id == collection_id).order_by(CollectionItem.position, CollectionItem.id)))
    if not rows:
        raise CollectionError("题集没有题目")
    ids = [question.id for _, question in rows]
    eligible = set(session.scalars(select(Question.id).where(
        Question.id.in_(ids), Question.learner_id == learner_id,
        Question.subject == collection.subject, ready_for_practice(Question.id),
    )))
    if eligible != set(ids):
        raise CollectionError("题集中有不能使用的题目，请检查")
    doubtful = session.scalar(select(ErrorItem.id).where(
        ErrorItem.learner_id == learner_id, ErrorItem.question_id.in_(ids),
        ErrorItem.last_reviewed_at.is_(None), ErrorItem.confidence < 1.0,
    ).limit(1))
    if doubtful is not None:
        raise CollectionError("题集中有尚待人工确认的题目")
    number = (session.scalar(select(func.max(CollectionVersion.number)).where(
        CollectionVersion.collection_id == collection_id)) or 0) + 1
    now = utcnow()
    generated = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    snapshot = {
        "manifest": {"format_version": 1, "name": collection.name, "subject": collection.subject,
                     "tags": json.loads(collection.tags_json), "license": collection.license,
                     "author": collection.author, "source": collection.source, "version": number,
                     "item_count": len(rows), "generated_at": generated.isoformat()},
        "items": [{"subject": question.subject, "grade_level": question.grade_level,
                   "question_type": question.question_type, "content": question.content,
                   "answer": question.answer, "analysis": question.analysis,
                   "tags": json.loads(item.tags_json), "difficulty": item.difficulty, "source": item.source}
                  for item, question in rows],
    }
    # Ensure every newly published snapshot can be exported and re-imported.
    from cairn.collection_package import _serialize, validate
    snapshot = validate(snapshot["manifest"], snapshot["items"])
    _serialize(snapshot)
    snapshot["question_ids"] = ids
    payload = canonical(snapshot)
    version = CollectionVersion(collection_id=collection_id, number=number, snapshot_json=payload,
                                content_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                                published_at=now)
    session.add(version)
    session.flush()
    session.add_all([CollectionVersionQuestion(version_id=version.id, question_id=qid) for qid in ids])
    session.flush()
    return version


def add_to_practice(session: Session, learner_id: int, collection_id: int, expected_revision: int) -> tuple[int, int]:
    """Add draft members once; preserve all existing learning and archive state."""
    collection = _owned(session, learner_id, collection_id)
    _claim(session, collection, expected_revision)
    ids = list(session.scalars(select(CollectionItem.question_id).where(
        CollectionItem.collection_id == collection_id).order_by(CollectionItem.position, CollectionItem.id)))
    if not ids:
        raise CollectionError("题集没有题目")
    eligible = set(session.scalars(select(Question.id).where(
        Question.id.in_(ids), Question.learner_id == learner_id,
        Question.subject == collection.subject, ready_for_practice(Question.id),
    )))
    if eligible != set(ids):
        raise CollectionError("题集中有不能使用的题目，请检查")
    existing = set(session.scalars(select(ErrorItem.question_id).where(
        ErrorItem.learner_id == learner_id, ErrorItem.question_id.in_(ids))))
    new_ids = [qid for qid in ids if qid not in existing]
    session.add_all([ErrorItem(learner_id=learner_id, question_id=qid, wrong_count=0, confidence=1.0)
                     for qid in new_ids])
    session.flush()
    return len(new_ids), len(ids) - len(new_ids)


def revoke_question_versions(session: Session, question_id: int) -> None:
    """Erase snapshots containing a deleted question, retaining only tombstones."""
    collection_ids = set(session.scalars(select(CollectionItem.collection_id).where(
        CollectionItem.question_id == question_id)))
    version_ids = list(session.scalars(select(CollectionVersionQuestion.version_id).where(
        CollectionVersionQuestion.question_id == question_id)))
    if version_ids:
        collection_ids.update(session.scalars(select(CollectionVersion.collection_id).where(
            CollectionVersion.id.in_(version_ids))))
        session.execute(update(CollectionVersion).where(CollectionVersion.id.in_(version_ids)).values(
            snapshot_json=None, revoked_at=utcnow()))
        session.execute(delete(CollectionVersionQuestion).where(CollectionVersionQuestion.version_id.in_(version_ids)))
    session.execute(delete(CollectionItem).where(CollectionItem.question_id == question_id))
    if collection_ids:
        session.execute(update(Collection).where(Collection.id.in_(collection_ids)).values(
            revision=Collection.revision + 1, updated_at=utcnow()))
