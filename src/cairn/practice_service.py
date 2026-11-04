"""Practice orchestration: fetch a batch (answers hidden, types interleaved),
then reveal after the learner answers.

fetch_practice pool = new entries (review_status=new, created_at asc)
                    + due items (next_due_at <= now, asc)
The whole pool is reordered by a greedy interleave (same question_type never
appears on consecutive positions whenever the type counts make it feasible;
when infeasible it produces the theoretical minimum number of adjacent
same-type pairs, max - rest - 1), and only then truncated to *limit*. The
order is deliberately interleave-then-truncate: truncating first lets a large
single-type prefix eat the whole batch and the interleave never gets to run.

Interleaving groups by question_type and keeps each group in pool order, so
within one type the new-first / earliest-due-first priority is unchanged.
Across types the batch may draw both new and due items — that is the point of
interleaving once the pool is larger than the batch.

fetch_practice never puts answer/analysis on PracticeItem (structurally
absent). It marks dictation cards with is_dictation: the question carries a cue
(audio_text) and its content is not its own answer. On such a card the ask page
shows content as the prompt and plays the cue aloud, and the answer is never
rendered as text — the learner hears it, does not read it. (When the cue string
happens to equal the answer, that same string still reaches the page as the
speech argument; it is the dictated form, not a rendered answer.) A card whose
content equals its answer (legacy rows, maths) is not a dictation card: it
shows the plain question face and no play button. submit_answer reveals
answer/analysis after the fact; it does not grade, store the learner's answer,
or touch REVIEW/FSRS state (parent grades via apply_review). submit_and_judge
is the grading path: deterministic judge (grading layer, no LLM) on
submission, then good/again review via apply_review, or into the pending
review queue when confidence < 0.8.

Empty standard answer is its own branch: the judge has nothing to compare
against, and the pending queue is not the place for it (that queue means "this
entry looks wrong", its buttons confirm/reject and never rate). So
submit_and_judge returns ManualGrade(needs_manual=True) and writes nothing —
no review, no confidence change, no scheduling. The reveal page then asks the
parent to look at the scanned original and presses 做对了/没做对, which lands
good/again through apply_review.

Batch composition (BUKA 配额, docs/BUKA-LESSONS.md 一.2/一.3): the pool is
still "who is due" — FSRS owns next_due_at and answered-but-not-due items
stay out — but *which* candidates fill the batch is quota-driven:

    green  = review_status == mastered            (cap: green_ratio of limit)
    red    = unmastered with a difficulty signal  (red_ratio of the rest)
    yellow = unmastered, single wrong, no hard rating, no lapse

Green is a hard cap: at most max(1, limit * CAIRN_GREEN_RATIO) slots (0 when
the ratio is 0). Only a true shortage — red+yellow pool smaller than limit —
lets green fill the leftover. When just one side is scarce (say red has fewer
than its share but yellow has plenty) the richer side absorbs the leftover;
green never grows past its cap that way. Red takes CAIRN_RED_RATIO of the
remaining slots, yellow fills the rest, and unpicked red backfills when yellow
runs dry, so the batch is full whenever the pool is.

zh/en balance is done once on the final batch, not per quota group: the batch
is swapped until zh and en differ by at most 1 (same-status swaps first to
keep composition, cross-status second, green additions still bounded by the
cap). It gives up only when the short language is exhausted in the pool or
every remaining swap would break the green cap — genuinely not enough, not
the algorithm skipping work. lang-less items (maths) never join the balance.

Difficulty score = wrong_count*2 + uncertain_count*1 + lapse_count*3, with
uncertain_count = number of "hard" ratings in the REVIEW ledger and
lapse_count derived from restored_at (a restored item fell back out of the
mastered box at least once; counting multiple restores exactly would need
a persisted counter — deliberately not done, no migration). The score only
reorders candidates *within the same due timestamp* (descending); it never
changes due dates or the scheduling algorithm.

Config (env-overridable, printed at web startup):
    CAIRN_BATCH_SIZE   default batch limit, 20
    CAIRN_GREEN_RATIO  green share cap,     0.2
    CAIRN_RED_RATIO    red share of rest,   0.8
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.entry_service import ValidationError
from cairn.grading.service import GradeResult, judge
from cairn.models import (
    ErrorItem,
    Question,
    REVIEW_STATUS_ARCHIVED,
    REVIEW_STATUS_MASTERED,
    REVIEW_STATUS_NEW,
)
from cairn.models.review import RATING_AGAIN, RATING_GOOD, RATING_HARD, Review
from cairn.models.scan import Scan, ScanQuestion
from cairn.review_service import apply_review

# BUKA 配额参数（docs/BUKA-LESSONS.md 一.2）：环境变量可覆盖，启动时打印生效值。
ENV_BATCH_SIZE = "CAIRN_BATCH_SIZE"
ENV_GREEN_RATIO = "CAIRN_GREEN_RATIO"
ENV_RED_RATIO = "CAIRN_RED_RATIO"


def batch_config() -> tuple[int, float, float]:
    """(batch_size, green_ratio, red_ratio) — env-overridable practice quota."""
    size = int(os.environ.get(ENV_BATCH_SIZE, "20"))
    green = float(os.environ.get(ENV_GREEN_RATIO, "0.2"))
    red = float(os.environ.get(ENV_RED_RATIO, "0.8"))
    return size, green, red


def difficulty_score(wrong_count: int, uncertain_count: int, lapse_count: int) -> int:
    """错×2 + 不熟（hard 评分）×1 + 掉绿×3（BUKA 一.3）。"""
    return wrong_count * 2 + uncertain_count + lapse_count * 3


@dataclass(frozen=True)
class PracticeItem:
    """Trimmed view of one practice candidate — no answer, no analysis.

    audio_text/lang are the dictation cue (what the page reads aloud).
    is_dictation marks a card that hides its answer as text and plays the cue
    instead; it is structural (has a cue, content is not its own answer), never
    a copy of the answer. Adding an answer field is still not allowed.

    crop_url is the scanned-paper crop of this question (``/questions/{id}/crop``)
    and is None for hand-entered questions. It is an image URL, never image
    bytes: the page must not re-run OCR or cropping, and a missing file is the
    image endpoint's 404, not this layer's problem.
    """

    error_item_id: int
    question_id: int
    subject: str
    question_type: str
    content: str
    audio_text: str | None
    lang: str | None
    is_dictation: bool
    crop_url: str | None = None
    tutor_enabled: bool = False
    recap_enabled: bool = False


@dataclass(frozen=True)
class ManualGrade:
    """空标准答案的判定结果：不判对错，也不写任何簿记（练习揭示页两键用）。

    与 `GradeResult` 同形，调用方（网页 / MCP）不必分支取字段；多出的
    `needs_manual=True` 说明「这次没自动判，交家长看原图」。它不是 judge 层
    的产物——judge 那层一行不动，本层在调用它之前就分出来了。
    """

    correct: bool = False
    confidence: float = 0.0  # 没有判定，不是「判定成 0 分」
    reason: str = "这道题还没有标准答案"
    hint: str | None = None
    needs_review: bool = False
    needs_manual: bool = True


@dataclass(frozen=True)
class RevealedAnswer:
    error_item_id: int
    question_id: int
    content: str
    answer: str | None
    analysis: str | None
    lang: str | None  # voice choice for reading the answer aloud (post-reveal)
    crop_url: str | None = None  # 同 PracticeItem：扫描题才有原图（揭示后再看也不泄露）


def _to_practice_item(
    item: ErrorItem, question: Question, crop_url: str | None = None
) -> PracticeItem:
    return PracticeItem(
        error_item_id=item.id,
        question_id=question.id,
        subject=question.subject,
        question_type=question.question_type,
        content=question.content,
        audio_text=question.audio_text,
        lang=question.lang,
        is_dictation=bool(question.audio_text) and question.content != question.answer,
        crop_url=crop_url,
        tutor_enabled=bool(item.workflow_tutor_enabled),
        recap_enabled=bool(item.workflow_recap_enabled),
    )


def _crop_urls(session: Session, question_ids: list[int]) -> dict[int, str]:
    """question_id → 原图入口，只给真有扫题块的题。

    手录题没有 `scan_question` 行，因此天然查不到 = None（页面一个字节都不变）。
    `crop_path` 为空（老行/裁图失败）同样不给。一题多行时取最早那条有图的。
    这里**不看磁盘**：文件被删不该让练习页 500，交给 `/questions/{id}/crop` 回 404。
    """
    if not question_ids:
        return {}
    rows = session.execute(
        select(ScanQuestion.question_id, ScanQuestion.crop_path)
        .join(Scan, Scan.id == ScanQuestion.scan_id)
        .join(Question, Question.id == ScanQuestion.question_id)
        .where(
            ScanQuestion.question_id.in_(question_ids),
            ScanQuestion.crop_path.is_not(None),
            Scan.learner_id == Question.learner_id,
        )
        .order_by(ScanQuestion.id.asc())
    ).all()
    urls: dict[int, str] = {}
    for question_id, crop_path in rows:
        if question_id is None or not (crop_path or "").strip():
            continue
        urls.setdefault(question_id, f"/questions/{question_id}/crop")
    return urls


@dataclass(frozen=True)
class _Candidate:
    """Internal selection unit: the practice view plus quota/sort metadata."""

    practice: PracticeItem
    status: str
    created_at: datetime
    next_due_at: datetime | None
    difficulty: int
    red: bool

    @property
    def question_type(self) -> str:
        return self.practice.question_type

    @property
    def lang(self) -> str | None:
        return self.practice.lang

    @property
    def error_item_id(self) -> int:
        return self.practice.error_item_id


def _as_utc(dt: datetime) -> datetime:
    """SQLite round-trips DateTime columns naive; stored values are UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _uncertain_counts(session: Session, item_ids: list[int]) -> dict[int, int]:
    """Number of "hard" ratings per error_item (REVIEW ledger, real-time)."""
    if not item_ids:
        return {}
    rows = session.execute(
        select(Review.error_item_id, func.count())
        .where(Review.error_item_id.in_(item_ids), Review.rating == RATING_HARD)
        .group_by(Review.error_item_id)
    ).all()
    return {item_id: int(n) for item_id, n in rows}


