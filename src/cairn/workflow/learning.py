"""Learning node handlers using Cairn's existing review strategy registry."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.models.error_item import ErrorItem, REVIEW_STATUS_ARCHIVED
from cairn.models.scan import Scan, ScanQuestion
from cairn.models.collection import CollectionItem
from cairn.models.question import Question
from cairn.models.workflow_run import WorkflowReviewItem, WorkflowRun
from cairn.strategies.registry import get_strategy


def apply_strategy(session: Session, scan: Scan, params: dict) -> None:
    """Bind a registered strategy to this scan's unreviewed learning items."""
    strategy_id = params.get("strategy", "fsrs")
    if not isinstance(strategy_id, str):
        raise ValueError("复习策略必须是字符串")
    get_strategy(strategy_id)
    question_ids = set(session.scalars(select(ScanQuestion.question_id).where(
        ScanQuestion.scan_id == scan.id, ScanQuestion.question_id.is_not(None)
    )))
    if scan.question_id is not None:
        question_ids.add(scan.question_id)
    if not question_ids:
        raise ValueError("扫描中没有已收录的学习题目")
    entries = list(session.scalars(select(ErrorItem).where(
        ErrorItem.learner_id == scan.learner_id,
        ErrorItem.question_id.in_(question_ids),
    )))
    if not entries:
        raise ValueError("扫描中没有可设置策略的学习题目")
    items = [item for item in entries if item.review_status != REVIEW_STATUS_ARCHIVED]
    for item in items:
        if item.last_reviewed_at is not None and item.workflow_strategy_id != strategy_id:
            raise ValueError("已复习题目的策略不能直接切换")
    for item in items:
        item.workflow_strategy_id = strategy_id


def enable_guided_tutor(session: Session, scan: Scan, params: dict) -> None:
    """Enable the existing text tutor for confirmed items from this scan."""
    question_ids = set(session.scalars(select(ScanQuestion.question_id).where(
        ScanQuestion.scan_id == scan.id, ScanQuestion.question_id.is_not(None),
    )))
    if scan.question_id is not None:
        question_ids.add(scan.question_id)
    if not question_ids:
        raise ValueError("扫描中没有已收录的学习题目")
    entries = list(session.scalars(select(ErrorItem).where(
        ErrorItem.learner_id == scan.learner_id,
        ErrorItem.question_id.in_(question_ids),
        ErrorItem.review_status != REVIEW_STATUS_ARCHIVED,
    )))
    for item in entries:
        item.workflow_tutor_enabled = 1


def enable_text_recap(session: Session, scan: Scan, params: dict) -> None:
    """Enable text recall for accepted questions from this scan."""
    question_ids = set(session.scalars(select(ScanQuestion.question_id).where(
        ScanQuestion.scan_id == scan.id, ScanQuestion.question_id.is_not(None),
    )))
    if scan.question_id is not None:
        question_ids.add(scan.question_id)
    if not question_ids:
        raise ValueError("扫描中没有已收录的学习题目")
    entries = session.scalars(select(ErrorItem).where(
        ErrorItem.learner_id == scan.learner_id,
        ErrorItem.question_id.in_(question_ids),
        ErrorItem.review_status != REVIEW_STATUS_ARCHIVED,
    ))
    for item in entries:
        item.workflow_recap_enabled = 1


def append_confirmed_to_collection(session: Session, scan: Scan, params: dict) -> None:
    """Append this scan's confirmed questions to an owned collection draft."""
    from cairn.collection_service import MAX_ITEMS, CollectionError, _claim, _owned

    collection_id = params.get("collection_id")
    if type(collection_id) is not int or collection_id <= 0:
        raise CollectionError("请选择有效的题集")
    collection = _owned(session, scan.learner_id, collection_id)
    run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
    if run is None or run.version_id != scan.workflow_version_id:
        raise CollectionError("卷子的工作流进度无效")
    confirmed = list(session.scalars(select(WorkflowReviewItem.question_id).where(
        WorkflowReviewItem.run_id == run.id, WorkflowReviewItem.status == "confirmed"
    ).order_by(WorkflowReviewItem.id)))
    if not confirmed:
        return
    questions = list(session.scalars(select(Question).where(
        Question.id.in_(confirmed), Question.learner_id == scan.learner_id)))
    if len(questions) != len(confirmed) or any(q.subject != collection.subject for q in questions):
        raise CollectionError("已确认题目的学科与题集不一致")
    accepted = set(session.scalars(select(ErrorItem.question_id).where(
        ErrorItem.learner_id == scan.learner_id, ErrorItem.question_id.in_(confirmed),
        ErrorItem.workflow_rejected == 0, ErrorItem.review_status != REVIEW_STATUS_ARCHIVED)))
    if accepted != set(confirmed):
        raise CollectionError("题目已被排除或不在学习库中")
    _claim(session, collection, collection.revision)
    existing = set(session.scalars(select(CollectionItem.question_id).where(
        CollectionItem.collection_id == collection.id)))
    new_ids = [qid for qid in confirmed if qid not in existing]
    if len(existing) + len(new_ids) > MAX_ITEMS:
        raise CollectionError("题集已达到题目数量上限")
    position = session.scalar(select(func.max(CollectionItem.position)).where(
        CollectionItem.collection_id == collection.id))
    start = 0 if position is None else position + 1
    session.add_all([CollectionItem(collection_id=collection.id, question_id=qid, position=start + index,
                                    tags_json="[]", difficulty=None, source="人工核对卷面")
                     for index, qid in enumerate(new_ids)])
    session.flush()
