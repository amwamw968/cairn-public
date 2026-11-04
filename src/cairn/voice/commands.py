"""短指令识别：孩子打断后说的「停一下」「跳过」「给个提示」不进闲聊。

真机实测（2026-10-31）：孩子打断时喊「停一下停一下」，ASR 认成「请小声一下。」，
被路由成 chat，助手回了句「嗯，我在。」——打断之后的短指令全被当闲聊敷衍掉了。

这一层在 ``router.route`` **之前**跑：命中就直接执行对应动作，不命中才走原来的
路由。匹配是**意图级**的，不是「任意包含」——「包含」会把「我不知道今天天气」
劫持成换题（闲聊被误伤，2026-10-31 评审实测复现）：

- **前缀匹配**：清洗后的整句以动作性短语**开头** → 命中（整句就是在下指令，
  后面偶尔拖个尾巴也认，如「我不会这道题」「跳过吧」）；
- **整句等于**：不以动作短语开头的词（「不知道」「怎么说」）只有整句就是它才
  命中——「我不会」= 换题，「我不知道今天天气」= 闲聊，不被劫持。

**词表是本文件的唯一真相**，误识别就改一行词表修掉——与 ``router.py`` 同一套哲学。
"""

from __future__ import annotations

import difflib
import os
import re
from enum import Enum
from collections.abc import Callable

# 总开关：``CAIRN_VOICE_INTENTS=0`` 时整层关闭，全部走原路由。
INTENTS_ENV = "CAIRN_VOICE_INTENTS"


class VoiceCommand(Enum):
    """短指令的四类动作；值只用于日志，不进协议。"""

    STOP = "stop"
    SKIP = "skip"
    HINT = "hint"
    CONTINUE = "continue"


# 停：动作性短语，作**前缀**命中。不放单词「停」——ASR 把「挺难」认成「停难」
# 这类错字会把闲聊误判成停止，多字词已够覆盖口语。
# 词表是**清洗后**的形态：句尾语气词剥掉后「别说了」→「别说」。
STOP_KEYWORDS = (
    "停一下",
    "停下来",
    "停一停",
    "停下",
    "别说",
    "安静",
    "先停",
)

# 跳过：动作短语（前缀命中）+「不会 / 不知道」（只整句等于命中）——
# 孩子说不出答案就是要换题，卡在原题没有意义；但「我不知道今天天气」
# 是闲聊，「不知道」只是句中一部分，不许被劫持成换题。
SKIP_PREFIX_KEYWORDS = (
    "跳过",
    "下一题",
    "换一题",
    "我不会",
)
SKIP_EXACT_KEYWORDS = (
    "不会",
    "不知道",
    "我不知道",
)

# 提示：只念当前题的 content（提示语）；PracticeItem 结构上没有 answer。
# 收敛回卡片列举的四种：「怎么办 / 怎么答」会误伤正常提问，评审已删。
HINT_PREFIX_KEYWORDS = (
    "提示",
    "给我点提示",
    "给我个提示",
)
HINT_EXACT_KEYWORDS = (
    "怎么说",
)

# 继续：按当前题状态接着来——还没答就再念一次提示，已答就出下一题。
# 「我会了 / 知道了 / 懂了」剥掉句尾语气词后的清洗形态。
CONTINUE_PREFIX_KEYWORDS = (
    "继续",
)
CONTINUE_EXACT_KEYWORDS = (
    "我会",
    "知道",
    "我知道",
    "懂",
)

# 一张有序表：命中即返回，先到先得。每类 = (前缀词表, 整句等于词表)。
COMMAND_MATCHERS: tuple[tuple[VoiceCommand, tuple[str, ...], tuple[str, ...]], ...] = (
    (VoiceCommand.STOP, STOP_KEYWORDS, ()),
    (VoiceCommand.SKIP, SKIP_PREFIX_KEYWORDS, SKIP_EXACT_KEYWORDS),
    (VoiceCommand.HINT, HINT_PREFIX_KEYWORDS, HINT_EXACT_KEYWORDS),
    (VoiceCommand.CONTINUE, CONTINUE_PREFIX_KEYWORDS, CONTINUE_EXACT_KEYWORDS),
)

# 只在短句上认指令：超过这个长度的是正常说话（复述题干、答题），
# 里头偶然带个「不会」「安静」不该被劫持。
MAX_COMMAND_CHARS = 20

# 模糊匹配（对付 ASR 近音字，真机实测「停一下」被认成「听一下」）：
# 只在清洗后 ≤ 6 字的短句上启用，与关键词逐条算相似度，≥ 0.62 判命中。
# 长句绝不模糊——「我今天要不要听一下音乐」不许被听成指令。
FUZZY_MAX_CHARS = 6
FUZZY_THRESHOLD = 0.62

# ASR 常见的标点与空白：匹配前剥掉，免得「停一下。」里的句号捣乱。
_NOISE = re.compile(r"[\s，。！？、,.!?:：;；~～…]")

# 句首尾的语气词：剥掉再匹配，「停一下吧」「嗯」不再捣乱；只剥句首尾，
# 词表得以用清洗后的形态（「我会了」→「我会」）整句对上。
_FILLER_CHARS = "啊呀吧了嗯哦"

# 句首的客气话：剥掉，「请先停下」→「先停下」仍命中停；
# 「请小声一下」剥完是「小声一下」，不在任何词表里，不会误命中。
_LEADING_POLITE = "请"


def _clean(text: str) -> str:
    """去标点空白 → 去句首尾语气词 → 去句首客气话，得到可比对的整句。"""
    clean = _NOISE.sub("", text or "").strip()
    clean = clean.strip(_FILLER_CHARS)
    return clean.lstrip(_LEADING_POLITE)


def intents_enabled(environ: dict[str, str] | None = None) -> bool:
    """读 ``CAIRN_VOICE_INTENTS``：只有 ``0`` 是关，缺省与其他值都开。"""
    env = os.environ if environ is None else environ
    return env.get(INTENTS_ENV, "") != "0"


def match_command(
    text: str, *, log: Callable[[str], None] | None = None
) -> VoiceCommand | None:
    """一句话 → 短指令；没命中返回 ``None``（调用方走原路由）。

    意图级匹配：整句以动作性短语**开头**，或**等于**某个关键词，才算命中；
    关键词只是句中一部分（「我不知道今天天气」）不算。精确匹配不中时，短句
    （≤ ``FUZZY_MAX_CHARS`` 字）再走一轮模糊匹配，对付 ASR 近音字。
    """
    clean = _clean(text)
    if not clean or len(clean) > MAX_COMMAND_CHARS:
        return None
    for command, prefixes, exact in COMMAND_MATCHERS:
        if clean in exact or clean.startswith(prefixes):
            return command
    return _fuzzy_match(clean, log)


def _fuzzy_match(clean: str, log: Callable[[str], None] | None) -> VoiceCommand | None:
    """短句近音字兜底：只对**动作短语表**逐条算 ``SequenceMatcher`` 相似度。

    整句等于表（「不会」「怎么说」这类）不进模糊——「怎么办」≈「怎么说」0.667
    会把正常提问劫持成指令（评审特意从 HINT 词表删过，不能再从模糊匹配溜回来）。
    """
    if len(clean) > FUZZY_MAX_CHARS:
        return None
    for command, prefixes, _exact in COMMAND_MATCHERS:
        for keyword in prefixes:
            ratio = difflib.SequenceMatcher(None, clean, keyword).ratio()
            if ratio >= FUZZY_THRESHOLD:
                if log is not None:
                    log(f"[cmd] 模糊命中 {clean}≈{keyword} ({ratio:.2f})")
                return command
    return None
