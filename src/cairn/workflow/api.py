"""HTTP contract used by the separate workflow authoring app."""

from __future__ import annotations

import hmac
import json
import os
import re
from datetime import timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from cairn.models.workflow import Workflow, WorkflowVersion
from cairn.models.workflow_run import WorkflowRun
from cairn.tools.registry import status as tool_status
from cairn.strategies.registry import list_strategies
from cairn.workflow.service import (
    COLLECTION_WORKFLOW_ID,
    NODE_CATALOG,
    WorkflowError,
    activate,
    publish,
    validate_compile,
    validate_for_publish,
    validate_resource_references,
)

router = APIRouter(prefix="/api", tags=["workflow-authoring"])
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _operator(request: Request) -> str:
    expected = os.environ.get("CAIRN_WORKFLOW_ADMIN_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    token = supplied[7:] if supplied.startswith("Bearer ") else ""
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=503 if not expected else 401,
                            detail="工作流写入接口未配置 operator 凭据" if not expected else "operator 凭据无效")
    return "operator"


def _session(request: Request):
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="数据库尚未初始化")
    return factory()


def _workflow(session, workflow_id: str) -> Workflow:
    workflow = session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="找不到工作流")
    return workflow


def _version_payload(version: WorkflowVersion) -> dict:
    published_at = version.published_at
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return {
        "version_id": version.version_id,
        "workflow_id": version.workflow_id,
        "number": version.number,
        "runtime": json.loads(version.runtime_json),
        "published_by": version.published_by,
        "published_at": published_at.isoformat(),
    }


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return body


@router.get("/tools")
def tools_catalog() -> dict:
    return {"tools": tool_status(), "strategies": list_strategies(), "nodes": [
        {"node_id": key, "type": value["type"], "tool": value["tool"],
         "params": value["params"]}
        for key, value in NODE_CATALOG.items()
    ]}


@router.get("/workflows/{workflow_id}")
def get_workflow(request: Request, workflow_id: str) -> dict:
    with _session(request) as session:
        workflow = _workflow(session, workflow_id)
        return {"workflow_id": workflow.id, "title": workflow.title,
                "revision": workflow.revision, "graph": json.loads(workflow.draft_json),
                "active_version_id": workflow.active_version_id}


