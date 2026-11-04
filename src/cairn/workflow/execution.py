"""Transactional state machine for a scan's immutable published workflow.

Handlers receive the caller's session and must not commit it. Their database
changes and the step transition are committed together by the caller.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, attributes

from cairn.models.learner import utcnow
from cairn.models.scan import Scan
from cairn.models.workflow import WorkflowVersion
from cairn.models.workflow_run import WorkflowRun, WorkflowRunStep

Handler = Callable[[Session, Scan, dict[str, Any]], None]


class RunError(ValueError):
    """The scan cannot perform the requested workflow transition."""


def snapshot(session: Session, scan: Scan) -> dict:
    """Read a scan's progress without creating or advancing its run."""
    if scan.workflow_version_id is None:
        return {"version_id": None, "status": "legacy", "cursor": None, "steps": []}
    run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
    if run is not None and run.version_id != scan.workflow_version_id:
        raise RunError("照片与固定的工作流版本不一致")
    version = run or WorkflowRun(version_id=scan.workflow_version_id)
    steps = _runtime(session, version)
    rows = [] if run is None else list(session.scalars(
        select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)
        .order_by(WorkflowRunStep.step_index)
    ))
    if run is not None and (len(rows) != len(steps) or any(
        row.step_index != index or row.node_id != steps[index]["node_id"]
        for index, row in enumerate(rows)
    )):
        raise RunError("工作流步骤记录与发布版本不一致")
    return {
        "version_id": scan.workflow_version_id,
        "status": run.status if run else "not_started",
        "cursor": run.cursor if run else 0,
        "steps": [
            {"node_id": step["node_id"], "type": step["type"],
             "status": rows[index].status if run else "pending"}
            for index, step in enumerate(steps)
        ],
    }


def _runtime(session: Session, run: WorkflowRun) -> list[dict]:
    version = session.get(WorkflowVersion, run.version_id)
    if version is None:
        raise RunError("工作流版本不存在")
    try:
        document = json.loads(version.runtime_json)
        steps = document["steps"]
        if document["version_id"] != run.version_id or not isinstance(steps, list) or not steps:
            raise ValueError
        for step in steps:
            if (not isinstance(step, dict) or not isinstance(step.get("node_id"), str)
                    or step.get("type") not in {"auto", "human"}
                    or not isinstance(step.get("params"), dict)):
                raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise RunError("工作流版本内容无效") from exc
    return steps


def start(session: Session, scan: Scan) -> WorkflowRun:
    """Create one run for a version-pinned scan; caller owns the transaction."""
    if scan.id is None or scan.workflow_version_id is None:
        raise RunError("历史照片没有固定工作流版本")
    existing = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
    if existing is not None:
        return existing
    draft = WorkflowRun(scan_id=scan.id, version_id=scan.workflow_version_id,
                        cursor=0, revision=0, status="pending")
    steps = _runtime(session, draft)
    result = session.execute(
        sqlite_insert(WorkflowRun).values(
            scan_id=scan.id, version_id=scan.workflow_version_id, cursor=0,
            revision=0, status="pending", updated_at=utcnow(),
        ).on_conflict_do_nothing(index_elements=[WorkflowRun.scan_id])
    )
    run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
    if run is None:
        raise RunError("无法创建工作流进度")
    if result.rowcount == 1:
        session.add_all([
            WorkflowRunStep(run_id=run.id, step_index=index, node_id=step["node_id"], status="pending")
            for index, step in enumerate(steps)
        ])
        session.flush()
    return run


