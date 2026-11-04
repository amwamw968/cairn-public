"""Deterministic graph validation and immutable runtime compilation."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from cairn.models.workflow import Workflow, WorkflowVersion
from cairn.tools.registry import TOOLS
from cairn.strategies.registry import get_strategy

WORKFLOW_ID = "default"
COLLECTION_WORKFLOW_ID = "collection"
NODE_CATALOG = {
    "upright": {"type": "auto", "tool": "preprocess", "params": {}},
    "crop": {"type": "human", "tool": "crop", "params": {}},
    "cut": {"type": "auto", "tool": "cut", "params": {}},
    "select": {"type": "human", "tool": None, "params": {"preselect": "boolean"}},
    "recognize": {"type": "auto", "tool": "ocr", "params": {}},
    "review": {"type": "human", "tool": None, "params": {}},
    "enhance": {"type": "auto", "tool": "enhance", "params": {}},
    "strategy": {"type": "auto", "tool": None, "params": {"strategy": "strategy"}},
    "guided_tutor": {"type": "auto", "tool": None, "params": {}},
    "text_recap": {"type": "auto", "tool": None, "params": {}},
    "collection_append": {"type": "auto", "tool": None, "params": {"collection_id": "collection"}},
    "collection_source": {"type": "auto", "tool": None, "params": {"version_id": "collection_version"}},
}
_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class WorkflowError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_workflow") -> None:
        self.code = code
        super().__init__(message)


def default_graph() -> dict:
    nodes = [
        {"id": "upright-1", "node_id": "upright", "params": {}, "position": {"x": 80, "y": 90}},
        {"id": "crop-1", "node_id": "crop", "params": {}, "position": {"x": 360, "y": 90}},
        {"id": "cut-1", "node_id": "cut", "params": {}, "position": {"x": 640, "y": 90}},
        {"id": "recognize-1", "node_id": "recognize", "params": {}, "position": {"x": 920, "y": 90}},
    ]
    return {
        "nodes": nodes,
        "edges": [
            {"source": nodes[index]["id"], "target": nodes[index + 1]["id"]}
            for index in range(len(nodes) - 1)
        ],
    }


def default_executable_graph() -> dict:
    """Reader workflow with explicit selection and review; v1 stays immutable."""
    ids = ("upright", "crop", "cut", "select", "recognize", "review")
    nodes = [{"id": f"{node_id}-1", "node_id": node_id, "params": {},
              "position": {"x": 80 + index * 280, "y": 90}}
             for index, node_id in enumerate(ids)]
    return {"nodes": nodes, "edges": [
        {"source": nodes[index]["id"], "target": nodes[index + 1]["id"]}
        for index in range(len(nodes) - 1)
    ]}


def validate_compile(graph: object, *, draft_only: bool = False) -> dict:
    if not isinstance(graph, dict) or set(graph) != {"nodes", "edges"}:
        raise WorkflowError("工作流必须包含 nodes 和 edges，且不能含额外字段")
    nodes, edges = graph["nodes"], graph["edges"]
    if not isinstance(nodes, list) or (not nodes and not draft_only) or len(nodes) > 32:
        raise WorkflowError("nodes 必须是最多 32 个节点的列表；发布时不能为空")
    if not isinstance(edges, list) or len(edges) > (128 if draft_only else 31):
        raise WorkflowError("edges 数量超出限制")
    by_id: dict[str, dict] = {}
    for index, node in enumerate(nodes, 1):
        if (not isinstance(node, dict) or set(node) - {"id", "node_id", "params", "position"}
                or not {"id", "node_id", "params"} <= set(node)):
            raise WorkflowError(f"第 {index} 个节点必须包含 id、node_id、params，可选 position")
        key, node_id, params = node["id"], node["node_id"], node["params"]
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key) or key in by_id:
            raise WorkflowError(f"第 {index} 个节点 id 非法或重复")
        spec = NODE_CATALOG.get(node_id) if isinstance(node_id, str) else None
        if spec is None:
            raise WorkflowError(f"节点 {key} 使用未知 node_id：{node_id!r}")
        if not isinstance(params, dict):
            raise WorkflowError(f"节点 {key} 的 params 必须是对象")
        position = node.get("position", {"x": 0, "y": 0})
        if (
            not isinstance(position, dict)
            or set(position) != {"x", "y"}
            or any(
                type(position[axis]) not in (int, float)
                or not math.isfinite(position[axis])
                for axis in ("x", "y")
            )
        ):
            raise WorkflowError(f"节点 {key} 的 position 必须是包含数值 x/y 的对象")
        unknown = set(params) - set(spec["params"])
        if unknown:
            raise WorkflowError(f"节点 {key} 含不支持的参数：{', '.join(sorted(unknown))}")
        for name, kind in spec["params"].items():
            if name in params and kind in {"collection", "collection_version"} and (type(params[name]) is not int or params[name] <= 0):
                raise WorkflowError(f"节点 {key} 的题集编号无效")
            if name in params and (kind == "boolean" and type(params[name]) is not bool):
                raise WorkflowError(f"节点 {key} 参数 {name} 必须是布尔值")
            if name in params and kind == "strategy":
                if not isinstance(params[name], str):
                    raise WorkflowError(f"节点 {key} 的复习策略必须是字符串")
                try:
                    get_strategy(params[name])
                except LookupError as exc:
                    raise WorkflowError(f"节点 {key} 的复习策略未注册") from exc
        tool = spec["tool"]
        if tool is not None and tool not in {entry.name for entry in TOOLS}:
            raise WorkflowError(f"节点 {key} 依赖未注册的工具：{tool}")
        by_id[key] = node

    outgoing: dict[str, str] = {}
    incoming: dict[str, str] = {}
    edge_pairs: set[tuple[str, str]] = set()
    for index, edge in enumerate(edges, 1):
        if not isinstance(edge, dict) or set(edge) != {"source", "target"}:
            raise WorkflowError(f"第 {index} 条连线必须包含 source 和 target")
        source, target = edge["source"], edge["target"]
        if not isinstance(source, str) or not isinstance(target, str):
            raise WorkflowError(f"第 {index} 条连线的 source/target 必须是节点 id 字符串")
        if source not in by_id or target not in by_id or source == target:
            raise WorkflowError(f"第 {index} 条连线引用了无效节点")
        if (source, target) in edge_pairs:
            raise WorkflowError(f"第 {index} 条连线重复")
        edge_pairs.add((source, target))
        if draft_only:
            continue
        if source in outgoing or target in incoming:
            raise WorkflowError("MVP 仅支持单链路；不允许分支或汇合")
        outgoing[source], incoming[target] = target, source
    if draft_only:
        return {"steps": []}
    starts = [key for key in by_id if key not in incoming]
    if len(starts) != 1:
        raise WorkflowError("工作流必须只有一个起点；存在环或多个断开的链路")
    ordered, seen = [], set()
    current: str | None = starts[0]
    while current is not None:
        if current in seen:
            raise WorkflowError("工作流存在环")
        seen.add(current)
        node = by_id[current]
        resolved_params = dict(node["params"])
        if node["node_id"] == "strategy":
            resolved_params.setdefault("strategy", "fsrs")
        ordered.append({
            "node_id": node["node_id"],
            "type": NODE_CATALOG[node["node_id"]]["type"],
            "params": resolved_params,
        })
        current = outgoing.get(current)
    if len(seen) != len(by_id):
        raise WorkflowError("工作流有未连接节点")
    if len(edges) != len(nodes) - 1:
        raise WorkflowError("工作流必须是一条从头到尾的完整单链路")
    return {"steps": ordered}


def ensure_default_workflow(session: Session) -> Workflow:
    workflow = session.get(Workflow, WORKFLOW_ID)
    if workflow is not None:
        return workflow
    graph = default_graph()
    runtime = validate_compile(graph)
    version_id = f"{WORKFLOW_ID}:v1"
    workflow = Workflow(
        id=WORKFLOW_ID,
        title="默认扫描流程",
        draft_json=json.dumps(graph, ensure_ascii=False, separators=(",", ":")),
        revision=1,
        active_version_id=version_id,
    )
    session.add(workflow)
    session.add(WorkflowVersion(
        version_id=version_id,
        workflow_id=WORKFLOW_ID,
        number=1,
        graph_json=workflow.draft_json,
        runtime_json=json.dumps({"version_id": version_id, **runtime}, ensure_ascii=False, separators=(",", ":")),
        published_by="system",
    ))
    session.flush()
    return workflow


def ensure_collection_workflow(session: Session) -> None:
    """Create an unpublished collection draft without changing the scan flow."""
    graph = {"nodes": [{"id": "collection-source-1", "node_id": "collection_source", "params": {},
                        "position": {"x": 80, "y": 90}}], "edges": []}
    session.execute(sqlite_insert(Workflow).values(
        id=COLLECTION_WORKFLOW_ID, title="题集练习流程",
        draft_json=json.dumps(graph, ensure_ascii=False, separators=(",", ":")),
        revision=1, active_version_id=None, updated_at=datetime.now(timezone.utc),
    ).on_conflict_do_nothing())


def ensure_runnable_default(session: Session) -> None:
    """Upgrade only the untouched system default; never rewrite a version."""
    from cairn.workflow.contracts import validate_scan_runtime

    workflow = session.get(Workflow, WORKFLOW_ID)
    if workflow is None or workflow.active_version_id != "default:v1" or workflow.revision != 1:
        return
    old = session.get(WorkflowVersion, "default:v1")
    old_graph = json.dumps(default_graph(), ensure_ascii=False, separators=(",", ":"))
    if old is None or old.published_by != "system" or workflow.draft_json != old_graph or old.graph_json != old_graph:
        return
    graph = default_executable_graph()
    graph_json = json.dumps(graph, ensure_ascii=False, separators=(",", ":"))
    compiled = validate_compile(graph)
    validate_scan_runtime(compiled["steps"])
    version_id = "default:v2"
    runtime_json = json.dumps({"version_id": version_id, **compiled}, ensure_ascii=False, separators=(",", ":"))
    session.execute(sqlite_insert(WorkflowVersion).values(
        version_id=version_id, workflow_id=WORKFLOW_ID, number=2, graph_json=graph_json,
        runtime_json=runtime_json, published_by="system", published_at=datetime.now(timezone.utc),
    ).on_conflict_do_nothing())
    version = session.get(WorkflowVersion, version_id)
    if version is None or version.published_by != "system" or version.graph_json != graph_json or version.runtime_json != runtime_json:
        return
    session.execute(update(Workflow).where(
        Workflow.id == WORKFLOW_ID, Workflow.active_version_id == "default:v1",
        Workflow.revision == 1, Workflow.draft_json == old_graph,
    ).values(draft_json=graph_json, revision=2, active_version_id=version_id, updated_at=datetime.now(timezone.utc)))


def active_version_id(session: Session) -> str | None:
    workflow = session.get(Workflow, WORKFLOW_ID)
    return workflow.active_version_id if workflow else None


def validate_for_publish(graph: object, *, workflow_id: str = WORKFLOW_ID) -> dict:
    from cairn.workflow.contracts import validate_collection_runtime, validate_scan_runtime
    from cairn.workflow.execution import RunError

    compiled = validate_compile(graph)
    try:
        if workflow_id == COLLECTION_WORKFLOW_ID:
            validate_collection_runtime(compiled["steps"])
        else:
            validate_scan_runtime(compiled["steps"])
    except RunError as exc:
        raise WorkflowError(str(exc), code="unsupported_runtime") from exc
    return compiled


def validate_resource_references(session: Session, learner_id: int | None, runtime: dict) -> None:
    """An author can only publish a reference to the current learner's collection."""
    from cairn.models.collection import Collection

    if not isinstance(runtime, dict) or not isinstance(runtime.get("steps"), list):
        raise WorkflowError("工作流版本内容无效")
    for step in runtime["steps"]:
        if (not isinstance(step, dict) or not isinstance(step.get("node_id"), str)
                or not isinstance(step.get("params"), dict)):
            raise WorkflowError("工作流版本内容无效")
        if step.get("node_id") == "collection_append":
            collection_id = step["params"].get("collection_id")
            if type(collection_id) is not int or collection_id <= 0:
                raise WorkflowError("请选择有效的题集", code="invalid_resource")
            collection = session.get(Collection, collection_id)
            if collection is None or learner_id is None or collection.learner_id != learner_id:
                raise WorkflowError("题集不存在或不属于当前学习者", code="invalid_resource")
        elif step.get("node_id") == "collection_source":
            from cairn.collection_package import validated_version
            from cairn.collection_service import CollectionError

            version_id = step["params"].get("version_id")
            if type(version_id) is not int or version_id <= 0 or learner_id is None:
                raise WorkflowError("请选择有效的题集版本", code="invalid_resource")
            try:
                validated_version(session, learner_id, version_id)
            except CollectionError as exc:
                raise WorkflowError(str(exc), code="invalid_resource") from exc


