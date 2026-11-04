"""英语拼写判定：词表比对为准，编辑距离/音码只做提示、不判对。

决策备忘：编辑距离会把 two/too 判错、their/there 放过；音码只能当提示。
所以判对只有一条路——归一化后完全相等；不一致时才用编辑距离和 soundex
生成「你是不是想写 X」。
"""

from __future__ import annotations

from dataclasses import dataclass

from cairn.grading.normalize import normalize_text


@dataclass(frozen=True)
class SpellingResult:
    correct: bool
    hint: str | None = None


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _soundex(word: str) -> str | None:
    """标准 Soundex（纯 Python，不引依赖）。只对纯拉丁词返回码。"""
    if not word.isascii() or not word.isalpha():
        return None
    codes = str.maketrans("", "", "aeiouyhw")
    groups = {"bfpv": "1", "cgjkqsxz": "2", "dt": "3", "l": "4", "mn": "5", "r": "6"}

    def code(ch: str) -> str:
        return next((v for k, v in groups.items() if ch in k), "")

    first = word[0].upper()
    digits, prev_code = [], ""
    for ch in word[1:].lower():
        if ch in "aeiouyhw":
            prev_code = ""  # 元音隔断：相邻同码不再合并
            continue
        c = code(ch)
        if c and c != prev_code:
            digits.append(c)
        prev_code = c
        if len(digits) == 3:
            break
    return first + "".join(digits).ljust(3, "0")


def check_spelling(gold: str, answer: str) -> SpellingResult:
    gold_norm, ans_norm = normalize_text(gold), normalize_text(answer)
    if not gold_norm or not ans_norm:
        return SpellingResult(False, None)
    if gold_norm == ans_norm:
        return SpellingResult(True, None)

    distance = _levenshtein(gold_norm, ans_norm)
    hint = f"正确拼写：{gold}"
    if distance <= 2 or _soundex(gold_norm) == _soundex(ans_norm):
        hint = f"你是不是想写 {gold}？"
    return SpellingResult(False, hint)