def _claim(session: Session, run: WorkflowRun) -> None:
    old_revision = run.revision
    changed = session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run.id, WorkflowRun.revision == old_revision)
        .values(revision=old_revision + 1, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise RunError("流程已在其他设备更新，请刷新后重试")
    attributes.set_committed_value(run, "revision", old_revision + 1)


def _step_row(session: Session, run: WorkflowRun) -> WorkflowRunStep:
    row = session.scalar(select(WorkflowRunStep).where(
        WorkflowRunStep.run_id == run.id, WorkflowRunStep.step_index == run.cursor
    ))
    if row is None:
        raise RunError("工作流进度记录不完整")
    return row


def _advance(session: Session, run: WorkflowRun, handlers: Mapping[str, Handler]) -> WorkflowRun:
    steps = _runtime(session, run)
    scan = session.get(Scan, run.scan_id)
    if scan is None or scan.workflow_version_id != run.version_id:
        raise RunError("照片与固定的工作流版本不一致")
    while run.cursor < len(steps):
        step = steps[run.cursor]
        row = _step_row(session, run)
        if row.node_id != step["node_id"]:
            raise RunError("工作流步骤记录与发布版本不一致")
        if step["type"] == "human":
            row.status = "waiting"
            run.status = "waiting"
            run.error = None
            session.flush()
            return run
        handler = handlers.get(step["node_id"])
        if handler is None:
            run.status = "failed"
            row.status = "failed"
            run.error = f"没有可执行的节点：{step['node_id']}"
            session.flush()
            return run
        try:
            with session.begin_nested():
                handler(session, scan, step["params"])
                row.status = "completed"
                row.completed_at = utcnow()
                run.cursor += 1
                session.flush()
        except Exception as exc:
            run.status = "failed"
            run.error = f"节点 {step['node_id']} 执行失败（{type(exc).__name__}）"
            row.status = "failed"
            session.flush()
            return run
    run.status = "completed"
    run.error = None
    session.flush()
    return run


def advance(session: Session, run: WorkflowRun, handlers: Mapping[str, Handler], *, retry: bool = False) -> WorkflowRun:
    if run.status == "completed" or run.status == "waiting":
        return run
    if run.status == "failed" and not retry:
        raise RunError("流程失败后需要显式重试")
    _claim(session, run)
    return _advance(session, run, handlers)


def skip_failed_cut(session: Session, run: WorkflowRun, handlers: Mapping[str, Handler]) -> WorkflowRun:
    """Let a reader explicitly replace a failed automatic cut with manual frames."""
    steps = _runtime(session, run)
    if (run.status != "failed" or run.cursor >= len(steps)
            or steps[run.cursor]["node_id"] != "cut"):
        raise RunError("只有切题失败时才能选择手动画框")
    _claim(session, run)
    row = _step_row(session, run)
    if row.node_id != "cut" or row.status != "failed":
        raise RunError("工作流切题状态不一致")
    row.status = "skipped"
    row.completed_at = utcnow()
    run.cursor += 1
    run.error = None
    session.flush()
    return _advance(session, run, handlers)


def skip_failed_collection(session: Session, run: WorkflowRun, handlers: Mapping[str, Handler]) -> WorkflowRun:
    """Explicitly skip a failed collection output without discarding questions."""
    steps = _runtime(session, run)
    if (run.status != "failed" or run.cursor >= len(steps)
            or steps[run.cursor]["node_id"] != "collection_append"):
        raise RunError("只有题集收录失败时才能跳过")
    _claim(session, run)
    row = _step_row(session, run)
    if row.node_id != "collection_append" or row.status != "failed":
        raise RunError("工作流题集状态不一致")
    row.status = "skipped"
    row.completed_at = utcnow()
    run.cursor += 1
    run.error = None
    session.flush()
    return _advance(session, run, handlers)


def resume(session: Session, run: WorkflowRun, node_id: str, action: Handler,
           handlers: Mapping[str, Handler]) -> WorkflowRun:
    if run.status != "waiting":
        raise RunError("当前流程没有等待人工处理")
    steps = _runtime(session, run)
    if (run.cursor >= len(steps) or steps[run.cursor]["node_id"] != node_id
            or steps[run.cursor]["type"] != "human"):
        raise RunError("当前等待的步骤与提交内容不一致")
    _claim(session, run)
    scan = session.get(Scan, run.scan_id)
    if scan is None or scan.workflow_version_id != run.version_id:
        raise RunError("照片与固定的工作流版本不一致")
    row = _step_row(session, run)
    if row.node_id != node_id or row.status != "waiting":
        raise RunError("人工步骤记录与当前进度不一致")
    with session.begin_nested():
        action(session, scan, steps[run.cursor]["params"])
        row.status = "completed"
        row.completed_at = utcnow()
        run.cursor += 1
        run.status = "pending"
        session.flush()
    return _advance(session, run, handlers)
