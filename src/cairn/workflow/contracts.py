"""Executable scan workflow constraints for the first reader runtime."""

from cairn.strategies.registry import get_strategy
from cairn.workflow.execution import RunError

_KINDS = {
    "upright": "auto",
    "crop": "human",
    "enhance": "auto",
    "cut": "auto",
    "select": "human",
    "recognize": "auto",
    "review": "human",
    "strategy": "auto",
    "guided_tutor": "auto",
    "text_recap": "auto",
    "collection_append": "auto",
}


def validate_scan_runtime(steps: object) -> None:
    """Reject graphs the current scan UI cannot safely execute or resume."""
    if not isinstance(steps, list) or not steps:
        raise RunError("工作流需要完整的扫描步骤")
    ids = []
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("node_id"), str):
            raise RunError("工作流步骤格式无效")
        node_id = step["node_id"]
        if node_id not in _KINDS or step.get("type") != _KINDS[node_id]:
            raise RunError(f"此节点尚不能在扫描流程中运行：{node_id}")
        if node_id in ids:
            raise RunError(f"扫描流程不能重复该节点：{node_id}")
        if not isinstance(step.get("params"), dict):
            raise RunError(f"节点参数无效：{node_id}")
        ids.append(node_id)
    # Cut depends on recognition and supplies the existing selection page;
    # the explicit recognition step exposes the already captured text.
    required = ["upright", "crop", "cut", "select", "recognize", "review"]
    if [node for node in ids if node not in {"enhance", "strategy", "guided_tutor", "text_recap", "collection_append"}] != required:
        raise RunError("扫描流程必须按顺序包含摆正、裁切、切题、圈选、认字和复核")
    if "enhance" in ids and not ids.index("crop") < ids.index("enhance") < ids.index("cut"):
        raise RunError("增强必须位于裁切之后、切题之前")
    if "guided_tutor" in ids and ids.index("guided_tutor") < ids.index("review"):
        raise RunError("引导讲解必须位于人工复核之后")
    if "text_recap" in ids and ids.index("text_recap") < ids.index("review"):
        raise RunError("文字复述必须位于人工复核之后")
    if "collection_append" in ids:
        if ids.index("collection_append") < ids.index("review"):
            raise RunError("收录到题集必须位于人工复核之后")
        params = steps[ids.index("collection_append")]["params"]
        if set(params) != {"collection_id"} or type(params["collection_id"]) is not int or params["collection_id"] <= 0:
            raise RunError("请选择有效的题集")
    if "strategy" in ids:
        if ids.index("strategy") < ids.index("recognize"):
            raise RunError("复习策略必须在认字之后设置")
        params = steps[ids.index("strategy")]["params"]
        if set(params) != {"strategy"} or not isinstance(params["strategy"], str):
            raise RunError("请选择复习策略")
        try:
            get_strategy(params["strategy"])
        except LookupError as exc:
            raise RunError("复习策略未注册") from exc


def validate_collection_runtime(steps: object) -> None:
    """Validate a separate, fully automatic collection-to-practice workflow."""
    if not isinstance(steps, list) or not steps:
        raise RunError("题集流程需要题集输入")
    allowed = {"collection_source", "strategy", "guided_tutor", "text_recap"}
    seen = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != {"node_id", "type", "params"}:
            raise RunError("工作流步骤格式无效")
        node_id, params = step["node_id"], step["params"]
        if not isinstance(node_id, str) or node_id not in allowed or step["type"] != "auto":
            raise RunError("此节点不能在题集流程中运行")
        if node_id in seen:
            raise RunError("题集流程不能重复节点")
        seen.add(node_id)
        if not isinstance(params, dict):
            raise RunError("节点参数无效")
        if node_id == "collection_source":
            if (index != 0 or set(params) != {"version_id"}
                    or type(params["version_id"]) is not int or params["version_id"] <= 0):
                raise RunError("请选择题集的已发布版本作为起点")
        elif node_id == "strategy":
            if set(params) != {"strategy"} or not isinstance(params["strategy"], str):
                raise RunError("请选择复习策略")
            try:
                get_strategy(params["strategy"])
            except LookupError as exc:
                raise RunError("复习策略未注册") from exc
        elif params:
            raise RunError("节点参数无效")
    if steps[0]["node_id"] != "collection_source":
        raise RunError("请选择题集的已发布版本作为起点")
