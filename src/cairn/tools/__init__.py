"""工具层（底座第一步）：六样能力收成同一个形状，每个能单独关。

用法：

    from cairn.tools import is_enabled, disabled_reason, status

    if not is_enabled("ocr"):
        # 走降级分支，别崩
        ...

开关：`CAIRN_TOOL_<NAME>=0` 关掉（`CAIRN_TOOL_OCR` 还兼容老的 `CAIRN_OCR_DISABLE=1`）。
清单写死在 `registry.TOOLS`，不做动态发现。
"""

from __future__ import annotations

from .registry import (
    BY_NAME,
    TOOLS,
    ToolDisabled,
    UnknownTool,
    disabled_reason,
    get,
    is_enabled,
    require,
    status,
)
from .spec import ENV_PREFIX, ToolSpec

__all__ = [
    "BY_NAME",
    "ENV_PREFIX",
    "TOOLS",
    "ToolDisabled",
    "ToolSpec",
    "UnknownTool",
    "disabled_reason",
    "get",
    "is_enabled",
    "require",
    "status",
]
