"""分流规则：每个意图各一句日常说法、闲聊五句、状态强制走判分、返回枚举不是字符串。

分流是「规则 + 状态」，不调大模型：这些用例就是规则的护栏，改关键词表必须能让
它们仍然成立；误判要能通过增删一条关键词修掉，而不是靠调 prompt。

**意图必须分细**：四个工具链各一个枚举值。只输出一个笼统的 ``practice`` 会让
「还剩几道」「题库多少题」都走成出题——静默答错比报错更糟。
"""

from __future__ import annotations

import pytest

from cairn.voice.router import Intent, pick_subject, route

# 四类要走工具链的说法，每类两句：一句标准问法、一句口语变体。
INTENT_CASES = (
    ("给我出一道英语题", Intent.ASK),
    ("再来一个", Intent.ASK),
    ("练一下", Intent.ASK),
    ("下一题", Intent.ASK),
    ("还剩几道", Intent.PROGRESS),
    ("今天多少题", Intent.PROGRESS),
    ("题库里有多少题", Intent.BANK),
    ("一共有几题", Intent.BANK),
    ("这道题对不对", Intent.GRADE),
    ("我答得对吗", Intent.GRADE),
)

# 闲聊：确认、道谢、情绪。都不含任何工具链关键词。
CHAT_CASES = (
    "嗯",
    "知道了",
    "谢谢你",
    "今天好累",
)


@pytest.mark.parametrize("text,expected", INTENT_CASES)
def test_intent_positive(text: str, expected: Intent) -> None:
    assert route(text) is expected


@pytest.mark.parametrize("text", CHAT_CASES)
def test_chat_negative(text: str) -> None:
    assert route(text) is Intent.CHAT


def test_quantity_words_keep_their_own_intent() -> None:
    """同一句话里「还剩」和「几道」都在时归进度，不能被题库的数量词抢走。"""
    assert route("题库里还剩几道") is Intent.PROGRESS
    assert route("还剩几道题") is Intent.PROGRESS


def test_intent_is_enum_not_string() -> None:
    """枚举值不能退化成字符串魔法值：`Intent.CHAT != "chat"`。"""
    intent = route("嗯")
    assert isinstance(intent, Intent)
    assert not isinstance(intent, str)
    assert intent != "chat"


@pytest.mark.parametrize("text", ("嗯", "知道了", "今天好累", ""))
def test_awaiting_answer_without_keyword_is_grade(text: str) -> None:
    """上一轮在等作答：没认出关键词的话当答案处理（判分），绝不换成新题。"""
    assert route(text, awaiting_answer=True) is Intent.GRADE


def test_awaiting_answer_still_honours_explicit_keyword() -> None:
    """孩子明确说「还剩几道」时仍按进度走：状态不覆盖说出口的词。"""
    assert route("还剩几道", awaiting_answer=True) is Intent.PROGRESS


@pytest.mark.parametrize("text", ("", "   ", "\n"))
def test_blank_text_is_chat(text: str) -> None:
    assert route(text) is Intent.CHAT


def test_pick_subject_said_out_loud() -> None:
    assert pick_subject("给我出一道英语题") == "英语"
    assert pick_subject("来一道语文题") == "语文"
    assert pick_subject("再出个数学的") == "数学"


def test_pick_subject_none_when_not_said() -> None:
    """没点名学科就不带，交给工具按到期顺序给——不猜。"""
    assert pick_subject("下一题") is None
    assert pick_subject("") is None
