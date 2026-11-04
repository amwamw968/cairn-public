"""Statistics queries over the review ledger (0.2 卡7).

Four blocks, all three subjects, one page:

1. **薄弱榜**（``weakness_ranking``）：按知识点聚合错题数 / 未出库数 /
   近 30 天答错次数 / 答对率，按近 30 天答错次数降序。
2. **错因 / 题型分布**（``wrong_distribution``）：近 30 天 question_type × subject
   的答错次数（again/hard 算错，good/easy 算对）。
3. **复习完成度**（``review_completion``）：今天到期 / 今天已完成 / 逾期未做。
   ``practice_today`` 另给 MCP 的「今天这一批」口径：新题 + 到期题，与取题池一致。
4. **打卡 + 掌握度曲线**（``streak`` / ``mastery_curve``）：连续作答天数；
   最近 30 天每天结束时已出库题数的累计值。

Hard rules（见 docs/CONCEPTS-RESEARCH.md 第二、五、七节）：

- 错误率的分母是**作答记录**（``review`` 行），不是 ``error_item`` 的 ``wrong_count``。
- 同一题挂多个知识点时，题集按 **question_id 集合**取并集：一道题的 3 次作答在
  单个知识点下仍是 3 次；跨知识点则各算一次（这是「按知识点归因」，不是重复计数）。
- **只查库**，不 import 调度器、不调模型、不发网络请求。
- 每个模块自带 ``has_data``；没有数据时页面显示「还没有数据」，不显示 0 或空白。

Two deliberate approximations, both documented in docs/HANDOFF.md:

- 库内没有 ``archived_at``：掌握度曲线把「该题最后一次作答日」当作出库日。
  走驳回出库、从没作答过的题不算掌握（它们本来也不是「学会了」）。
- 「今天」按传入时区（默认服务器本地时区）的日历日切；库里存的是 naive UTC，
  ``_to_tz`` 统一把 naive 当 UTC 再转换。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.models.concept import CONCEPT_STATUS_ACTIVE, Concept, question_concept
from cairn.models.error_item import ErrorItem, REVIEW_STATUS_ARCHIVED
from cairn.models.learner import utcnow
from cairn.models.question import Question
from cairn.models.review import RATING_AGAIN, RATING_HARD, Review

WINDOW_DAYS = 30
MIN_SAMPLE = 3
WRONG_RATINGS = frozenset({RATING_AGAIN, RATING_HARD})
SUBJECT_ORDER = ("数学", "英语", "语文")

_CURVE_WIDTH = 600
_CURVE_HEIGHT = 120
_CURVE_PAD = 8


@dataclass(frozen=True)
class ConceptStat:
    """One row of the weakness ranking."""

    concept_id: int
    name: str
    subject: str
    error_count: int  # 该知识点下有错题的题目数（按题去重）
    open_count: int  # 其中还没出库的题目数
    wrong_30d: int  # 近 30 天 again/hard 次数（分母是 review）
    answered: int  # 该知识点下全部作答记录数（样本量）
    rate_percent: int | None  # 答对率；样本 < MIN_SAMPLE 时为 None

    @property
    def sample_sufficient(self) -> bool:
        return self.rate_percent is not None


@dataclass(frozen=True)
class DistributionRow:
    """One question_type row; *cells* align with the subject columns."""

    question_type: str
    cells: tuple[int, ...]
    total: int


@dataclass(frozen=True)
class CompletionStat:
    due_today: int
    completed_today: int
    overdue: int
    has_data: bool


@dataclass(frozen=True)
class PracticeTodayStat:
    """今天这一批题的口径（与 ``practice_service.fetch_practice`` 对齐）。

    ``playable`` 含今天已经练过的题：否则复习一次会把 ``next_due_at`` 推到将来、
    同时把 ``last_reviewed_at`` 写满，可练与已完成各减一，练一道剩余会掉 2。
    """

    playable: int
    completed: int
    remaining: int
    total_items: int


@dataclass(frozen=True)
class StreakStat:
    days: int
    reviewed_today: bool
    has_data: bool


@dataclass(frozen=True)
class MasteryPoint:
    day: date
    count: int
    x: float
    y: float


@dataclass(frozen=True)
class MasteryCurve:
    points: tuple[MasteryPoint, ...]
    max_count: int
    start_label: str
    end_label: str
    has_data: bool


@dataclass(frozen=True)
class StatsReport:
    weakness: tuple[ConceptStat, ...]
    distribution_subjects: tuple[str, ...]
    distribution: tuple[DistributionRow, ...]
    completion: CompletionStat
    streak: StreakStat
    mastery: MasteryCurve
    window_label: str


def _to_tz(value: datetime, tz: tzinfo) -> datetime:
    """Normalize a stored timestamp for calendar-day bucketing.

    SQLite round-trips DateTime columns without tzinfo; the app writes UTC
    (``utcnow()``), so a naive value is UTC.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def build_stats(
    session: Session,
    learner_id: int,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> StatsReport:
    """Aggregate every block for *learner_id* in one pass over the ledger."""
    from cairn.workflow.review import ready_for_practice

    tzinfo = tz if tz is not None else datetime.now().astimezone().tzinfo
    now_local = _to_tz(now if now is not None else utcnow(), tzinfo)
    today = now_local.date()
    today_start = datetime.combine(today, time.min, tzinfo=tzinfo)
    tomorrow_start = today_start + timedelta(days=1)
    window_start = today_start - timedelta(days=WINDOW_DAYS - 1)

    # --- load the learner's slice (items -> questions -> reviews) ---------
    item_rows = session.execute(
        select(
            ErrorItem.id,
            ErrorItem.question_id,
            ErrorItem.review_status,
            ErrorItem.next_due_at,
            ErrorItem.last_reviewed_at,
        ).join(Question, Question.id == ErrorItem.question_id).where(
            ErrorItem.learner_id == learner_id, Question.learner_id == learner_id,
            ready_for_practice(Question.id))
    ).all()
    item_meta: dict[int, tuple[int, str, datetime | None, datetime | None]] = {
        item_id: (
            question_id,
            status,
            _to_tz(due, tzinfo) if due is not None else None,
            _to_tz(last, tzinfo) if last is not None else None,
        )
        for item_id, question_id, status, due, last in item_rows
    }
    question_ids = {meta[0] for meta in item_meta.values()}

    question_meta: dict[int, tuple[str, str]] = {}
    if question_ids:
        for qid, subject, qtype in session.execute(
            select(Question.id, Question.subject, Question.question_type).where(
                Question.id.in_(question_ids),
                # 题面归属也要对得上：统计只算本孩子名下的题，跨归属的历史引用跳过。
                Question.learner_id == learner_id,
            )
        ):
            question_meta[qid] = (subject, qtype)

    reviews: list[tuple[int, int, str, datetime]] = [
        (review_id, error_item_id, rating, _to_tz(reviewed_at, tzinfo))
        for review_id, error_item_id, rating, reviewed_at in session.execute(
            select(Review.id, Review.error_item_id, Review.rating, Review.reviewed_at)
            .join(ErrorItem, ErrorItem.id == Review.error_item_id)
            .join(Question, Question.id == ErrorItem.question_id)
            .where(ErrorItem.learner_id == learner_id, Question.learner_id == learner_id,
                   ready_for_practice(Question.id))
        )
    ]

    # --- 1. weakness ranking ---------------------------------------------
    concept_rows = list(
        session.scalars(
            select(Concept)
            .where(Concept.status == CONCEPT_STATUS_ACTIVE)
            .order_by(Concept.id)
        )
    )
    concept_questions: dict[int, set[int]] = {}
    for concept_id, question_id in session.execute(
        select(question_concept.c.concept_id, question_concept.c.question_id)
    ):
        if question_id in question_ids:
            concept_questions.setdefault(concept_id, set()).add(question_id)

    items_by_question: dict[int, list[int]] = {}
    for item_id, meta in item_meta.items():
        items_by_question.setdefault(meta[0], []).append(item_id)

    reviews_by_item: dict[int, list[tuple[str, datetime]]] = {}
    for _, error_item_id, rating, reviewed_at in reviews:
        reviews_by_item.setdefault(error_item_id, []).append((rating, reviewed_at))

    weakness: list[ConceptStat] = []
    for concept in concept_rows:
        qids = concept_questions.get(concept.id)
        if not qids:
            continue
        answered = 0
        wrong_all = 0
        wrong_30d = 0
        for question_id in qids:
            for item_id in items_by_question.get(question_id, []):
                for rating, reviewed_at in reviews_by_item.get(item_id, []):
                    answered += 1
                    if rating in WRONG_RATINGS:
                        wrong_all += 1
                        if window_start <= reviewed_at <= now_local:
                            wrong_30d += 1
        error_count = sum(
            1 for question_id in qids if items_by_question.get(question_id)
        )
        open_count = sum(
            1
            for question_id in qids
            if any(
                item_meta[item_id][1] != REVIEW_STATUS_ARCHIVED
                for item_id in items_by_question.get(question_id, [])
            )
        )
        rate_percent = (
            round((answered - wrong_all) * 100 / answered)
            if answered >= MIN_SAMPLE
            else None
        )
        weakness.append(
            ConceptStat(
                concept_id=concept.id,
                name=concept.name,
                subject=concept.subject,
                error_count=error_count,
                open_count=open_count,
                wrong_30d=wrong_30d,
                answered=answered,
                rate_percent=rate_percent,
            )
        )
    weakness.sort(key=lambda row: (-row.wrong_30d, -row.error_count, row.name))

    # --- 2. question_type x subject wrong distribution --------------------
    observed_subjects: set[str] = set()
    cell_counts: dict[tuple[str, str], int] = {}
    for _, error_item_id, rating, reviewed_at in reviews:
        if rating not in WRONG_RATINGS:
            continue
        if not (window_start <= reviewed_at <= now_local):
            continue
        meta = item_meta[error_item_id]
        question = question_meta.get(meta[0])
        if question is None:
            continue
        subject, qtype = question
        observed_subjects.add(subject)
        cell_counts[(qtype, subject)] = cell_counts.get((qtype, subject), 0) + 1

    subjects = tuple(
        [s for s in SUBJECT_ORDER if s in observed_subjects]
        + sorted(observed_subjects - set(SUBJECT_ORDER))
    )
    distribution: list[DistributionRow] = []
    for qtype in {key[0] for key in cell_counts}:
        cells = tuple(cell_counts.get((qtype, subject), 0) for subject in subjects)
        distribution.append(
            DistributionRow(question_type=qtype, cells=cells, total=sum(cells))
        )
    distribution.sort(key=lambda row: (-row.total, row.question_type))

    # --- 3. review completion --------------------------------------------
    reviewed_today_ids = {
        error_item_id
        for _, error_item_id, _, reviewed_at in reviews
        if today_start <= reviewed_at < tomorrow_start
    }
    due_today = 0
    overdue = 0
    for item_id, (_, status, due, _) in item_meta.items():
        if status == REVIEW_STATUS_ARCHIVED or due is None:
            continue
        if today_start <= due < tomorrow_start:
            due_today += 1
        elif due < today_start and item_id not in reviewed_today_ids:
            overdue += 1
    completion = CompletionStat(
        due_today=due_today,
        completed_today=len(reviewed_today_ids),
        overdue=overdue,
        has_data=bool(item_meta) or bool(reviews),
    )

    # --- 4. streak + mastery curve ---------------------------------------
    review_days = {reviewed_at.date() for _, _, _, reviewed_at in reviews}
    streak_days = _streak_length(review_days, today)
    streak = StreakStat(
        days=streak_days,
        reviewed_today=today in review_days,
        has_data=bool(review_days),
    )

    mastered_days = [
        last.date()
        for _, status, _, last in item_meta.values()
        if status == REVIEW_STATUS_ARCHIVED and last is not None
    ]
    days = [today - timedelta(days=WINDOW_DAYS - 1 - i) for i in range(WINDOW_DAYS)]
    counts = [sum(1 for day in mastered_days if day <= current) for current in days]
    max_count = max(counts)
    points = tuple(
        _curve_point(index, day, count, max_count)
        for index, (day, count) in enumerate(zip(days, counts))
    )
    mastery = MasteryCurve(
        points=points,
        max_count=max_count,
        start_label=days[0].strftime("%m-%d"),
        end_label=days[-1].strftime("%m-%d"),
        has_data=max_count > 0,
    )

    return StatsReport(
        weakness=tuple(weakness),
        distribution_subjects=subjects,
        distribution=tuple(distribution),
        completion=completion,
        streak=streak,
        mastery=mastery,
        window_label=f"近 {WINDOW_DAYS} 天（{days[0].strftime('%m-%d')} 至 {days[-1].strftime('%m-%d')}）",
    )


def practice_today(
    session: Session,
    learner_id: int,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> PracticeTodayStat:
    """今天可练 / 已完成 / 剩余，口径与取题池一致。

    - 可练（还没做的）= 未出库，且 ``next_due_at <= now`` 或从未复习过
      （``last_reviewed_at`` 为空，新录的题就落在这一支）；
    - 已完成 = 今天有 review 记录的不同错题数（与 ``build_stats`` 同源）；
    - 剩余 = 可练（还没做的）- 今天已完成的；
    - 对外报的 ``playable`` = 剩余 + 已完成（今天这一批的全体，见 dataclass 注释）。
    """
    from cairn.workflow.review import ready_for_practice

    tzinfo = tz if tz is not None else datetime.now().astimezone().tzinfo
    now_local = _to_tz(now if now is not None else utcnow(), tzinfo)
    today_start = datetime.combine(now_local.date(), time.min, tzinfo=tzinfo)
    tomorrow_start = today_start + timedelta(days=1)

    items = session.execute(
        select(
            ErrorItem.id,
            ErrorItem.review_status,
            ErrorItem.next_due_at,
            ErrorItem.last_reviewed_at,
        ).join(Question, Question.id == ErrorItem.question_id).where(
            ErrorItem.learner_id == learner_id, Question.learner_id == learner_id,
            ready_for_practice(Question.id))
    ).all()

    reviewed_today_ids = {
        error_item_id
        for error_item_id, reviewed_at in session.execute(
            select(Review.error_item_id, Review.reviewed_at)
            .join(ErrorItem, ErrorItem.id == Review.error_item_id)
            .join(Question, Question.id == ErrorItem.question_id)
            .where(ErrorItem.learner_id == learner_id, Question.learner_id == learner_id,
                   ready_for_practice(Question.id))
        )
        if today_start <= _to_tz(reviewed_at, tzinfo) < tomorrow_start
    }

    open_ids: set[int] = set()
    for item_id, status, due, last in items:
        if status == REVIEW_STATUS_ARCHIVED:
            continue
        if last is None or (due is not None and _to_tz(due, tzinfo) <= now_local):
            open_ids.add(item_id)

    remaining = len(open_ids - reviewed_today_ids)
    completed = len(reviewed_today_ids)
    return PracticeTodayStat(
        playable=remaining + completed,
        completed=completed,
        remaining=remaining,
        total_items=len(items),
    )


def _streak_length(review_days: set[date], today: date) -> int:
    """Consecutive review days ending today — or yesterday when today is still
    empty, so the current day never punishes the child (温和打卡)."""
    if today in review_days:
        anchor = today
    elif (today - timedelta(days=1)) in review_days:
        anchor = today - timedelta(days=1)
    else:
        return 0
    length = 0
    cursor = anchor
    while cursor in review_days:
        length += 1
        cursor -= timedelta(days=1)
    return length


def _curve_point(
    index: int, day: date, count: int, max_count: int
) -> MasteryPoint:
    span_x = _CURVE_WIDTH - 2 * _CURVE_PAD
    span_y = _CURVE_HEIGHT - 2 * _CURVE_PAD
    x = _CURVE_PAD + span_x * index / (WINDOW_DAYS - 1)
    ratio = count / max_count if max_count else 0.0
    y = _CURVE_HEIGHT - _CURVE_PAD - span_y * ratio
    return MasteryPoint(day=day, count=count, x=round(x, 1), y=round(y, 1))