def _quota_group(c: _Candidate) -> str:
    """Which quota bucket a candidate belongs to (green/red/yellow)."""
    if c.status == REVIEW_STATUS_MASTERED:
        return "green"
    return "red" if c.red else "yellow"


def _rebalance_lang(
    taken: list[_Candidate],
    unselected: list[_Candidate],
    green_cap: int,
    green_ratio: float,
) -> list[_Candidate]:
    """Batch-level zh/en balance: swap surplus-language items in the final
    batch for short-language ones from the unselected pool until the two
    counts differ by at most 1. Same-status swaps go first (composition
    untouched), cross-status second — adding green is still bounded by the
    hard cap (and forbidden outright when the ratio is 0). Gives up only when
    the short language is exhausted in the pool or every remaining swap would
    break the cap: the language is genuinely not there, not skipped work.
    lang-less items never join the balance."""
    pool_langs = {c.lang for c in taken} | {c.lang for c in unselected}
    if not ("zh" in pool_langs and "en" in pool_langs):
        return taken  # 池子里本就只有一种语言（或无语种），无从平衡
    groups_t: dict[str, list[_Candidate]] = {"green": [], "red": [], "yellow": []}
    for c in taken:
        groups_t[_quota_group(c)].append(c)
    groups_u: dict[str, list[_Candidate]] = {"green": [], "red": [], "yellow": []}
    for c in unselected:
        groups_u[_quota_group(c)].append(c)

    def _can_swap(rm_group: str, add_group: str) -> bool:
        """Green additions must respect the hard cap; a green-for-green swap
        leaves the count unchanged and is always fine (ratio > 0)."""
        if add_group != "green":
            return True
        if green_ratio <= 0:
            return False
        net_green = (add_group == "green") - (rm_group == "green")
        return net_green <= 0 or len(groups_t["green"]) < green_cap

    pair_orders = [(g, g) for g in ("green", "red", "yellow")] + [
        (rm, add)
        for rm in ("green", "yellow", "red")
        for add in ("red", "yellow", "green")
        if rm != add
    ]
    zh_n = sum(1 for c in taken if c.lang == "zh")
    en_n = sum(1 for c in taken if c.lang == "en")
    while abs(zh_n - en_n) > 1:
        surplus, short = ("zh", "en") if zh_n > en_n else ("en", "zh")
        swapped = False
        for rm_group, add_group in pair_orders:
            if not _can_swap(rm_group, add_group):
                continue
            rm_item = next(
                (c for c in groups_t[rm_group] if c.lang == surplus), None
            )
            add_item = next(
                (c for c in groups_u[add_group] if c.lang == short), None
            )
            if rm_item is None or add_item is None:
                continue
            groups_t[rm_group].remove(rm_item)
            groups_t[add_group].append(add_item)
            groups_u[add_group].remove(add_item)
            groups_u[rm_group].append(rm_item)
            if rm_item.lang == "zh":
                zh_n -= 1
            elif rm_item.lang == "en":
                en_n -= 1
            if add_item.lang == "zh":
                zh_n += 1
            elif add_item.lang == "en":
                en_n += 1
            swapped = True
            break
        if not swapped:
            break  # 短语言池已尽或绿 cap 挡死：确实不够
    return [c for g in groups_t.values() for c in g]


