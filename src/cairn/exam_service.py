"""考试倒计时（0.2 卡10）：考前 N 天把「考后才到期」的题提前到考前。

三件事，页面与路由不做业务判断：

1. ``set_exam`` / ``delete_exam`` / ``build_exam_view`` —— 家长维护考试（名称、日期、
   学科、备注）。学科为空 = 全部学科；过期记录只标「已结束」，不自动删。
2. ``active_exam`` / ``days_left`` / ``in_sprint`` —— 当前生效的考试是「日期最早且
   还没过期」的那一场；考前 ``SPRINT_DAYS``（默认 7）天进入冲刺期。
3. ``plan_exam_pull_forward`` —— 冲刺期里把 ``next_due_at`` 落在
   ``考试日之后、考试日 + 窗口之内`` 的题，按考试日期倒排、均匀铺到「今天 → 考前一天」。

为什么是「考试日之后」：考前本来就该到期的题不用动，它们自然会被练到；真正会漏的是
「考完之后才轮到我」的那批题 —— 考前不提前见一面，考试就用不上。

幂等：被提前的题落点都在考试日之前，第二次执行时不再命中筛选条件，所以
``pulled == 0``、``next_due_at`` 一字不变，不会越拉越前。

边界：只写 ``error_item.next_due_at``，不碰 stability / difficulty / review 账本，
不生成新题、不增加每日题量（``fetch_practice`` 的 limit 与取题池一行未改）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.entry_service import ValidationError
from cairn.models import Exam, ErrorItem, Question
from cairn.models.error_item import REVIEW_STATUS_ARCHIVED

__all__ = [
    "SPRINT_DAYS",
    "SUBJECTS",
    "ExamRow",
    "ExamView",
    "PullResult",
    "active_exam",
    "build_exam_view",
    "days_left",
    "delete_exam",
    "in_sprint",
    "plan_exam_pull_forward",
    "set_exam",
]

# 默认考前 7 天进入冲刺期（TODO-9GE 第 2 节：N 默认 7）
SPRINT_DAYS = 7
# 与录入/统计同一个中文口径；空 = 全部学科
SUBJECTS = ("数学", "英语", "语文")


@dataclass(frozen=True)
class PullResult:
    """One sprint run: which exam, how far away, how many items moved."""

    exam: Exam | None
    days_left: int | None
    in_sprint: bool
    pulled: int


@dataclass(frozen=True)
class ExamRow:
    """One exam as the list shows it."""

    id: int
    name: str
    exam_date: date
    subject: str | None
    note: str | None
    days_left: int  # 已结束为负数
    active: bool


@dataclass(frozen=True)
class ExamView:
    rows: tuple[ExamRow, ...]
    active_id: int | None


def set_exam(
    session: Session,
    learner_id: int,
    name: str,
    exam_date: date,
    subject: str | None = None,
    note: str | None = None,
) -> Exam:
    """Create one exam row; validation happens before anything is written."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValidationError("考试名称不能为空")
    if isinstance(exam_date, datetime) or not isinstance(exam_date, date):
        raise ValidationError(f"exam_date 必须是日期（YYYY-MM-DD），收到 {exam_date!r}")
    clean_subject = (subject or "").strip() or None
    if clean_subject is not None and clean_subject not in SUBJECTS:
        raise ValidationError(
            f"学科只能是 {'/'.join(SUBJECTS)} 或留空（全部学科），收到 {clean_subject!r}"
        )
    exam = Exam(
        learner_id=learner_id,
        name=clean_name,
        exam_date=exam_date,
        subject=clean_subject,
        note=(note or "").strip() or None,
    )
    session.add(exam)
    session.commit()
    return exam


def delete_exam(session: Session, learner_id: int, exam_id: int) -> bool:
    """Delete one of *learner_id*'s exams; someone else's id is a no-op."""
    exam = session.get(Exam, exam_id)
    if exam is None or exam.learner_id != learner_id:
        return False
    session.delete(exam)
    session.commit()
    return True


def active_exam(session: Session, learner_id: int, today: date) -> Exam | None:
    """The earliest exam that has not passed yet (same day ties by id).

    ``exam_date < today`` is history: it stays in the table but stops counting.
    """
    return session.scalars(
        select(Exam)
        .where(Exam.learner_id == learner_id, Exam.exam_date >= today)
        .order_by(Exam.exam_date.asc(), Exam.id.asc())
        .limit(1)
    ).first()


