"""配方层（底座第二步）：一页纸怎么走，写在 YAML 里，不在代码里。

用法：

    from cairn.recipe import load, run

    result = run(load("scan-entry"), {"path": path, "scan": scan})
    result.trace()      # 摆正 ✓ · 切题 ✓ · 认字 ✓ 1.1s
    result.data["rows"]  # 认字结果；哪步没做成，对应的键就不在

三份东西各管一件事：`spec` 定形状，`loader` 读 YAML 并校验工具名，
`runner` 按顺序跑、每步留痕。`src/cairn/recipes/*.yaml` 才是配方本体。
"""

from __future__ import annotations

from .loader import RECIPE_DIR, RecipeError, clear_cache, load, load_all, names
from .runner import (
    FAILED_FALLBACK,
    RAN,
    SKIPPED_DISABLED,
    SKIPPED_UNAVAILABLE,
    RunResult,
    StepResult,
    StepUnavailable,
    run,
)
from .spec import ON_ERROR_ASK, ON_ERROR_KEEP, ON_ERROR_MODES, ON_ERROR_SKIP, Recipe, Step

__all__ = [
    "FAILED_FALLBACK",
    "ON_ERROR_ASK",
    "ON_ERROR_KEEP",
    "ON_ERROR_MODES",
    "ON_ERROR_SKIP",
    "RAN",
    "RECIPE_DIR",
    "Recipe",
    "RecipeError",
    "RunResult",
    "SKIPPED_DISABLED",
    "SKIPPED_UNAVAILABLE",
    "Step",
    "StepResult",
    "StepUnavailable",
    "clear_cache",
    "load",
    "load_all",
    "names",
    "run",
]