def active_scan_runtime(session: Session, *, learner_id: int | None = None) -> tuple[str, bool]:
    """Return the chosen version and whether it uses the new reader runtime."""
    from cairn.workflow.contracts import validate_scan_runtime
    from cairn.workflow.execution import RunError

    workflow = session.get(Workflow, WORKFLOW_ID)
    def selected():
        if workflow is None or workflow.active_version_id is None:
            raise WorkflowError("尚未设置活动工作流")
        version = session.get(WorkflowVersion, workflow.active_version_id)
        if version is None:
            raise WorkflowError("活动工作流版本不存在")
        try:
            runtime = json.loads(version.runtime_json)
            if runtime.get("version_id") != version.version_id:
                raise ValueError
            legacy = (version.version_id == "default:v1" and version.published_by == "system"
                      and runtime.get("steps") == validate_compile(default_graph())["steps"])
            if not legacy:
                validate_scan_runtime(runtime.get("steps"))
            return version, runtime, legacy
        except (ValueError, TypeError, AttributeError, RunError) as exc:
            raise WorkflowError("活动工作流与当前扫描流程不兼容，请切换到可运行版本") from exc

    version, runtime, legacy = selected()
    if legacy:
        return version.version_id, False
    if any(step["node_id"] == "collection_append" for step in runtime["steps"]):
        # Serialize collection dependency checks with activation and deletion.
        # A switch can occur before this lock, so re-read the selected version.
        session.execute(update(Workflow).where(Workflow.id == WORKFLOW_ID).values(
            revision=Workflow.revision).execution_options(synchronize_session=False))
        session.refresh(workflow)
        version, runtime, legacy = selected()
        if legacy:
            return version.version_id, False
    validate_resource_references(session, learner_id, runtime)
    return version.version_id, True