def days_left(exam: Exam, today: date) -> int:
    """Calendar days from *today* to the exam (0 = today, negative = past)."""
    return (exam.exam_date - today).days


def in_sprint(exam: Exam, today: date, window_days: int = SPRINT_DAYS) -> bool:
    """Within the pre-exam window (exam day itself counts as the last day)."""
    left = days_left(exam, today)
    return 0 <= left <= window_days


def plan_exam_pull_forward(
    session: Session,
    learner_id: int,
    today: date,
    window_days: int = SPRINT_DAYS,
) -> PullResult:
    """Pull the exam's post-exam due items forward into the days before it.

    Selection: this learner's non-archived items whose ``next_due_at`` falls in
    ``(exam day, exam day + window_days]``, optionally limited to the exam's
    subject. Targets: the days ``today … exam_date - 1``, items sorted by
    original due date and spread evenly (latest due lands closest to the exam —
    scheduling backwards from the exam date). Clock time is preserved; only the
    date changes.
    """
    exam = active_exam(session, learner_id, today)
    if exam is None:
        return PullResult(exam=None, days_left=None, in_sprint=False, pulled=0)
    left = days_left(exam, today)
    if left > window_days or left <= 0:
        # 还没进冲刺期，或者今天就是考试日（没有「考前」的可用日）
        return PullResult(
            exam=exam, days_left=left, in_sprint=in_sprint(exam, today, window_days), pulled=0
        )

    exam_end = datetime.combine(exam.exam_date + timedelta(days=1), time.min)
    horizon = datetime.combine(exam.exam_date + timedelta(days=window_days + 1), time.min)
    statement = (
        select(ErrorItem)
        .join(Question, Question.id == ErrorItem.question_id)
        .where(
            ErrorItem.learner_id == learner_id,
            ErrorItem.review_status != REVIEW_STATUS_ARCHIVED,
            ErrorItem.next_due_at.is_not(None),
            ErrorItem.next_due_at >= exam_end,
            ErrorItem.next_due_at < horizon,
        )
        .order_by(ErrorItem.next_due_at.asc(), ErrorItem.id.asc())
    )
    if exam.subject is not None:
        statement = statement.where(Question.subject == exam.subject)
    items = list(session.scalars(statement))
    if not items:
        return PullResult(exam=exam, days_left=left, in_sprint=True, pulled=0)

    days = [today + timedelta(days=offset) for offset in range(left)]
    pulled = 0
    for index, item in enumerate(items):
        due = item.next_due_at
        clock = _naive_utc(due).time()
        # 倒排：到期越晚的题，落在越靠近考试日的那一天
        target_day = days[index * len(days) // len(items)]
        target = datetime.combine(target_day, clock)
        if target != due:
            item.next_due_at = target
            pulled += 1
    if pulled:
        session.commit()
    return PullResult(exam=exam, days_left=left, in_sprint=True, pulled=pulled)


def build_exam_view(session: Session, learner_id: int, today: date) -> ExamView:
    """Upcoming exams (nearest first), then history (most recent first)."""
    exams = list(
        session.scalars(select(Exam).where(Exam.learner_id == learner_id))
    )
    active = active_exam(session, learner_id, today)
    upcoming = sorted(
        (exam for exam in exams if exam.exam_date >= today),
        key=lambda exam: (exam.exam_date, exam.id),
    )
    history = sorted(
        (exam for exam in exams if exam.exam_date < today),
        key=lambda exam: (exam.exam_date, exam.id),
        reverse=True,
    )
    rows = tuple(
        ExamRow(
            id=exam.id,
            name=exam.name,
            exam_date=exam.exam_date,
            subject=exam.subject,
            note=exam.note,
            days_left=days_left(exam, today),
            active=exam.exam_date >= today,
        )
        for exam in (*upcoming, *history)
    )
    return ExamView(rows=rows, active_id=None if active is None else active.id)


def _naive_utc(value: datetime) -> datetime:
    """SQLite hands back naive UTC; normalise an aware value if one arrives."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