def _quota_select(
    cands: list[_Candidate], limit: int, green_ratio: float, red_ratio: float
) -> list[_Candidate]:
    """Pick which candidates fill the batch (BUKA 配额).

    *cands* must already be in final display order (interleaved). Green is a
    hard cap of max(1, limit*green_ratio) — 0 slots when the ratio is 0 —
    exceeded only on a true shortage (red+yellow pool < limit, green fills the
    gap). Red takes red_ratio of the remaining slots; when yellow runs dry the
    unpicked red absorbs the leftover, never green. zh/en balance is applied
    once on the final batch.
    """
    green = [c for c in cands if c.status == REVIEW_STATUS_MASTERED]
    rest = [c for c in cands if c.status != REVIEW_STATUS_MASTERED]
    red = [c for c in rest if c.red]
    yellow = [c for c in rest if not c.red]

    green_cap = max(1, int(limit * green_ratio)) if green_ratio > 0 else 0
    # 真·红黄不足（总数 < limit）才允许绿突破 cap 补满；一边稀缺由另一边吸收
    if len(red) + len(yellow) < limit and green_ratio > 0:
        green_size = min(len(green), limit - len(red) - len(yellow))
    else:
        green_size = min(len(green), green_cap)

    taken: list[_Candidate] = green[:green_size]
    remaining = limit - len(taken)

    red_size = min(len(red), int(remaining * red_ratio))
    taken.extend(red[:red_size])
    yellow_size = remaining - red_size
    taken.extend(yellow[:yellow_size])
    leftover = remaining - red_size - len(yellow[:yellow_size])
    if leftover > 0:  # 黄尽了：富余的红吸收剩余名额（绿不越界）
        taken.extend(red[red_size:][:leftover])

    taken_ids = {c.error_item_id for c in taken}
    unselected = [c for c in cands if c.error_item_id not in taken_ids]
    return _rebalance_lang(taken, unselected, green_cap, green_ratio)


