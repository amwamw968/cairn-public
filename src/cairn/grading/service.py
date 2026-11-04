"""判分服务：judge(question, child_answer) -> GradeResult。

题型路由：计算/填空 → 符号等价；选择 → 选项比对；判断 → 对错同义词；
拼写/听写 → 词表比对（拼音不判对，见 spelling 模块）；其余按标准答案的
形态自动路由。confidence < 0.8 → needs_review，交家长复核，不硬判。
LLM 不参与对错判定——本层全部是确定性规则。
"""

from __future__ import annotations

from dataclasses import dataclass

from cairn.grading import math_equiv, spelling
from cairn.grading.normalize import ParsedAnswer, normalize_text, parse_answer

_REVIEW_THRESHOLD = 0.8

_MATH_TYPES = {"calc", "计算", "计算题", "fill", "填空", "apply", "应用", "应用题"}
_CHOICE_TYPES = {"choice", "选择", "选择题"}
_JUDGE_TYPES = {"judge", "判断", "判断题"}
_SPELL_TYPES = {"spelling", "拼写", "dictation", "听写", "word", "单词"}
_TRUE_SET = {"对", "正确", "√", "✓", "t", "true", "是", "y", "yes"}
_FALSE_SET = {"错", "错误", "×", "x", "✗", "f", "false", "否", "n", "no"}


@dataclass(frozen=True)
class GradeResult:
    correct: bool
    confidence: float
    reason: str
    hint: str | None = None
    needs_review: bool = False


def _result(correct: bool, confidence: float, reason: str, hint: str | None = None) -> GradeResult:
    return GradeResult(correct, confidence, reason, hint,
                       needs_review=confidence < _REVIEW_THRESHOLD)


def _route(question) -> str:
    qtype = (getattr(question, "question_type", "") or "").strip().lower()
    subject = (getattr(question, "subject", "") or "").strip().lower()
    is_en = "英" in subject or "english" in subject
    if qtype in _CHOICE_TYPES:
        return "choice"
    if qtype in _JUDGE_TYPES:
        return "judge"
    # 数学题型不看学科：英语科目里的计算/填空也是数学题，必须走符号等价，
    # 不能落进拼写词表比对（否则 1/2 答 0.5 会被判错）。
    if qtype in _MATH_TYPES:
        return "math"
    if qtype in _SPELL_TYPES:
        return "spelling" if is_en else "text"
    return "auto"


def judge(question, child_answer: str) -> GradeResult:
    gold_raw = getattr(question, "answer", None) or ""
    if not gold_raw.strip():
        return _result(False, 0.5, "题目无标准答案，无法自动判定")
    route = _route(question)
    if route == "choice":
        return _judge_choice(gold_raw, child_answer)
    if route == "judge":
        return _judge_truefalse(gold_raw, child_answer)
    if route == "spelling":
        return _judge_spelling(gold_raw, child_answer)
    if route == "text":
        return _judge_text(gold_raw, child_answer)
    return _judge_math_or_text(gold_raw, child_answer)


# --- 各题型判定 ---------------------------------------------------------------


def _judge_math_or_text(gold_raw: str, child_answer: str) -> GradeResult:
    gold, ans = parse_answer(gold_raw), parse_answer(child_answer)
    if gold.kind == "empty":
        return _result(False, 0.5, "标准答案为空，无法自动判定")
    if gold.kind == "word" or ans.kind == "word":
        return _judge_text(gold_raw, child_answer)
    if gold.kind == "ratio" or ans.kind == "ratio":
        if gold.kind == "ratio" and ans.kind == "ratio":
            correct = math_equiv.ratio_equivalent(gold.items, ans.items)
            return _result(correct, 1.0, "比号按比判定（不折算成商）")
        return _result(
            False,
            1.0,
            "比号题需按比的形式作答（如 3:4），不能写成商"
            if gold.kind == "ratio" else "答案不应写成比的形式",
        )
    if gold.kind in ("multi", "set") or ans.kind in ("multi", "set"):
        return _judge_structured(gold, ans)
    if gold.kind == "unit_math" or ans.kind == "unit_math":
        if gold.kind == ans.kind and gold.text == ans.text:
            return _result(True, 1.0, "带单位答案原文一致")
        return _result(False, 0.5, "答案带单位，无法确定换算，转人工复核")
    verdict = math_equiv.is_equivalent(gold.text, ans.text)
    if verdict is None:
        return _result(False, 0.5, "答案无法解析成数学对象，转人工复核")
    return _result(verdict, 1.0, "符号等价判定")


def _judge_structured(gold: ParsedAnswer, ans: ParsedAnswer) -> GradeResult:
    if gold.kind != ans.kind:
        return _result(False, 1.0, f"答案形式不一致：{gold.kind} vs {ans.kind}")
    if len(gold.items) != len(ans.items):
        return _result(False, 1.0, "答案个数不一致")
    if gold.kind == "set":  # 集合无序：贪心配对
        remaining = list(ans.items)
        for g in gold.items:
            match = next((a for a in remaining if _pair_ok(g, a)), None)
            if match is None:
                return _result(False, 1.0, "集合元素不一致")
            remaining.remove(match)
        return _result(True, 1.0, "集合等价")
    for g, a in zip(gold.items, ans.items):  # 并列答案按顺序
        if not _pair_ok(g, a):
            return _result(False, 1.0, "并列答案不一致")
    return _result(True, 1.0, "并列答案全部等价")


def _pair_ok(g: ParsedAnswer, a: ParsedAnswer) -> bool:
    if g.kind == "math" and a.kind == "math":
        return math_equiv.is_equivalent(g.text, a.text) is True
    return g.text == a.text


def _judge_choice(gold_raw: str, child_answer: str) -> GradeResult:
    if not child_answer.strip():
        return _result(False, 0.5, "未作答，转人工复核")
    correct = normalize_text(gold_raw) == normalize_text(child_answer)
    return _result(correct, 1.0, "选项比对")


def _judge_truefalse(gold_raw: str, child_answer: str) -> GradeResult:
    gold, ans = normalize_text(gold_raw), normalize_text(child_answer)
    gold_val = _truefalse_value(gold)
    ans_val = _truefalse_value(ans)
    if gold_val is None or ans_val is None:
        return _result(False, 0.5, "判断题答案无法识别，转人工复核")
    return _result(gold_val == ans_val, 1.0, "判断题比对")


def _truefalse_value(text: str) -> bool | None:
    if text in _TRUE_SET:
        return True
    if text in _FALSE_SET:
        return False
    return None


def _judge_spelling(gold_raw: str, child_answer: str) -> GradeResult:
    if not child_answer.strip():
        return _result(False, 0.5, "未作答，转人工复核")
    res = spelling.check_spelling(gold_raw, child_answer)
    return _result(res.correct, 1.0, "拼写词表比对", hint=res.hint)


def _judge_text(gold_raw: str, child_answer: str) -> GradeResult:
    if not child_answer.strip():
        return _result(False, 0.5, "未作答，转人工复核")
    res = spelling.check_spelling(gold_raw, child_answer)
    return _result(res.correct, 1.0, "文本比对", hint=res.hint)
