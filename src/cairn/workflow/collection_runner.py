"""Apply a validated collection workflow to durable practice copies."""

import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cairn import collection_package
from cairn.collection_service import CollectionError
from cairn.models import Collection, CollectionPracticeItem, CollectionVersion, ErrorItem, Question, Workflow, WorkflowVersion
from cairn.models.error_item import REVIEW_STATUS_ARCHIVED
from cairn.parse import detect_lang
from cairn.workflow.contracts import validate_collection_runtime
from cairn.workflow.execution import RunError
from cairn.workflow.service import COLLECTION_WORKFLOW_ID


def start_active_collection_workflow(session: Session, learner_id: int) -> tuple[int, int]:
    """Pin the active workflow and apply its collection within one transaction."""
    claimed = session.execute(update(Workflow).where(Workflow.id == COLLECTION_WORKFLOW_ID).values(
        revision=Workflow.revision).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise CollectionError("题集练习流程尚未发布")
    workflow = session.get(Workflow, COLLECTION_WORKFLOW_ID)
    session.refresh(workflow)
    version = session.get(WorkflowVersion, workflow.active_version_id) if workflow.active_version_id else None
    try:
        if version is None or version.workflow_id != COLLECTION_WORKFLOW_ID:
            raise ValueError
        runtime = json.loads(version.runtime_json)
        if runtime.get("version_id") != version.version_id:
            raise ValueError
        validate_collection_runtime(runtime.get("steps"))
    except (AttributeError, ValueError, TypeError, RunError) as exc:
        raise CollectionError("题集练习流程未发布或内容无效") from exc
    return add_version_to_practice(session, learner_id, runtime["steps"])


def add_version_to_practice(session: Session, learner_id: int, steps: list[dict]) -> tuple[int, int]:
    """Create or reuse copies atomically; callers commit or roll back the transaction."""
    validate_collection_runtime(steps)
    version_id = steps[0]["params"]["version_id"]
    collection_id = session.scalar(select(CollectionVersion.collection_id).join(Collection).where(
        CollectionVersion.id == version_id, Collection.learner_id == learner_id))
    if collection_id is None:
        raise CollectionError("题集版本不存在或已撤销")
    # Serialize version validation with deletion and other starts. Re-read the
    # snapshot after obtaining the SQLite write lock.
    claimed = session.execute(update(Collection).where(
        Collection.id == collection_id, Collection.learner_id == learner_id
    ).values(revision=Collection.revision).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise CollectionError("题集版本不存在或已撤销")
    document, source_ids = collection_package.validated_version(session, learner_id, version_id)
    mappings = list(session.scalars(select(CollectionPracticeItem).where(
        CollectionPracticeItem.version_id == version_id)))
    by_source = {entry.source_question_id: entry for entry in mappings}
    if len(by_source) != len(mappings) or not set(by_source).issubset(source_ids):
        raise CollectionError("题集练习来源关联无效")
    copy_ids = [entry.question_id for entry in mappings]
    owned_copies = set(session.scalars(select(Question.id).where(
        Question.id.in_(copy_ids), Question.learner_id == learner_id)))
    if len(owned_copies) != len(copy_ids) or set(source_ids) & owned_copies:
        raise CollectionError("题集练习来源关联无效")
    entries = list(session.scalars(select(ErrorItem).where(
        ErrorItem.question_id.in_(copy_ids), ErrorItem.learner_id == learner_id)))
    by_question = {entry.question_id: entry for entry in entries}
    if len(entries) != len(mappings) or len(by_question) != len(entries):
        raise CollectionError("题集练习记录不完整")
    strategy = next((step["params"]["strategy"] for step in steps if step["node_id"] == "strategy"), None)
    tutor = int(any(step["node_id"] == "guided_tutor" for step in steps))
    recap = int(any(step["node_id"] == "text_recap" for step in steps))
    for entry in entries:
        if (entry.review_status != REVIEW_STATUS_ARCHIVED and entry.last_reviewed_at is not None
                and entry.workflow_strategy_id != strategy):
            raise CollectionError("已复习题目的策略不能直接切换")
    for entry in entries:
        if entry.review_status != REVIEW_STATUS_ARCHIVED:
            entry.workflow_strategy_id = strategy
            entry.workflow_tutor_enabled = tutor
            entry.workflow_recap_enabled = recap
    new_pairs = [(source_id, item) for source_id, item in zip(source_ids, document["items"])
                 if source_id not in by_source]
    copies = [Question(learner_id=learner_id, subject=item["subject"], grade_level=item["grade_level"],
                       question_type=item["question_type"], content=item["content"], answer=item["answer"],
                       analysis=item["analysis"], lang=detect_lang(item["answer"]),
                       source="collection-practice") for _, item in new_pairs]
    session.add_all(copies)
    session.flush()
    for (source_id, _), question in zip(new_pairs, copies):
        session.add(CollectionPracticeItem(version_id=version_id, source_question_id=source_id,
                                           question_id=question.id))
        session.add(ErrorItem(learner_id=learner_id, question_id=question.id, wrong_count=0, confidence=1.0,
                              workflow_strategy_id=strategy, workflow_tutor_enabled=tutor,
                              workflow_recap_enabled=recap))
    session.flush()
    return len(copies), len(mappings)