def _interleave(items: list[_Candidate]) -> list[_Candidate]:
    """Greedy: each step take one from the largest remaining type group whose
    type differs from the last emitted one. Ties resolve to the group that
    appeared first in the pool order (deterministic, no randomness)."""
    groups: dict[str, deque[_Candidate]] = {}
    type_order: list[str] = []  # first-appearance order, used for tie-breaks
    for it in items:
        if it.question_type not in groups:
            groups[it.question_type] = deque()
            type_order.append(it.question_type)
        groups[it.question_type].append(it)

    rank = {t: i for i, t in enumerate(type_order)}
    result: list[_Candidate] = []
    last: str | None = None
    while any(groups.values()):
        candidates = [t for t in type_order if groups[t] and t != last]
        if not candidates:
            # only the last emitted type remains: emit rather than drop
            candidates = [t for t in type_order if groups[t]]
        pick = max(candidates, key=lambda t: (len(groups[t]), -rank[t]))
        result.append(groups[pick].popleft())
        last = pick
    return result


def fetch_practice(
    session: Session,
    learner_id: int,
    now: datetime,
    limit: int | None = None,
) -> list[PracticeItem]:
    """Build today's practice batch for *learner_id* (answers hidden).

    Pool = new + due (unchanged); composition = BUKA quota (see module
    docstring); ordering = interleave over the whole pool, quota-select from
    it. Difficulty only reorders candidates sharing one due timestamp.
    """
    from cairn.workflow.review import ready_for_practice

    if limit is None:
        limit = batch_config()[0]
    new_items = list(
        session.scalars(
            select(ErrorItem)
            .where(
                ErrorItem.learner_id == learner_id,
                ErrorItem.review_status == REVIEW_STATUS_NEW,
                ready_for_practice(ErrorItem.question_id),
            )
            .order_by(ErrorItem.created_at.asc())
        )
    )
    due_items = list(
        session.scalars(
            select(ErrorItem)
            .where(
                ErrorItem.learner_id == learner_id,
                ErrorItem.review_status != REVIEW_STATUS_ARCHIVED,
                ready_for_practice(ErrorItem.question_id),
                ErrorItem.review_status != REVIEW_STATUS_NEW,
                ErrorItem.next_due_at.is_not(None),
                ErrorItem.next_due_at <= now,
            )
            .order_by(ErrorItem.next_due_at.asc())
        )
    )

    pool = new_items + due_items
    # 题目本身也按归属过滤：pool 已经按 error_item.learner_id 收窄，这里再要求题目属于
    # 同一个孩子，才不会把别的孩子的题面带出来。历史数据里若有跨归属的引用（老库迁移
    # 前手工建的），题目取不到就跳过那一项，而不是让整批 KeyError。
    questions = {
        q.id: q for q in session.scalars(
            select(Question).where(
                Question.id.in_([i.question_id for i in pool]),
                Question.learner_id == learner_id,
            )
        )
    } if pool else {}

    # 难度分三要素全部实时算，不落库：wrong_count 列 + REVIEW 里 hard 计数
    # + restored_at（掉绿≥1 次；精确计多次需落库，本卡明确不做）。
    uncertain = _uncertain_counts(session, [i.id for i in pool])
    # 原图入口：只查已有 scan_question.crop_path，一次 IN 查询，不影响选谁/排序。
    crops = _crop_urls(session, list(questions))
    candidates: list[_Candidate] = []
    for item in pool:
        question = questions.get(item.question_id)
        if question is None:
            continue
        unc = uncertain.get(item.id, 0)
        lapse = 1 if item.restored_at is not None else 0
        candidates.append(
            _Candidate(
                practice=_to_practice_item(item, question, crops.get(question.id)),
                status=item.review_status,
                created_at=item.created_at,
                next_due_at=item.next_due_at,
                difficulty=difficulty_score(item.wrong_count, unc, lapse),
                red=unc > 0 or lapse > 0 or item.wrong_count >= 2,
            )
        )

    def _pool_key(c: _Candidate) -> tuple:
        if c.next_due_at is None:  # new：保持 created_at asc 优先
            return (0, _as_utc(c.created_at), 0)
        # due：到期早的先出；同一到期时刻内难度分降序（本卡唯一排序改动）
        return (1, _as_utc(c.next_due_at), -c.difficulty, _as_utc(c.created_at))

    candidates.sort(key=_pool_key)
    ordered = _interleave(candidates)
    _, green_ratio, red_ratio = batch_config()
    selected = _quota_select(ordered, limit, green_ratio, red_ratio)
    selected_ids = {c.error_item_id for c in selected}
    # 保持交错序输出：配额决定「谁进批」，交错序决定「批内先后」。
    return [c.practice for c in ordered if c.error_item_id in selected_ids][:limit]


