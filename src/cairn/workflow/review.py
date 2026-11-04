"""Per-scan review decisions, persisted independently from confidence queues."""

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, aliased

from cairn.models.error_item import ErrorItem, REVIEW_STATUS_ARCHIVED
from cairn.models.learner import utcnow
from cairn.models.question import Question
from cairn.models.scan import Scan, ScanQuestion
from cairn.models.workflow_run import WorkflowReviewItem, WorkflowRun
from cairn.workflow import execution


def ready_for_practice(question_id):
    """SQL predicate excluding unfinished or explicitly rejected workflow items."""
    pending = (select(WorkflowReviewItem.id)
               .join(WorkflowRun, WorkflowRun.id == WorkflowReviewItem.run_id)
               .where(WorkflowReviewItem.question_id == question_id,
                      or_(WorkflowRun.status != "completed", WorkflowReviewItem.status == "rejected"))
               .exists())
    excluded_item = aliased(ErrorItem)
    rejected = select(excluded_item.id).where(
        excluded_item.question_id == question_id, excluded_item.workflow_rejected == 1
    ).correlate_except(excluded_item).exists()
    return ~pending & ~rejected


def prepare(session: Session, scan: Scan, run: WorkflowRun) -> None:
    """Create one pending decision per selected, owned question."""
    if run.scan_id != scan.id or run.version_id != scan.workflow_version_id:
        raise execution.RunError("照片与固定的工作流版本不一致")
    ids = set(session.scalars(select(ScanQuestion.question_id).where(
        ScanQuestion.scan_id == scan.id, ScanQuestion.question_id.is_not(None),
        ScanQuestion.is_wrong.is_(True),
    )))
    if scan.question_id is not None:
        ids.add(scan.question_id)
    owned = set(session.scalars(select(Question.id).where(
        Question.id.in_(ids), Question.learner_id == scan.learner_id,
    )))
    if not owned or owned != ids:
        raise execution.RunError("没有可复核的题目或题目归属不匹配")
    existing = set(session.scalars(select(WorkflowReviewItem.question_id).where(
        WorkflowReviewItem.run_id == run.id,
    )))
    for question_id in sorted(owned - existing):
        session.add(WorkflowReviewItem(run_id=run.id, question_id=question_id, status="pending"))
    session.flush()


def decide(session: Session, run: WorkflowRun, question_id: int, decision: str,
           handlers: dict) -> WorkflowRun:
    if decision not in {"confirmed", "rejected"}:
        raise execution.RunError("复核操作无效")
    item = session.scalar(select(WorkflowReviewItem).where(
        WorkflowReviewItem.run_id == run.id, WorkflowReviewItem.question_id == question_id,
    ))
    if item is None:
        raise execution.RunError("这道题不属于当前复核")
    scan = session.get(Scan, run.scan_id)
    question = session.get(Question, question_id)
    if (scan is None or scan.workflow_version_id != run.version_id
            or question is None or question.learner_id != scan.learner_id):
        raise execution.RunError("题目归属不匹配")
    if item.status == decision:
        return run
    if item.status != "pending":
        raise execution.RunError("这道题已经处理")
    steps = execution._runtime(session, run)
    if run.status != "waiting" or run.cursor >= len(steps) or steps[run.cursor]["node_id"] != "review":
        raise execution.RunError("当前流程没有等待复核")
    execution._claim(session, run)
    entries = list(session.scalars(select(ErrorItem).where(
        ErrorItem.learner_id == scan.learner_id, ErrorItem.question_id == question_id,
    )))
    if not entries:
        raise execution.RunError("题目尚未入库")
    for entry in entries:
        if decision == "confirmed":
            entry.confidence = 1.0
        elif entry.review_status != REVIEW_STATUS_ARCHIVED:
            entry.transition_to(REVIEW_STATUS_ARCHIVED)
        if decision == "rejected":
            entry.workflow_rejected = 1
    item.status = decision
    item.decided_at = utcnow()
    session.flush()
    remaining = session.scalar(select(WorkflowReviewItem.id).where(
        WorkflowReviewItem.run_id == run.id, WorkflowReviewItem.status == "pending",
    ).limit(1))
    if remaining is None:
        execution.resume(session, run, "review", lambda s, scan, params: None, handlers)
    return run
