"""工具注册表：**写死的一份清单**。

八样能力各一行，顺序就是 `/tools` 页上的顺序。这里故意不扫描目录、不按包名
动态导入：工具目录里出现一个新文件不该让线上行为自己变，装插件是下一张卡的事，
且要人点名批准。

对外四个函数：

    status()            八行字典（name/title/description/enabled/reason），给页面用
    is_enabled(name)    这个工具现在能不能用
    disabled_reason(name)  关了的话给一句人话，开着是 None
    require(name)       要用它：关着就抛 ToolDisabled（调用方决定怎么降级）

工具名写错（拼错、旧名）一律抛 UnknownTool，不悄悄返回 False——那样会让
"开关没生效"和"名字打错"长得一模一样，排查时最费时间。
"""

from __future__ import annotations

from .spec import ENV_PREFIX, ToolSpec

__all__ = [
    "TOOLS",
    "BY_NAME",
    "ToolDisabled",
    "UnknownTool",
    "disabled_reason",
    "get",
    "is_enabled",
    "require",
    "status",
]


class UnknownTool(KeyError):
    """问了一个不在清单里的工具名。"""


class ToolDisabled(RuntimeError):
    """工具被开关关掉了；`spec` 上是它的元信息，消息就是给家长/运维看的原因。"""

    def __init__(self, spec: ToolSpec):
        self.spec = spec
        super().__init__(spec.disabled_reason() or f"{spec.title}已关闭")


# 八样能力，写死。新增工具 = 在这里加一行 + 一个 ToolSpec，不走任何动态发现。
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="preprocess",
        title="预处理",
        description="找纸边并拉正",
        env=f"{ENV_PREFIX}PREPROCESS",
    ),
    ToolSpec(
        name="crop",
        title="裁边",
        description="把纸从背景里裁出来",
        env=f"{ENV_PREFIX}CROP",
    ),
    ToolSpec(
        name="enhance",
        title="增强",
        description="把纸压白、把字压黑",
        env=f"{ENV_PREFIX}ENHANCE",
    ),
    ToolSpec(
        name="cut",
        title="切题",
        description="把一页切成一道一道",
        env=f"{ENV_PREFIX}CUT",
    ),
    ToolSpec(
        name="ocr",
        title="文字识别",
        description="认出文字行与题号",
        env=f"{ENV_PREFIX}OCR",
        # 老变量：CAIRN_OCR_DISABLE=1 与 CAIRN_TOOL_OCR=0 等价（两处任一成立即关）
        legacy_env="CAIRN_OCR_DISABLE",
    ),
    ToolSpec(
        name="grading",
        title="判题",
        description="按标准答案判对错",
        env=f"{ENV_PREFIX}GRADING",
    ),
    ToolSpec(
        name="dict",
        title="词典",
        description="按科目补全提示",
        env=f"{ENV_PREFIX}DICT",
    ),
    ToolSpec(
        name="tts",
        title="朗读",
        description="把文字念出来",
        env=f"{ENV_PREFIX}TTS",
    ),
)

BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


def get(name: str) -> ToolSpec:
    """按名字取一条；不在清单里抛 UnknownTool（拼错要吵，不要静默）。"""
    try:
        return BY_NAME[name]
    except KeyError:
        raise UnknownTool(
            f"没有这个工具：{name}（现有：{', '.join(spec.name for spec in TOOLS)}）"
        ) from None


def status() -> list[dict]:
    """八行，给 `/tools` 页和调试用。`reason` 开着时是 None（AC2）。"""
    return [
        {
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "enabled": spec.enabled(),
            "reason": spec.disabled_reason(),
        }
        for spec in TOOLS
    ]


def is_enabled(name: str) -> bool:
    """这个工具现在能不能用。名字不在清单里抛 UnknownTool。"""
    return get(name).enabled()


def disabled_reason(name: str) -> str | None:
    """关掉的原因（一句人话）；开着返回 None。"""
    return get(name).disabled_reason()


def require(name: str) -> ToolSpec:
    """要用这个工具：关着就抛 ToolDisabled，开着返回它的 spec。"""
    spec = get(name)
    if not spec.enabled():
        raise ToolDisabled(spec)
    return spec