@router.post("/workflows/{workflow_id}")
async def save_workflow(request: Request, workflow_id: str) -> dict:
    _operator(request)
    learner_id = request.app.state.learner_id
    if not _ID_RE.fullmatch(workflow_id):
        raise HTTPException(status_code=400, detail="workflow_id 格式无效")
    body = await _json_body(request)
    expected_revision, title, graph = body.get("expected_revision"), body.get("title"), body.get("graph")
    if type(expected_revision) is not int or expected_revision < 0:
        raise HTTPException(status_code=400, detail="expected_revision 必须是非负整数")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
        raise HTTPException(status_code=400, detail="title 必须是 1 到 120 字符")
    try:
        validate_compile(graph, draft_only=True)
    except WorkflowError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    try:
        compiled = validate_for_publish(graph, workflow_id=workflow_id)
    except WorkflowError:
        compiled = None
    with _session(request) as session:
        if compiled is not None:
            try:
                validate_resource_references(session, learner_id, compiled)
            except WorkflowError:
                compiled = None
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            if expected_revision != 0:
                raise HTTPException(status_code=409, detail={"code": "revision_conflict", "revision": 0})
            workflow = Workflow(
                id=workflow_id,
                title=title.strip(),
                draft_json=json.dumps(graph, ensure_ascii=False, separators=(",", ":")),
                revision=1,
            )
            session.add(workflow)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise HTTPException(status_code=409, detail={"code": "revision_conflict", "revision": 1}) from None
            return {"workflow_id": workflow_id, "revision": 1,
                    "compiled_preview": compiled, "active_version_id": None}
        if workflow.revision != expected_revision:
            raise HTTPException(status_code=409, detail={"code": "revision_conflict", "revision": workflow.revision})
        result = session.execute(
            update(Workflow)
            .where(Workflow.id == workflow_id, Workflow.revision == expected_revision)
            .values(
                title=title.strip(),
                draft_json=json.dumps(graph, ensure_ascii=False, separators=(",", ":")),
                revision=expected_revision + 1,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            current_revision = session.scalar(select(Workflow.revision).where(Workflow.id == workflow_id))
            raise HTTPException(status_code=409, detail={"code": "revision_conflict", "revision": current_revision})
        active_version_id = workflow.active_version_id
        session.commit()
        return {"workflow_id": workflow_id, "revision": expected_revision + 1,
                "compiled_preview": compiled, "active_version_id": active_version_id}


@router.post("/workflows/{workflow_id}/validate")
async def validate_workflow(request: Request, workflow_id: str) -> dict:
    _operator(request)
    learner_id = request.app.state.learner_id
    body = await _json_body(request)
    graph = body.get("graph")
    try:
        compiled = validate_for_publish(graph, workflow_id=workflow_id)
        with _session(request) as session:
            validate_resource_references(session, learner_id, compiled)
    except WorkflowError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    return {"valid": True, "compiled_preview": compiled}


@router.post("/workflows/{workflow_id}/preview")
async def preview_workflow(request: Request, workflow_id: str) -> dict:
    _operator(request)
    learner_id = request.app.state.learner_id
    body = await _json_body(request)
    graph = body.get("graph")
    try:
        compiled = validate_for_publish(graph, workflow_id=workflow_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    with _session(request) as session:
        try:
            validate_resource_references(session, learner_id, compiled)
        except WorkflowError as exc:
            raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
        workflow = session.get(Workflow, workflow_id)
        active = session.get(WorkflowVersion, workflow.active_version_id) if workflow and workflow.active_version_id else None
        old = json.loads(active.runtime_json) if active else None
        return {"valid": True, "active_version_id": workflow.active_version_id if workflow else None,
                "current": old, "candidate": compiled}


@router.post("/workflows/{workflow_id}/publish")
async def publish_workflow(request: Request, workflow_id: str) -> dict:
    actor = _operator(request)
    learner_id = request.app.state.learner_id
    raw = await request.body()
    body = await _json_body(request) if raw else {}
    expected_revision = body.get("expected_revision")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise HTTPException(status_code=400, detail="expected_revision 必须是非负整数")
    with _session(request) as session:
        workflow = _workflow(session, workflow_id)
        try:
            version = publish(session, workflow, published_by=actor, expected_revision=expected_revision,
                              learner_id=learner_id)
        except WorkflowError as exc:
            session.rollback()
            raise HTTPException(status_code=409 if exc.code == "revision_conflict" else 422,
                                detail={"code": exc.code, "message": str(exc)}) from exc
        session.commit()
        return {**_version_payload(version), "active_version_id": workflow.active_version_id}


@router.get("/workflows/{workflow_id}/runs")
def list_runs(request: Request, workflow_id: str) -> dict:
    _operator(request)
    with _session(request) as session:
        _workflow(session, workflow_id)
        runs = session.scalars(select(WorkflowRun).join(
            WorkflowVersion, WorkflowVersion.version_id == WorkflowRun.version_id
        ).where(WorkflowVersion.workflow_id == workflow_id).order_by(
            WorkflowRun.updated_at.desc(), WorkflowRun.id.desc()
        ).limit(50)).all()
        return {"runs": [{"scan_id": run.scan_id, "version_id": run.version_id,
                          "status": run.status,
                          "updated_at": (run.updated_at.replace(tzinfo=timezone.utc)
                                         if run.updated_at.tzinfo is None else run.updated_at).isoformat()}
                         for run in runs]}


@router.get("/workflows/{workflow_id}/versions")
def list_versions(request: Request, workflow_id: str) -> dict:
    with _session(request) as session:
        workflow = _workflow(session, workflow_id)
        versions = session.scalars(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id
        ).order_by(WorkflowVersion.number.desc())).all()
        return {"active_version_id": workflow.active_version_id,
                "versions": [_version_payload(item) for item in versions]}


@router.get("/workflows/{workflow_id}/versions/{version_id}")
def get_version(request: Request, workflow_id: str, version_id: str) -> dict:
    with _session(request) as session:
        _workflow(session, workflow_id)
        version = session.get(WorkflowVersion, version_id)
        if version is None or version.workflow_id != workflow_id:
            raise HTTPException(status_code=404, detail="找不到该工作流版本")
        return _version_payload(version)


@router.post("/workflows/{workflow_id}/rollback/{version_id}")
def rollback_workflow(request: Request, workflow_id: str, version_id: str) -> dict:
    _operator(request)
    with _session(request) as session:
        workflow = _workflow(session, workflow_id)
        try:
            version = activate(session, workflow, version_id, learner_id=request.app.state.learner_id)
        except WorkflowError as exc:
            session.rollback()
            raise HTTPException(status_code=404 if exc.code == "version_not_found" else 422, detail=str(exc)) from exc
        session.commit()
        return {"active_version_id": workflow.active_version_id,
                "version": _version_payload(version)}


@router.post("/workflows/{workflow_id}/deactivate")
def deactivate_collection_workflow(request: Request, workflow_id: str) -> dict:
    _operator(request)
    if workflow_id != COLLECTION_WORKFLOW_ID:
        raise HTTPException(status_code=400, detail="只能停用题集练习流程")
    with _session(request) as session:
        _workflow(session, workflow_id)
        session.execute(update(Workflow).where(Workflow.id == workflow_id).values(active_version_id=None))
        session.commit()
        return {"active_version_id": None}
