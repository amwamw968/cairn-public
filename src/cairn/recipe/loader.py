"""读配方：`src/cairn/recipes/<name>.yaml` → `Recipe`，带缓存。

为什么带缓存：配方是启动时就不该变的东西，每次请求都去 parse 一遍 YAML 既慢
又多一个"文件正好写了一半"的失败面。缓存按名字存，测试/运维改完 YAML 调
`clear_cache()` 就能重新读。

为什么在这里校验工具名：配方写错（`cutt`、旧名、拼错）要在**加载时**就吵，
不能等到家长点开一页卷子才发现某一步永远跑不到。工具清单的唯一事实源是
`cairn.tools.registry`，这里只问它，不另存一份白名单。

所有报错都是中文，并且带出**是哪份配方、第几步、哪个字段 / 哪个名字**——
排查"配方为什么没生效"时，这行字就是全部线索。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from cairn import tools

from .spec import ON_ERROR_MODES, Recipe, Step

__all__ = [
    "RECIPE_DIR",
    "RecipeError",
    "clear_cache",
    "load",
    "load_all",
    "names",
]

# 配方放包外一层：`src/cairn/recipes/*.yaml`。改配方不用动 Python。
RECIPE_DIR = Path(__file__).resolve().parent.parent / "recipes"

# 配方名就是文件名：只允许字母数字连字符，免得 load("../x") 读到别处去。
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_CACHE: dict[str, Recipe] = {}


class RecipeError(Exception):
    """配方读不出来 / 写错了（缺字段、工具名不认识、YAML 坏了）。"""


def names() -> list[str]:
    """现有配方名（= 目录里的 `*.yaml` 文件名），按字母序。"""
    if not RECIPE_DIR.is_dir():
        return []
    return sorted(path.stem for path in RECIPE_DIR.glob("*.yaml"))


def load(name: str) -> Recipe:
    """按名字取一份配方；已经读过就直接给缓存里的那一份。"""
    if name in _CACHE:
        return _CACHE[name]
    if not _NAME_RE.match(name):
        raise RecipeError(f"配方名不合法：{name!r}（只能是字母、数字、连字符）")
    path = RECIPE_DIR / f"{name}.yaml"
    if not path.is_file():
        known = "、".join(names()) or "（一份都没有）"
        raise RecipeError(f"没有这份配方：{name}（现有：{known}；目录 {RECIPE_DIR}）")
    recipe = _parse(path, name)
    _CACHE[name] = recipe
    return recipe


def load_all() -> list[Recipe]:
    """目录里所有配方，按名字排序。"""
    return [load(name) for name in names()]


def clear_cache() -> None:
    """丢掉缓存：改完 YAML（测试、运维手工试）之后调一次。"""
    _CACHE.clear()


def _parse(path: Path, name: str) -> Recipe:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RecipeError(f"配方文件 {path.name} 读不出来：{exc}") from exc
    if not isinstance(payload, dict):
        raise RecipeError(
            f"配方文件 {path.name} 应该是一张映射（name / title / steps），实际是"
            f" {type(payload).__name__}"
        )
    declared = payload.get("name")
    if not isinstance(declared, str) or not declared.strip():
        raise RecipeError(f"配方文件 {path.name} 缺字段 name（或不是非空字符串）")
    if declared != name:
        raise RecipeError(
            f"配方 {name} 里的 name 写成了 {declared}（文件名和 name 必须一致）"
        )
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise RecipeError(f"配方 {name} 缺字段 title（一句人话，比如「扫一页卷子并切成题」）")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise RecipeError(f"配方 {name} 的字段 steps 必须是非空列表")
    steps = tuple(
        _parse_step(recipe=name, index=index, raw=raw)
        for index, raw in enumerate(raw_steps, start=1)
    )
    return Recipe(name=name, title=title, steps=steps)


def _parse_step(*, recipe: str, index: int, raw: object) -> Step:
    where = f"配方 {recipe} 的第 {index} 步"
    if not isinstance(raw, dict):
        raise RecipeError(f"{where} 应该是一张映射（name / tool / on_error）")
    step_name = raw.get("name")
    if not isinstance(step_name, str) or not step_name.strip():
        raise RecipeError(f"{where} 缺字段 name（这一步叫什么）")
    where = f"{where}（{step_name}）"
    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise RecipeError(f"{where}缺字段 tool（用哪个工具）")
    try:
        tools.get(tool)
    except tools.UnknownTool as exc:
        # UnknownTool 是 KeyError 子类，str() 会带引号；这里取它原话
        detail = exc.args[0] if exc.args else str(exc)
        raise RecipeError(f"{where}的工具名不认识：{detail}") from exc
    on_error = raw.get("on_error", "ask")
    if on_error not in ON_ERROR_MODES:
        raise RecipeError(
            f"{where}的字段 on_error 写成了 {on_error!r}（只能是 keep / skip / ask）"
        )
    return Step(name=step_name, tool=tool, on_error=on_error)
