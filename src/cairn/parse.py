"""Paste-a-whole-day's-words parser: raw text in, entry items out.

Pure functions only (no DB, no I/O) so every rule is unit-testable. Rules come
from the A three-colour card, in this order, per line:

1. drop blank lines
2. strip a leading item number: ``1.`` ``2、`` ``3)`` ``(4)`` ``（5）`` ``①``
3. strip ``Unit 3`` / ``Lesson 2`` / ``Module 1`` prefixes (repeatable:
   ``Unit 3 Lesson 2 apple 苹果``)
4. drop explanatory prose: a pure-Chinese line longer than 6 visible chars
   (punctuation not counted), or a line whose text after a colon reads as an
   explanation (long / ends with sentence punctuation / starts with a marker
   such as 意思是)
5. split a pair into ``content`` (the hint the child sees/hears: Chinese
   meaning or pinyin) and ``answer`` (the word to write: English word or
   Chinese characters). ``apple 苹果`` → content=苹果, answer=apple (en);
   ``苹果 píngguǒ`` → content=píngguǒ, answer=苹果 (zh). Whitespace, tab and a
   colon all work as separators; a colon label in front
   (``单词：apple 苹果``) uses the part after the colon
6. drop anything that yields no content+answer pair — a lone 苹果 has no answer,
   and keeping it would fail entry validation for the whole batch
7. within one paste, keep only the first occurrence of each answer (the word)

``lang`` describes the answer (the word to write): ``en`` for an English word,
``zh`` for Chinese characters. Pinyin is recognised by tone letters (píngguǒ)
or by being plain latin next to the Chinese word (zhuozi).

``audio_text`` (0.2 卡3) is the pronunciation cue read aloud on a dictation
card: an English word reads itself, a Chinese word reads its pinyin. It is the
card's *reading*, not necessarily its written answer. ``audio_cue`` is the only
place that rule lives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ParsedItem",
    "audio_cue",
    "detect_lang",
    "is_explanatory_sentence",
    "parse_line",
    "parse_pasted_text",
    "strip_line_prefixes",
]

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
# pinyin: latin letters, tone-marked vowels (U+00C0–U+01FF), spaces, apostrophes
_PINYIN_ONLY = re.compile(r"^[A-Za-z\u00c0-\u01ff\s'\-]+$")
# punctuation + whitespace, ignored when measuring a sentence's length
_NOISE = re.compile(r"[\s，。、；：？！,.;:?!\"'“”‘’（）()《》〈〉\[\]【】—\-…·~]")

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_NUMBER_PREFIXES = (
    re.compile(rf"^[{_CIRCLED}]\s*[.、)）:：]?\s*"),
    re.compile(r"^[(（]\s*\d{1,3}\s*[)）]\s*"),
    re.compile(r"^\d{1,3}\s*[.、)）]\s*"),
)
# a digit is required: "part 部分" is a word pair, not a chapter prefix
_UNIT_PREFIX = re.compile(r"^(?:unit|lesson|module|part)\s*\d{1,3}\s*[.、:：\-—]?\s*", re.I)
_COLON = re.compile(r"[:：]")
_WHITESPACE = re.compile(r"\s+")

# 纯中文超过这个字数（不含标点）就当说明性长句
_MAX_PLAIN_CHINESE = 6

_EXPLANATION_STARTS = ("意思是", "指的是", "表示", "即", "这是", "这里", "注意", "说明")


@dataclass(frozen=True)
class ParsedItem:
    """One pasted line turned into an entry item."""

    content: str
    answer: str
    lang: str  # "zh" | "en"
    audio_text: str | None = None  # dictation cue; None → nothing to read aloud


def strip_line_prefixes(line: str) -> str:
    """Remove leading item numbers and Unit/Lesson prefixes, in any order."""
    text = line.strip()
    while True:
        trimmed = text
        for pattern in _NUMBER_PREFIXES:
            trimmed = pattern.sub("", trimmed, count=1)
        trimmed = _UNIT_PREFIX.sub("", trimmed, count=1).strip()
        if trimmed == text:
            return text
        text = trimmed


def is_explanatory_sentence(line: str) -> bool:
    """True for pure-Chinese prose longer than 6 visible chars (not a word)."""
    if _LATIN.search(line):
        return False
    return len(_NOISE.sub("", line)) > _MAX_PLAIN_CHINESE


def parse_line(raw_line: str) -> ParsedItem | None:
    """Parse one line; return None when the line is noise or yields no pair."""
    text = strip_line_prefixes(raw_line)
    if not text or is_explanatory_sentence(text):
        return None

    colon = _COLON.search(text)
    pair: tuple[str, str] | None
    if colon:
        head = text[: colon.start()].strip()
        tail = text[colon.end() :].strip()
        if tail and _is_explanation(tail):
            return None
        pair = _split_pair(tail) if tail else None
        if pair is None:
            pair = _pair_from_sides(head, tail)
    else:
        pair = _split_pair(text)

    if pair is None:
        return None
    # unified rule: content is the hint, answer is what the learner writes.
    # _split_pair/_pair_from_sides return (word, hint), so swap the sides.
    content, answer = pair[1].strip(), pair[0].strip()
    if not content or not answer:
        return None
    lang = detect_lang(answer)
    return ParsedItem(
        content=content,
        answer=answer,
        lang=lang,
        audio_text=audio_cue(content, answer, lang),
    )


def parse_pasted_text(raw: str) -> list[ParsedItem]:
    """Parse a whole paste; blank/noise lines dropped, duplicates kept once."""
    items: list[ParsedItem] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        item = parse_line(line)
        if item is None:
            continue
        key = _dedupe_key(item.answer)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def _is_explanation(text: str) -> bool:
    """Text after a colon that reads as prose rather than a definition."""
    if is_explanatory_sentence(text):
        return True
    if text.endswith(("。", "！", "？", "!", "?")):
        return True
    return text.startswith(_EXPLANATION_STARTS)


def detect_lang(content: str) -> str:
    """Single source of truth for ``question.lang`` — shared by the paste
    parser and manual entry so both paths agree.

    ``en`` only for a clearly Latin word/phrase; ``zh`` for Chinese or for
    anything with neither script (digits/symbols → judged as ``zh`` by
    default, e.g. a maths stem).
    """
    if _CJK.search(content):
        return "zh"
    if _LATIN.search(content):
        return "en"
    return "zh"


def _looks_like_pinyin(text: str) -> bool:
    return (
        bool(_LATIN.search(text))
        and not _CJK.search(text)
        and bool(_PINYIN_ONLY.match(text))
    )


def audio_cue(content: str, answer: str | None, lang: str) -> str | None:
    """The pronunciation cue read aloud on a dictation card.

    Works for both shapes: ``en`` reads the English word (from ``content`` in
    the paste shape, or from ``answer`` when content is a Chinese hint); ``zh``
    reads the pinyin (from ``answer`` in the paste shape, or from ``content``
    when content is the pinyin hint). Anything else (a maths stem, a Chinese
    gloss) has no cue and the page shows no play button. This is the card's
    *reading*, not necessarily its written answer. Single source of truth for
    both the paste parser and manual entry.
    """
    if lang == "en":
        # old shape: content is the English word; new shape: content is the
        # Chinese hint and the word to say lives in answer.
        if content and _LATIN.search(content) and not _CJK.search(content):
            return content.strip() or None
        if answer and _LATIN.search(answer):
            return answer.strip() or None
        return None
    # zh: the reading is the pinyin, in answer (old shape) or content (new
    # shape, where content is the pinyin hint and answer is the character).
    if answer and _looks_like_pinyin(answer):
        return answer.strip()
    if content and _looks_like_pinyin(content):
        return content.strip()
    return None


def _split_pair(text: str) -> tuple[str, str] | None:
    """Split at the first whitespace that separates the two scripts."""
    for match in _WHITESPACE.finditer(text):
        left = text[: match.start()].strip()
        right = text[match.end() :].strip()
        if not left or not right:
            continue
        if _LATIN.search(left) and _CJK.match(right):
            return left, right
        if _CJK.search(left) and _looks_like_pinyin(right):
            return left, right
    return None


def _pair_from_sides(head: str, tail: str) -> tuple[str, str] | None:
    """Treat an explicit colon as the separator: ``apple：苹果``."""
    if not head or not tail:
        return None
    if _LATIN.search(head) and not _CJK.search(head) and _CJK.search(tail):
        return head, tail
    if _CJK.search(head) and _looks_like_pinyin(tail):
        return head, tail
    return None


def _dedupe_key(content: str) -> str:
    return _WHITESPACE.sub("", content).casefold()
