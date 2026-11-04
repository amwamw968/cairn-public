"""工具开关的统一形状。

底座第一步：六样能力（预处理、切题、OCR、判题、词典、TTS）原先各用各的写法，
只有 OCR 有一个 `CAIRN_OCR_DISABLE`。这一层给每样能力一张 `ToolSpec`：

- 统一开关变量：`CAIRN_TOOL_<NAME>=0` 关掉，不写就是开（`default`）。
- 老变量由 `legacy_env` 兼容：老变量是**禁用开关**（写 1 才是关），
  所以 `CAIRN_OCR_DISABLE=1` 与 `CAIRN_TOOL_OCR=0` 等价。
- 只认环境变量：不读配置文件、不扫目录——配方层是下一张卡的事。

`enabled()` 每次现读环境变量、不做缓存：测试 monkeypatch 之后立刻生效，
线上改环境变量重启即生效，不会有"改了没反应"的隐藏状态。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_PREFIX = "CAIRN_TOOL_"

# 认得出"关"的写法。空字符串也算关：`CAIRN_TOOL_OCR=` 这种手滑不该被当成开。
_OFF_VALUES = frozenset({"0", "false", "off", "no", ""})
_ON_VALUES = frozenset({"1", "true", "on", "yes"})


def _flag(raw: str | None) -> bool | None:
    """环境变量原文 → True/False；没设置或认不出来 → None（用默认值）。"""
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in _OFF_VALUES:
        return False
    if text in _ON_VALUES:
        return True
    return None


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的全部元信息 + 开关判定。frozen：注册表建好之后不许被改。"""

    name: str
    title: str
    description: str
    env: str
    legacy_env: str | None = None
    default: bool = True

    def _off_switch(self) -> str | None:
        """是谁把它关掉的：返回开关的名字，没关返回 None。

        顺序与 `enabled()` 一致：新变量优先，其次老变量，最后才看默认值。
        """
        explicit = _flag(os.environ.get(self.env))
        if explicit is not None:
            return None if explicit else self.env
        if self.legacy_env is not None:
            legacy = _flag(os.environ.get(self.legacy_env))
            if legacy is not None:
                return None if not legacy else self.legacy_env
        return None if self.default else "默认关闭"

    def enabled(self) -> bool:
        """现在能不能用。每次现读环境变量，不缓存。"""
        return self._off_switch() is None

    def disabled_reason(self) -> str | None:
        """给运维看的一句话：为什么关了。开着的时候是 None。"""
        switch = self._off_switch()
        if switch is None:
            return None
        return f"{self.title}已关闭（{switch}）"
