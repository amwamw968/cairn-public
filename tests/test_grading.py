"""判题确定性层单测：五个静默误判 + 数学等价 + 拼写 + 低置信度复核。

依据：docs/DECISIONS-2026-10-31.md 第一节（30 例实测：裸比对 6/30、
裸 Math-Verify 18/30、归一化外壳 29/30——本批用例复现的口径见
test_normalization_batch，本机数字为 2/17/29，见 EVIDENCE）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cairn.grading import judge
from cairn.grading.math_equiv import is_equivalent
from cairn.grading.normalize import parse_answer
from cairn.grading.spelling import check_spelling


def math_question(answer: str):
    return SimpleNamespace(subject="数学", question_type="计算", answer=answer)


# --- 决策备忘五个静默误判（各一条，修复前后必须可分辨） -----------------------


def test_pit1_ratio_is_not_quotient():
    """3:4 不能被当 3/4 或 0.75；比等价按交叉相乘。"""
    assert parse_answer("3:4").kind == "ratio"
    assert judge(math_question("3:4"), "3∶4").correct  # 全角比号
    assert judge(math_question("3:4"), "6:8").correct  # 同比值
    assert not judge(math_question("3:4"), "3/4").correct  # 当商 → 判错
    assert not judge(math_question("3:4"), "0.75").correct


def test_pit2_chinese_wan_unit():
    """100万 → 1000000，不是 100。"""
    assert parse_answer("100万").text == "1000000"
    assert judge(math_question("100万"), "1000000").correct
    assert not judge(math_question("100万"), "100").correct
    assert judge(math_question("3.5万"), "35000").correct


def test_pit3_comma_answers_are_not_decimal():
    """"2,3" 是两个独立答案，不是 2.3。"""
    assert parse_answer("2,3").kind == "multi"
    assert judge(math_question("2,3"), "2，3").correct  # 顿号/全角逗号
    assert not judge(math_question("2,3"), "2.3").correct
    assert judge(math_question("2,3"), "3,2").correct is False  # 顺序敏感
    assert parse_answer("1,000").text == "1000"  # 千分位不当并列


def test_pit4_set_survives():
    """{2,3} 是两个元素的集合，不是空。"""
    parsed = parse_answer("{2,3}")
    assert parsed.kind == "set" and len(parsed.items) == 2
    assert judge(math_question("{2,3}"), "{3,2}").correct  # 无序
    assert not judge(math_question("{2,3}"), "{}").correct
    assert not judge(math_question("{2,3}"), "2.3").correct


def test_pit5_fullwidth_minus_and_chinese_numerals():
    """−3（U+2212）→ -3；中文数字 → 数值。"""
    assert parse_answer("−3").text == "-3"
    assert judge(math_question("-3"), "−3").correct
    assert parse_answer("二十五").text == "25"
    assert judge(math_question("25"), "二十五").correct
    assert judge(math_question("一百零三"), "103").correct
    assert judge(math_question("两"), "2").correct


# --- 数学等价（AC2） -----------------------------------------------------------


@pytest.mark.parametrize(
    "gold,ans,expected",
    [
        ("1/2", "0.5", True),
        ("2+3", "5", True),
        ("√4", "2", True),
        ("1/2", "0.51", False),
    ],
)
def test_math_equivalence(gold, ans, expected):
    gold_parsed, ans_parsed = parse_answer(gold), parse_answer(ans)
    assert gold_parsed.kind == ans_parsed.kind == "math"
    assert is_equivalent(gold_parsed.text, ans_parsed.text) is expected


def test_math_unparseable_goes_to_review():
    """math-verify 没解析出数学对象（全是字符串兜底）→ None，不硬判。"""
    assert is_equivalent("5", "((") is None


def test_unit_answers_not_silently_stripped():
    """5厘米 不能被当成 5 判对；两侧原文一致才判对。"""
    q = SimpleNamespace(subject="数学", question_type="填空", answer="5厘米")
    result = judge(q, "5")
    assert not result.correct and result.needs_review
    assert judge(q, "5厘米").correct


# --- 拼写（AC3） ---------------------------------------------------------------


def test_two_vs_too_wrong_with_hint():
    result = check_spelling("two", "too")
    assert not result.correct
    assert result.hint and "two" in result.hint  # 编辑距离只做提示


def test_their_vs_there_wrong():
    result = check_spelling("their", "there")
    assert not result.correct
    assert result.hint


def test_spelling_exact_match_correct_and_distance_never_passes():
    assert check_spelling("apple", "Apple ").correct
    # 编辑距离 1 的近邻（aple）必须判错：距离只能给提示
    assert not check_spelling("apple", "aple").correct


def test_judge_routes_spelling():
    q = SimpleNamespace(subject="英语", question_type="单词", answer="two")
    result = judge(q, "too")
    assert not result.correct and result.hint and "two" in result.hint


def test_dictation_never_graded_by_pinyin():
    """听写按字面比对：平果 ≠ 苹果，拼音相同也不能判对。"""
    q = SimpleNamespace(subject="语文", question_type="听写", answer="苹果")
    assert judge(q, "苹果").correct
    assert not judge(q, "平果").correct


# --- 学科 × 题型路由（评审阻塞 2 的回归防线） ---------------------------------


def test_english_math_question_routes_to_math_not_spelling():
    """英语科目的计算题也是数学题：1/2 答 0.5 判对，走符号等价不进拼写词表。"""
    q = SimpleNamespace(subject="英语", question_type="计算", answer="1/2")
    assert judge(q, "0.5").correct
    assert not judge(q, "0.51").correct


def test_english_spelling_question_still_uses_word_list_with_hint():
    """英语拼写题仍走词表比对：futrue 判错且给 hint。"""
    q = SimpleNamespace(subject="英语", question_type="单词", answer="future")
    result = judge(q, "futrue")
    assert not result.correct and result.hint and "future" in result.hint


def test_chinese_counting_measure_word_stripped_symmetrically():
    """口语计数量词两端对称剥掉：八个 == 8；物理单位不动，仍转复核。"""
    assert judge(math_question("8"), "八个").correct
    assert parse_answer("3只").kind == "math" and parse_answer("3只").text == "3"
    assert parse_answer("5厘米").kind == "unit_math"  # 单位不剥，绝不静默丢
    assert judge(math_question("2小时"), "150分").needs_review  # 单位换算仍交人工


# --- 低置信度复核（AC4） -------------------------------------------------------


def test_low_confidence_needs_review():
    """真分不清：数学解析失败 → confidence < 0.8 → needs_review。"""
    result = judge(math_question("5"), "((")
    assert not result.correct
    assert result.confidence < 0.8
    assert result.needs_review


def test_unanswerable_question_needs_review():
    result = judge(SimpleNamespace(subject="数学", question_type="计算", answer=""), "27")
    assert result.needs_review and result.confidence < 0.8


def test_unit_mismatch_needs_review():
    result = judge(math_question("2小时"), "150分")
    assert result.needs_review


# --- 30 例批量 + 变异自证（AC6） ------------------------------------------------

# 本机口径（2026-10-31 实测）：裸字符串 2/30、裸 math-verify 17/30、
# 归一化外壳 29/30。末位 1/3 vs 0.33 是故意保留的失败例（四舍五入不等，
# 交家长），对应决策备忘 29/30 里那 1 例。
BATCH = [
    ("27", "27"), ("0.5", "0.5"), ("八", "8"), ("100万", "1000000"),
    ("3:4", "3∶4"), ("3:4", "6:8"), ("1/2", "0.5"), ("2+3", "5"),
    ("√4", "2"), ("−3", "-3"), ("２５", "25"), ("二十五", "25"),
    ("2,3", "2，3"), ("{2,3}", "{3, 2}"), ("5/10", "1/2"), ("0.25", "1/4"),
    ("3/4", "0.75"), (" 27 ", "27"), ("27。", "27"), ("十二", "12"),
    ("3.5万", "35000"), ("一万", "10000"), ("2/4", "0.5"), ("1/3+2/3", "1"),
    ("6÷2", "3"), ("4×5", "20"), ("八十一", "81"), ("一百零三", "103"),
    ("2.50", "2.5"), ("1/3", "0.33"),
]


def test_normalization_batch_shell_passes_29():
    """归一化外壳批量口径：29/30 判对。变异测试时此用例必须变红。"""
    correct = sum(
        judge(math_question(g), a).correct for g, a in BATCH
    )
    assert correct == 29, f"外壳批量口径期望 29/30，实际 {correct}/30"


def test_raw_math_verify_baseline_below_shell():
    """裸 math-verify（无外壳）显著低于外壳口径——变异的量化基线。"""
    pytest.importorskip("math_verify")
    from math_verify import parse, verify

    def raw(gold, ans):
        try:
            return bool(verify(parse(f"${gold}$"), parse(f"${ans}$")))
        except Exception:
            return False

    raw_hits = sum(raw(g, a) for g, a in BATCH)
    assert raw_hits <= 20, f"裸 math-verify 基线不应接近外壳口径（实际 {raw_hits}/30）"