def publish(session: Session, workflow: Workflow, *, published_by: str = "operator",
            expected_revision: int | None = None, learner_id: int | None = None) -> WorkflowVersion:
    revision = workflow.revision if expected_revision is None else expected_revision
    if workflow.revision != revision:
        raise WorkflowError("草稿已被更新，请刷新后再发布", code="revision_conflict")
    graph = json.loads(workflow.draft_json)
    runtime = validate_for_publish(graph, workflow_id=workflow.id)
    # Acquire the SQLite write lock while checking the draft we just compiled.
    # Another editor's save changes revision, preventing publication of a
    # different document between the caller's save and this operation.
    claimed = session.execute(update(Workflow).where(
        Workflow.id == workflow.id, Workflow.revision == revision
    ).values(updated_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise WorkflowError("草稿已被更新，请刷新后再发布", code="revision_conflict")
    validate_resource_references(session, learner_id, runtime)
    number = session.scalar(select(WorkflowVersion.number).where(
        WorkflowVersion.workflow_id == workflow.id
    ).order_by(WorkflowVersion.number.desc()).limit(1)) or 0
    next_number = number + 1
    version_id = f"{workflow.id}:v{next_number}"
    version = WorkflowVersion(
        version_id=version_id,
        workflow_id=workflow.id,
        number=next_number,
        graph_json=workflow.draft_json,
        runtime_json=json.dumps({"version_id": version_id, **runtime}, ensure_ascii=False, separators=(",", ":")),
        published_by=published_by[:64],
        published_at=datetime.now(timezone.utc),
    )
    session.add(version)
    workflow.active_version_id = version_id
    workflow.updated_at = datetime.now(timezone.utc)
    session.flush()
    return version


def activate(session: Session, workflow: Workflow, version_id: str, *, learner_id: int | None = None) -> WorkflowVersion:
    version = session.get(WorkflowVersion, version_id)
    if version is None or version.workflow_id != workflow.id:
        raise WorkflowError("找不到该工作流版本", code="version_not_found")
    session.execute(update(Workflow).where(Workflow.id == workflow.id).values(updated_at=datetime.now(timezone.utc)))
    try:
        runtime = json.loads(version.runtime_json)
        validate_resource_references(session, learner_id, runtime)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise WorkflowError("工作流版本内容无效") from exc
    workflow.active_version_id = version.version_id
    workflow.updated_at = datetime.now(timezone.utc)
    session.flush()
    return version
