"""意图分流：这一句话该走哪条工具链，还是直连应答。

规则 = 关键词 + 调用方给的状态，**不用大模型做分类**（alpha 阶段不增加不确定性）：
同一个输入今天和明天必须走同一条路，误判要能靠改一行关键词修掉。

    route("给我出一道英语题")           -> Intent.ASK
    route("还剩几道")                   -> Intent.PROGRESS
    route("题库里有多少题")             -> Intent.BANK
    route("这道题对不对")               -> Intent.GRADE
    route("嗯")                         -> Intent.CHAT
    route("嗯", awaiting_answer=True)   -> Intent.GRADE

**关键词表是本文件的唯一真相**：`turn.py` 只按枚举值分流，不再自己认字。
一张表按顺序匹配，先命中先生效，所以「还剩几道」归进度而不是归要题。

「上一轮在等作答」时，没命中任何关键词的一句「嗯」按**判分**走：那多半是孩子对题的
回应，宁可明确说「判分还没接」，也不许静默换成一道新题。
"""

from __future__ import annotations

from enum import Enum


class Intent(Enum):
    """一轮的走向。

    故意**不**混入 ``str``：``Intent.CHAT != "chat"``，调用方只能按枚举比对，
    字面量魔法值在测试里会直接红掉。前四个各对应一条工具链，``CHAT`` 不走工具。
    """

    CHAT = "chat"
    ASK = "ask"
    GRADE = "grade"
    PROGRESS = "progress"
    BANK = "bank"


# 判分：对错只能由工具决定，所以这类话绝不能落到闲聊分支。
# M1 还没接判分链路，但**必须**落到 GRADE（明确说没接），不许换成一道新题。
GRADE_KEYWORDS = (
    "判",
    "对不对",
    "对吗",
    "对了没",
    "错没错",
    "答对",
    "答得对",
)

# 进度：数字只认工具返回，不许模型估。
PROGRESS_KEYWORDS = (
    "进度",
    "还剩",
    "剩几",
    "还差",
    "几道",
    "做了多少",
    "练了多少",
    "今天多少",
)

# 题库概况：总量与分科。
BANK_KEYWORDS = (
    "题库",
    "有多少题",
    "多少道",
    "一共几题",
    "几题",
)

# 要题：出题、换题、练一下。含「再来一个」——评审口径把它算作要题（口语里就是「再给一道」）。
ASK_KEYWORDS = (
    "出题",
    "出一道",
    "出个题",
    "来一道",
    "再来一道",
    "再来一个",
    "练一下",
    "下一题",
    "换一题",
    "换个题",
    "题目",
)

# 一张有序表：命中即返回。GRADE 最优先（判分绝不能变成出题），
# 「还剩几道」要先于「题库」里的数量词被看见，所以 PROGRESS 排在 BANK 前面。
INTENT_RULES: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (Intent.GRADE, GRADE_KEYWORDS),
    (Intent.PROGRESS, PROGRESS_KEYWORDS),
    (Intent.BANK, BANK_KEYWORDS),
    (Intent.ASK, ASK_KEYWORDS),
)

# 取题时要带的学科；认不出就不带，交给工具按到期顺序给。
SUBJECT_KEYWORDS = ("数学", "语文", "英语")


def route(text: str, *, awaiting_answer: bool = False) -> Intent:
    """一句话 → ``Intent``。

    *awaiting_answer* 是调用方手里的会话状态：上一轮问出去一道题、还没判分。
    这种状态下没认出关键词的话一律当答案（GRADE），不猜、不出题。
    """
    clean = (text or "").strip()
    for intent, keywords in INTENT_RULES:
        if any(keyword in clean for keyword in keywords):
            return intent
    if awaiting_answer:
        return Intent.GRADE
    return Intent.CHAT


def pick_subject(text: str) -> str | None:
    """话里点名了学科就带出去，没点名返回 ``None``（不要猜）。"""
    clean = (text or "").strip()
    for subject in SUBJECT_KEYWORDS:
        if subject in clean:
            return subject
    return None
