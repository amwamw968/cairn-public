"""一份配方的形状：叫什么、有哪几步、每步失败时怎么办。

配方只描述**顺序与容错**，不描述怎么实现：`Step.tool` 写的是 `cairn.tools`
里的英文短名，真正干活的是 `runner` 里对应的执行器。所以同一个 `cut` 既能出现
在"扫一页卷子"里，也能出现在下一张卡的英语配方里，不用抄一遍代码。

`on_error` 只有三种，都表示"别崩，接着走"：

- `keep` 保持原样继续——这步没做成，页面就用没有它的样子（比如没摆正就用原图）
- `skip` 这步先不做——结果丢弃，后面的步骤照跑
- `ask`  把原因交给界面——页面把这句话显示给家长，让他决定（重拍/自己框）

写成别的值（比如 `raise`）等于**没有兜底**：这步失败就直接往上抛。这正是
`docs/PLAN.md` 里 AC7 的变异自证要用的口子——把 `on_error` 改成 `raise`，
AC4 必须红。
"""

from __future__ import annotations

from dataclasses import dataclass

# 三种兜底方式（写进 YAML 的 on_error）
ON_ERROR_KEEP = "keep"
ON_ERROR_SKIP = "skip"
ON_ERROR_ASK = "ask"

ON_ERROR_MODES = frozenset({ON_ERROR_KEEP, ON_ERROR_SKIP, ON_ERROR_ASK})


@dataclass(frozen=True)
class Step:
    """配方里的一步：叫什么（给人看）、用哪个工具、失败怎么办。"""

    name: str
    tool: str
    on_error: str = ON_ERROR_ASK


@dataclass(frozen=True)
class Recipe:
    """一份配方：一个短名（= YAML 文件名）、一句人话标题、有序的步骤。"""

    name: str
    title: str
    steps: tuple[Step, ...]