def get_practice_item(
    session: Session, learner_id: int, error_item_id: int
) -> PracticeItem | None:
    """Ask face of one specific item (0.2 卡8 同类题入口).

    Same shape as a batch candidate — no answer, no analysis — so the reveal/
    grade flow afterwards is unchanged. Returns ``None`` for a missing item or
    one belonging to another learner (the caller decides what to show).
    """
    item = session.get(ErrorItem, error_item_id)
    if item is None or item.learner_id != learner_id:
        return None
    from cairn.workflow.review import ready_for_practice
    if not session.scalar(select(ready_for_practice(item.question_id))):
        return None
    question = session.get(Question, item.question_id)
    if question is None or question.learner_id != learner_id:
        return None
    return _to_practice_item(
        item, question, _crop_urls(session, [question.id]).get(question.id)
    )


def submit_answer(
    session: Session,
    learner_id: int,
    error_item_id: int,
    learner_answer: str,
) -> RevealedAnswer:
    """Reveal the canonical answer + analysis after the learner has answered.

    *learner_answer* is accepted for interface stability but is not persisted
    and not graded in this batch (parent judges manually, then rates via
    apply_review). Another learner's item is a validation error — the answer
    face must not leak across siblings.
    """
    item = session.get(ErrorItem, error_item_id)
    if item is None:
        raise ValidationError(f"error_item does not exist: error_item_id={error_item_id}")
    if item.learner_id != learner_id:
        raise ValidationError(f"error_item {error_item_id} 不属于当前 learner")
    from cairn.workflow.review import ready_for_practice
    if not session.scalar(select(ready_for_practice(item.question_id))):
        raise ValidationError("题目仍在核对或安排复习，请稍后再练")
    question = session.get(Question, item.question_id)
    if question is None or question.learner_id != learner_id:
        raise ValidationError(f"question does not exist: question_id={item.question_id}")
    return RevealedAnswer(
        error_item_id=item.id,
        question_id=question.id,
        content=question.content,
        answer=question.answer,
        analysis=question.analysis,
        lang=question.lang,
        crop_url=_crop_urls(session, [question.id]).get(question.id),
    )


