"""答案归一化：把孩子的答案与标准答案拉到同一口径。

修补决策备忘实测的五个静默误判：

1. ``3:4`` 被解析成 3        → 比号单独成类（ratio），不进符号等价当商
2. ``100万`` 被解析成 100    → 中文数字/万倍换算成完整数值
3. ``"2,3"`` 被解析成 2.3    → 逗号/顿号分隔且两侧都是完整数值 → 多个答案
4. ``{2,3}`` 判成空          → 集合单独成类
5. ``−3``（全角减号）、中文数字解析为空 → 先做全角/异体符号映射，再做数值换算

parse_answer 返回结构化结果：raw 原串用于展示，text 是可比对口径，kind 决定
service 层走哪条判定路。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# NFKC 盖不住的异体符号：全角减号（U+2212）、比号（U+2236）、破折号当减号。
# × ÷ 也在 NFKC 下保持原样，映射成 * / 才能进符号等价。
_EXTRA_MAP = str.maketrans({
    "−": "-",
    "–": "-",
    "—": "-",
    "∶": ":",
    "×": "*",
    "÷": "/",
    "／": "/",
})

# 句末标点（小数点单独处理，见 _strip_tail）。
_TAIL_PUNCT = "。．!?！？；;，,、…~～·"

_MATHISH = re.compile(r"[\d√+\-*/^=()\\]")
_RATIO = re.compile(r"^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$")
_SET = re.compile(r"^\{(.*)\}$")
_NUM = re.compile(r"^\d+(?:\.\d+)?$")
_THOUSANDS = re.compile(r"^\d{1,3}(,\d{3})+$")  # 1,000 千分位，不当并列答案
# 中文数字串 / 数字+万倍：整串都是数字材料才换算，"三好学生" 这类不碰。
_CN_MIX = re.compile(r"^([\d.]+|[零一二两三四五六七八九十百千]+)([万亿]?)$")
_CN_CHARS = "零一二两三四五六七八九十百千"
_SEPARATORS = re.compile(r"[、,]")
_SQRT = re.compile(r"√\(?(\d+(?:\.\d+)?)\)?")
_CJK = re.compile(r"[\u4e00-\u9fff]")

# 纯计数量词：口语答案「八个」「3只」剥掉尾巴就是数值。对称作用于标准答案与
# 孩子答案。物理单位（米/小时/分/克/元…）刻意不在表里——5厘米 ≠ 5，
# 单位换算没法确定就转人工复核，绝不静默剥单位。
_COUNT_MEASURES = "个只支本条块岁人天页颗朵件张棵匹头双套遍次"
_COUNT_TAIL = re.compile(
    r"^([\d.]+|[零一二两三四五六七八九十百千]+(?:点[零一二三四五六七八九]+)?)"
    rf"[{_COUNT_MEASURES}]$"
)


@dataclass(frozen=True)
class ParsedAnswer:
    """归一化后的答案：raw 留展示，text 是可比对口径，items 只给 ratio/set/multi。"""

    raw: str
    kind: str  # empty / ratio / set / multi / math / word
    text: str
    items: tuple["ParsedAnswer", ...] = ()


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn2num(s: str) -> int:
    """中文数字 → 整数（十/百/千 + 万/亿分节，支持 零/两/二十/一百零三）。"""
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (num or 1) * _CN_UNITS[ch]
            num = 0
        elif ch == "万":
            total = (total + section + num) * 10_000
            section = num = 0
        elif ch == "亿":
            total = (total + section + num) * 100_000_000
            section = num = 0
        else:
            raise ValueError(f"非中文数字字符: {ch!r}")
    return total + section + num


def _fmt_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(value)


def _halfwidth(s: str) -> str:
    return unicodedata.normalize("NFKC", s).translate(_EXTRA_MAP)


def _strip_tail(s: str) -> str:
    """去首尾引号与句末标点；小数点只在不在两数字之间时才算句末（3. → 3，0.5 不动）。"""
    s = s.strip().strip("“”\"'`「」『』").strip()
    while s and s[-1] in _TAIL_PUNCT:
        s = s[:-1].rstrip()
    if s.endswith(".") and not re.search(r"\d\.\d$", s):
        s = s[:-1].rstrip()
    return s.strip()


def _squash(s: str) -> str:
    """数学串去掉全部空白；文字串压成单空格（英语短语保留分词）。"""
    if _MATHISH.search(s):
        return re.sub(r"\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_answer(raw: str | None) -> ParsedAnswer:
    text = _squash(_strip_tail(_halfwidth(raw or "")))
    if not text:
        return ParsedAnswer(raw or "", "empty", "")

    tail = _COUNT_TAIL.match(text)
    if tail:  # 口语计数量词（八个/3只）：剥掉尾巴当数值，两端对称
        text = tail.group(1)

    if _THOUSANDS.match(text):  # 1,000 千分位，先于并列答案识别
        text = text.replace(",", "")

    ratio = _RATIO.match(text)
    if ratio:
        return ParsedAnswer(raw or "", "ratio", text,
                            (ParsedAnswer(raw or "", "math", ratio.group(1)),
                             ParsedAnswer(raw or "", "math", ratio.group(2))))

    set_match = _SET.match(text)
    if set_match:
        inner = set_match.group(1)
        parts = [p.strip() for p in _SEPARATORS.split(inner) if p.strip()] if inner else []
        return ParsedAnswer(raw or "", "set", text,
                            tuple(parse_answer(p) for p in parts))

    # 中文数字 / 万倍（100万、十万、二十五、3.5万）
    cn = _CN_MIX.match(text)
    if cn and any(c in text for c in _CN_CHARS + "万亿"):
        base_part, suffix = cn.groups()
        try:
            if set(base_part) <= set("0123456789."):
                base = float(base_part)
            else:
                base = _cn2num(base_part)
            value = base * {"": 1, "万": 10_000, "亿": 100_000_000}[suffix]
            return ParsedAnswer(raw or "", "math", _fmt_number(value))
        except (ValueError, KeyError):
            pass  # 换算不了就落回 word，不硬判

    if _SEPARATORS.search(text):
        parts = [p for p in _SEPARATORS.split(text) if p]
        if len(parts) > 1 and all(_NUM.match(p) for p in parts):
            return ParsedAnswer(raw or "", "multi", text,
                                tuple(ParsedAnswer(raw or "", "math", p) for p in parts))

    if "√" in text:
        text = _SQRT.sub(r"\\sqrt{\1}", text)

    if _MATHISH.search(text) or text.startswith("\\"):
        # 数学串里还留着中文（"5米"、"5厘米"）：单位没法确定换算，单独立类
        # 交复核，绝不静默丢单位（否则 5厘米 会被当成 5 判对）。
        if _CJK.search(text):
            return ParsedAnswer(raw or "", "unit_math", text)
        return ParsedAnswer(raw or "", "math", text)
    return ParsedAnswer(raw or "", "word", text)


def normalize_text(raw: str | None) -> str:
    """纯文本口径：全角→半角、去句末标点、压空白。拼写/文字比对用。"""
    return _squash(_strip_tail(_halfwidth(raw or ""))).casefold()
