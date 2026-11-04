"""确定性判题层：归一化 → 题型路由 → 符号等价 → 低置信度复核。

决策备忘（docs/DECISIONS-2026-10-31.md 第一节）：客观题用「归一化 + 符号等价」
确定性判定，LLM 只做讲评与低置信度复核，不参与对错决定。
"""

from cairn.grading.service import GradeResult, judge

__all__ = ["GradeResult", "judge"]