def submit_and_judge(
    session: Session,
    learner_id: int,
    error_item_id: int,
    learner_answer: str,
) -> tuple[RevealedAnswer, GradeResult | ManualGrade]:
    """练习提交即判分（判题确定性层，LLM 不参与对错判定）。

    没有标准答案（NULL / 空白）：判题层无从比对，直接回 `ManualGrade`
    ——**不写 review、不动 confidence、不进 pending 复核队列**。复核队列的语义
    是「这条题录得对不对」，按钮只有确认/驳回、不落评分；把扫描题塞进去只会
    让它永远拿不到分、永远出不了库。这里交回家长，由揭示页两键落 good/again。

    判定置信度 >= 0.8：沿用现有评分映射立即写 review（对→good、错→again），
    复用 apply_review 的全部簿记（FSRS 排期、错误计数、状态迁移）。
    判定置信度 < 0.8：不写 review，把 confidence 压进 0.8 以下，让错题落入
    现有 pending 复核队列（graduation_service.pending_review_queue）交家长确认。
    """
    revealed = submit_answer(session, learner_id, error_item_id, learner_answer)
    if not (revealed.answer or "").strip():
        # 一行都不碰库：没有 commit，confidence / review_status / next_due_at 全不变。
        return revealed, ManualGrade()
    question = session.get(Question, revealed.question_id)
    result = judge(question, learner_answer)
    item = session.get(ErrorItem, error_item_id)
    if result.needs_review:
        item.confidence = result.confidence
        session.commit()
    else:
        apply_review(
            session,
            item,
            RATING_GOOD if result.correct else RATING_AGAIN,
        )
    return revealed, result
