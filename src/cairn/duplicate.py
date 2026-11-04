"""Entry-time duplicate detection (0.2 卡6): "这题好像录过".

Pure functions only (no DB, no I/O) so every rule is unit-testable. The
service layer (``cairn.entry_service``) feeds candidates in; this module decides.

Three outcomes, matching the product口径 the Lead fixed:

- ``exact``   — same question after normalization → blocked, nothing written
- ``similar`` — highly similar (one number / punctuation / spacing off) →
                warned only, the parent can still add it
- no match    — nothing said, normal insert

Normalization (``normalize_text``) is one pass over NFKC + casefold:

1. NFKC: full-width → half-width (``１２３ＡＢＣ`` → ``123ABC``), compatibility
   forms folded (``①`` → ``1``)
2. casefold: English lower-cased
3. all whitespace dropped (spacing differences must not matter)
4. punctuation dropped (``，。、？！（）“”`` …)

Math operators (``+ - * / = < > ^ % × ÷``) are **kept**: ``-`` is Unicode
punctuation but ``3-5`` and ``35`` are different sums, and dropping it would
make them look like the same question.

Similarity is ``difflib.SequenceMatcher`` on the normalized **content** (the
stem the parent reads), no third-party dependency. ``SIMILARITY_THRESHOLD``
is the single knob: two short stems differing by one digit (``25×4=?`` vs
``26×4=?``) score 0.8 and are deliberately *not* flagged; longer word problems
with one number changed do flag.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher

__all__ = [
    "KIND_EXACT",
    "KIND_SIMILAR",
    "SIMILARITY_THRESHOLD",
    "Candidate",
    "DuplicateMatch",
    "describe_exact",
    "describe_preview_row",
    "describe_similar",
    "find_duplicate",
    "normalize_text",
    "question_key",
    "shorten",
    "similarity",
]

# 高于等于这个比值才算「高度相似」；只影响提示，不影响入库
SIMILARITY_THRESHOLD = 0.85

KIND_EXACT = "exact"
KIND_SIMILAR = "similar"

# 数学运算符不当标点去掉：3-5 和 35 不是一道题；小数点同理（3.5 ≠ 35）
_KEEP_SYMBOLS = frozenset("+-*/=<>^%~.×÷")

_PREVIEW_LIMIT = 24


@dataclass(frozen=True)
class Candidate:
    """One question already in the library (or an earlier row of this batch).

    ``question_id`` is None for a row of the paste being previewed — it has no
    id yet, and the message then points at the row number instead.
    """

    content: str
    answer: str
    question_id: int | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class DuplicateMatch:
    """What a new entry collided with."""

    kind: str  # KIND_EXACT | KIND_SIMILAR
    candidate: Candidate
    score: float  # 1.0 for exact

    @property
    def is_exact(self) -> bool:
        return self.kind == KIND_EXACT


def normalize_text(text: str | None) -> str:
    """Comparison form of one field: half-width, lower-case, no space/punctuation."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept: list[str] = []
    for char in folded:
        if char.isspace():
            continue
        if char in _KEEP_SYMBOLS:
            kept.append(char)
            continue
        if unicodedata.category(char).startswith("P"):
            continue
        kept.append(char)
    return "".join(kept)


def question_key(content: str, answer: str) -> str:
    """Exact-match key: the stem *and* the answer, both normalized.

    Both sides are needed — on maths two different sums can share one answer
    (``3+5=?`` and ``2+6=?`` both → 8), and matching on the answer alone would
    block a genuinely new question.
    """
    return f"{normalize_text(content)}\x1e{normalize_text(answer)}"


def similarity(left: str, right: str) -> float:
    """Ratio in [0, 1] between two texts (normalized here, so callers need not)."""
    a = normalize_text(left)
    b = normalize_text(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # autojunk=False: keep long stems honest (heuristic junk only kicks in >200
    # chars, but the flag makes behaviour explicit and independent of length)
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def find_duplicate(
    content: str, answer: str, candidates: list[Candidate] | tuple[Candidate, ...]
) -> DuplicateMatch | None:
    """Best collision for one entry, scanning *candidates* in order.

    Exact wins over similar regardless of order; among similar ones the highest
    score wins. Ties keep the earlier candidate (the older question is the more
    useful reference for the parent).
    """
    key = question_key(content, answer)
    best: DuplicateMatch | None = None
    for candidate in candidates:
        if question_key(candidate.content, candidate.answer) == key:
            return DuplicateMatch(KIND_EXACT, candidate, 1.0)
        score = similarity(content, candidate.content)
        if score < SIMILARITY_THRESHOLD:
            continue
        if best is None or score > best.score:
            best = DuplicateMatch(KIND_SIMILAR, candidate, score)
    return best


def shorten(text: str, limit: int = _PREVIEW_LIMIT) -> str:
    """One-line excerpt for messages (never a score, always the parent's words)."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _where(match: DuplicateMatch) -> str:
    """「第 12 条」 for a stored question; 「本批第 3 条」 is added by the caller."""
    candidate = match.candidate
    if candidate.question_id is None:
        return "本批里前面那条"
    return f"第 {candidate.question_id} 条"


def describe_exact(match: DuplicateMatch) -> str:
    """Blocked message: 这道题已经录过（第 12 条，录入于 10 月 31 日）."""
    candidate = match.candidate
    if candidate.question_id is None:
        return f"这道题已经录过（{_where(match)}）"
    stamp = _month_day(candidate.created_at)
    if stamp is None:
        return f"这道题已经录过（{_where(match)}）"
    return f"这道题已经录过（{_where(match)}，录入于 {stamp}）"


def describe_similar(match: DuplicateMatch) -> str:
    """Soft warning: names the question it resembles, offers no score."""
    candidate = match.candidate
    return (
        f"这道题和已录过的「{shorten(candidate.content)}」（{_where(match)}）几乎一样，"
        "确认是不同题再点「仍然添加」。"
    )


def describe_preview_row(match: DuplicateMatch, row_no: int, content: str) -> str:
    """Batch preview label: 第 3 条：25×4=?（已录过） / （疑似重复：…）."""
    head = f"第 {row_no} 条：{shorten(content)}"
    if match.is_exact:
        return f"{head}（已录过）"
    return f"{head}（疑似重复：{shorten(match.candidate.content)}）"


def _month_day(created_at: datetime | None) -> str | None:
    """Local-time 「10 月 31 日」.

    ``created_at`` is written as UTC, but SQLite drops tzinfo on the way back,
    so a naive value is read as UTC (not as local) before converting — otherwise
    an entry made after local midnight shows yesterday's date.
    """
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    local = created_at.astimezone()
    return f"{local.month} 月 {local.day} 日"
