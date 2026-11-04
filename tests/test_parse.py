"""0.2 卡2 AC1/AC2 — pasted-text parser (pure functions, no DB)."""

from __future__ import annotations

import pytest

from cairn.parse import (
    ParsedItem,
    audio_cue,
    detect_lang,
    is_explanatory_sentence,
    parse_line,
    parse_pasted_text,
    strip_line_prefixes,
)

# 10 条有效词 + 2 条说明性噪声 = 12 行
PASTE = """\
1. apple 苹果
2、banana 香蕉
(3) orange 橙子
④ grape 葡萄
Unit 3 pear 梨
Lesson 2 peach 桃子
单词：watermelon 西瓜
苹果 píngguǒ
香蕉 xiāngjiāo
桌子 zhuōzi
今天老师讲的这些都是本单元必须掌握的单词，回家要好好复习。
注意：这一行也是说明文字，不该入库。
"""


# ---- AC1 噪声被丢：12 行进，10 条出 ----

def test_noise_lines_are_dropped():
    items = parse_pasted_text(PASTE)
    assert len(items) == 10
    contents = [i.content for i in items]
    assert contents == [
        "苹果",
        "香蕉",
        "橙子",
        "葡萄",
        "梨",
        "桃子",
        "西瓜",
        "píngguǒ",
        "xiāngjiāo",
        "zhuōzi",
    ]
    # 两条长句一条也没进来
    assert all("老师" not in c and "说明" not in c for c in contents)


def test_lang_is_detected_per_item():
    items = {i.answer: i for i in parse_pasted_text(PASTE)}
    assert items["apple"] == ParsedItem(
        content="苹果", answer="apple", lang="en", audio_text="apple"
    )
    assert items["watermelon"].content == "西瓜"
    assert items["watermelon"].lang == "en"
    assert items["苹果"] == ParsedItem(
        content="píngguǒ", answer="苹果", lang="zh", audio_text="píngguǒ"
    )
    assert items["桌子"].lang == "zh"


# ---- AC2 行首编号 / Unit 前缀 ----

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1. apple 苹果", "apple 苹果"),
        ("2、banana 香蕉", "banana 香蕉"),
        ("3) orange 橙子", "orange 橙子"),
        ("(4) grape 葡萄", "grape 葡萄"),
        ("（5）pear 梨", "pear 梨"),
        ("① apple 苹果", "apple 苹果"),
        ("Unit 3 pear 梨", "pear 梨"),
        ("lesson 2: peach 桃子", "peach 桃子"),
        ("Unit 3 Lesson 2 apple 苹果", "apple 苹果"),
        ("  7. 桌子 zhuōzi  ", "桌子 zhuōzi"),
    ],
)
def test_strip_line_prefixes(raw, expected):
    assert strip_line_prefixes(raw) == expected


# ---- AC2 中英 / 中拼切分 ----

@pytest.mark.parametrize(
    "raw, content, answer, lang, audio",
    [
        ("apple 苹果", "苹果", "apple", "en", "apple"),
        ("look after 照顾", "照顾", "look after", "en", "look after"),
        ("pear\t梨", "梨", "pear", "en", "pear"),
        ("苹果 píngguǒ", "píngguǒ", "苹果", "zh", "píngguǒ"),
        ("桌子 zhuōzi", "zhuōzi", "桌子", "zh", "zhuōzi"),
        ("香蕉 xiangjiao", "xiangjiao", "香蕉", "zh", "xiangjiao"),
        ("单词：watermelon 西瓜", "西瓜", "watermelon", "en", "watermelon"),
        ("apple：苹果", "苹果", "apple", "en", "apple"),
    ],
)
def test_parse_line_pairs(raw, content, answer, lang, audio):
    assert parse_line(raw) == ParsedItem(
        content=content, answer=answer, lang=lang, audio_text=audio
    )


# ---- AC2 说明性长句 / 冒号解释 / 空行 / 无词对 ----

@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "今天老师讲的这些都是本单元必须掌握的单词回家要好好复习",
        "说明：这些词明天听写都要考，请大家认真复习。",
        "注意：这里的词都要会写",
        "苹果",  # 只有词没有释义，凑不成题
    ],
)
def test_noise_lines_parse_to_none(raw):
    assert parse_line(raw) is None


def test_explanatory_sentence_rule():
    # 纯中文 6 字以内不算长句；超过 6 字（不含标点）算
    assert not is_explanatory_sentence("重点词语")
    assert is_explanatory_sentence("今天的重点是这些词语")
    # 带英文的不是纯中文，交给词对切分去处理
    assert not is_explanatory_sentence("apple 苹果是一个单词的示例")


# ---- AC2 批内去重 ----

def test_same_word_kept_once_per_paste():
    items = parse_pasted_text("apple 苹果\n1. apple 苹果\nAPPLE 苹果\npear 梨")
    # 按 answer（要写的词）去重：apple 的三种写法合一，梨 独立
    assert [i.answer for i in items] == ["apple", "pear"]
    assert [i.content for i in items] == ["苹果", "梨"]


# ---- AC11 语言判定唯一入口 detect_lang（单条/批量/回填共用） ----

@pytest.mark.parametrize(
    "content, expected",
    [
        ("apple", "en"),
        ("look after", "en"),
        ("苹果", "zh"),
        ("桌子", "zh"),
        ("3 + 5 = ?", "zh"),  # 无中文也无拉丁：判断不了，默认 zh
    ],
)
def test_detect_lang(content, expected):
    assert detect_lang(content) == expected


def test_blank_lines_do_not_create_items():
    assert parse_pasted_text("\n\n \n\n") == []


# ---- 0.2 卡3 AC7：朗读 cue（只念 cue，不念答案） ----

@pytest.mark.parametrize(
    "content, answer, lang, expected",
    [
        ("apple", "苹果", "en", "apple"),  # 英文词念单词本身
        ("look after", "照顾", "en", "look after"),
        ("桌子", "zhuōzi", "zh", "zhuōzi"),  # 中文词念拼音
        ("香蕉", "xiangjiao", "zh", "xiangjiao"),  # 无声调拼音也认
        ("chūn tiān", "春天", "zh", "chūn tiān"),  # 新口径：content 是拼音提示
        ("苹果", "apple", "en", "apple"),  # 新口径：content 是中文提示、answer 是英文
        ("桌子", "书桌", "zh", None),  # 释义是中文，不是拼音 → 没有 cue
        ("3 + 5 = ?", "8", "zh", None),  # 数学题：不朗读
        ("三加五等于多少？", "八", "zh", None),
    ],
)
def test_audio_cue_rules(content, answer, lang, expected):
    assert audio_cue(content, answer, lang) == expected


def test_pasted_chinese_line_puts_pinyin_into_audio_text():
    """中文+拼音行：拼音落 content（提示）与 audio_text，汉字落 answer（要写的）。"""
    item = parse_line("苹果 píngguǒ")
    assert (item.content, item.answer, item.audio_text) == ("píngguǒ", "苹果", "píngguǒ")
    assert item.audio_text != item.answer  # 念的是拼音，不是要写的汉字
