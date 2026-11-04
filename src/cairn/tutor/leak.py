"""泄漏检测：判断 tutor 生成的一段文本是否把最终答案说漏了嘴。

规则：把答案与文本各自**归一化**后做包含比对——

- Unicode NFKC：全角数字/字母/符号折成半角（`３．５` → `3.5`）
- 转小写：`X` 与 `x` 一视同仁
- 去掉所有空白：`200 厘米` 与 `200厘米` 一视同仁

归一化后答案是文本的子串 → 泄漏。小范围整数答案还检查常见中文数字写法；宁可偶尔误拦
（拦了走预置兜底文案，孩子看到的话仍然通顺），不可漏放——PNAS RCT 里
练习期直给答案的那组，考试反而退步 17%。

**这是确定性代码，不是 prompt 约束**：LLM 的任何承诺都不算数，每段
（`explain` 除外）产出都必须过一遍这里。

注意这是「包含」判断：答案 `3` 时「3 个苹果」也会判泄漏。对小学题
这是可接受的保守偏差——被拦的代价是一段兜底文案，漏放的代价是研究
里那个 −17%。
"""

from __future__ import annotations

import re
import unicodedata

# 各类空白（含全角空格 \u3000）与零宽字符，全部算「没有」。
_WHITESPACE = re.compile(r"[\s\u200b\u200c\u200d\ufeff]+")
_INTEGER_ANSWER = re.compile(r"([+-]?\d{1,4})(?:个|只|本|支|厘米|毫米|米|元|块|秒|分钟|小时|千克|克|kg|cm|mm|m|%)?\Z")


def normalize_for_leak_check(text: str | None) -> str:
    """NFKC 折叠 + 小写 + 去空白。泄漏比对的唯一口径，测试也直接用。"""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text)
    return _WHITESPACE.sub("", folded).lower()


def _chinese_integer_variants(answer: str) -> set[str]:
    match = _INTEGER_ANSWER.fullmatch(answer)
    if match is None:
        return set()
    value = int(match.group(1))
    number = abs(value)
    digits = "零一二三四五六七八九"
    if number == 0:
        return {"零", "〇"}
    parts = []
    pending_zero = False
    for power, unit in ((3, "千"), (2, "百"), (1, "十"), (0, "")):
        digit = (number // (10 ** power)) % 10
        if not digit:
            if parts and number % (10 ** power):
                pending_zero = True
            continue
        if pending_zero:
            parts.append("零")
            pending_zero = False
        if not (digit == 1 and power == 1 and not parts):
            parts.append(digits[digit])
        parts.append(unit)
    text = ("负" if value < 0 else "") + "".join(parts)
    variants = {text, text.replace("二百", "两百"), text.replace("二千", "两千"),
                text.replace("二百", "两百").replace("二千", "两千")}
    return variants | {item.replace("零", "〇") for item in variants}


def is_leak(text: str | None, answer: str | None) -> bool:
    """文本（归一化后）包含答案（归一化后）→ True。

    答案为空（题库里没存标准答案）时无从泄漏，恒 False——此时兜底
    文案本身就是"没有标准答案"的说法，不存在泄漏面。
    """
    needle = normalize_for_leak_check(answer)
    if not needle:
        return False
    normalized = normalize_for_leak_check(text)
    return needle in normalized or any(candidate in normalized for candidate in _chinese_integer_variants(needle))
