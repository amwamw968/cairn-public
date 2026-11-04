"""数学等价判定：优先 math-verify（Apache-2.0），不可用退化 sympy 并记日志。

比号不进符号等价（决策备忘第一坑：3:4 被当 3/4 或 0.75）——service 层在
ratio 类型上先走比判定，本模块只收已经归一化的 math 文本。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:  # 优先 math-verify
    from math_verify import parse as _mv_parse, verify as _mv_verify
except ImportError:  # pragma: no cover - 装了 math-verify 的环境走不到
    _mv_parse = None
    logger.warning("math_equiv: math-verify 不可用，将退化到 sympy 做符号等价")

_degraded_logged = False


def _mv_equivalent(gold: str, ans: str) -> bool | None:
    try:
        # parsing_timeout=None：math-verify 默认用 signal.alarm() 做超时，
        # 在非主线程（FastAPI 线程池、uvicorn worker）直接 ValueError。
        # 判题层输入都来自表单短串，解析不会挂死，自己不设超时。
        gold_parsed = _mv_parse(f"${gold}$", parsing_timeout=None)
        ans_parsed = _mv_parse(f"${ans}$", parsing_timeout=None)
        if not _usable(gold_parsed) or not _usable(ans_parsed):
            return None
        return bool(_mv_verify(gold_parsed, ans_parsed, timeout_seconds=None))
    except Exception:
        return None


def _usable(parsed) -> bool:
    """math-verify 对垃圾串会退回原串（字符串列表），等于没解析出数学对象。"""
    return bool(parsed) and not all(isinstance(item, str) for item in parsed)


def _sympy_equivalent(gold: str, ans: str) -> bool | None:
    global _degraded_logged
    try:
        import sympy
    except ImportError:
        logger.error("math_equiv: math-verify 与 sympy 均不可用，无法做符号等价")
        return None
    if not _degraded_logged:
        logger.warning("math_equiv: 退化到 sympy 做符号等价（math-verify 不可用）")
        _degraded_logged = True

    def to_expr(text: str):
        text = text.replace("\\sqrt{", "sqrt(").replace("}", ")")
        text = text.replace("√", "sqrt")
        return sympy.sympify(text, rational=True, convert_xor=True)

    try:
        diff = to_expr(gold) - to_expr(ans)
        if sympy.simplify(diff) == 0:
            return True
        return bool(abs(float(diff.evalf())) < 1e-9)
    except Exception:
        return None


def is_equivalent(gold: str, ans: str) -> bool | None:
    """两边都是归一化后的 math 文本；解析不了返回 None（交复核，不硬判）。"""
    if _mv_parse is not None:
        return _mv_equivalent(gold, ans)
    return _sympy_equivalent(gold, ans)


def ratio_equivalent(gold_items, ans_items) -> bool:
    """比等价：3:4 == 6:8（交叉相乘），明确不当商。"""
    try:
        a1, b1 = float(gold_items[0].text), float(gold_items[1].text)
        a2, b2 = float(ans_items[0].text), float(ans_items[1].text)
    except (IndexError, ValueError, ZeroDivisionError):
        return False
    return a1 * b2 == a2 * b1
