"""近况一句话（首页 + 「最近怎么样」页同源）：这周练了几题 / 几题还在反复错。

口径（唯一定义，两页共用；G2-7 的「最近怎么样」从这里取数，两页必须同数）：

- **这周练了 N 题**：近 7 个自然日（含今天）有过至少一次作答记录（review 行）
  的不同错题数。用滚动日历周而不是「本周一至今」——定稿线框「这周练了 18 题」
  画的就是周一，按「本周一至今」周一只剩当天，线框数字就假了。
- **M 题还在反复错**：还没「学会」的题里，练习中又答错过至少一次
  （wrong_count ≥ 2：入库时错 1 次 + 练习里又「答错」≥ 1 次），或学会过又掉
  回来过（restored_at 非空）。只答「还迷糊」不算——「错」就是真错了。

只查库：不 import 调度器、不发网络请求；practice / stats 的既有口径原样，
本模块不改它们一行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.models.error_item import ErrorItem, REVIEW_STATUS_ARCHIVED
from cairn.models.learner import utcnow
from cairn.models.question import Question
from cairn.models.review import Review

WINDOW_DAYS = 7


def _to_tz(value: datetime, tz: tzinfo) -> datetime:
    """SQLite 取回的 datetime 会丢时区；按 UTC 解释再换算（写入时就是 UTC）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


@dataclass(frozen=True)
class Glance:
    """首页近况一句话的两个数（没数据时 has_data=False，页面整行不出现）。"""

    practiced_this_week: int
    repeated_wrong: int

    @property
    def has_data(self) -> bool:
        return self.practiced_this_week > 0 or self.repeated_wrong > 0


def build_glance(
    session: Session,
    learner_id: int,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> Glance:
    """一次取全近况两个数：练过的题数（滚动 7 个自然日）+ 还在反复错的题数。"""
    from cairn.workflow.review import ready_for_practice

    tzinfo = tz if tz is not None else datetime.now().astimezone().tzinfo
    now_local = _to_tz(now if now is not None else utcnow(), tzinfo)
    today = now_local.date()
    window_start = datetime.combine(
        today - timedelta(days=WINDOW_DAYS - 1), time.min, tzinfo=tzinfo
    )

    practiced: set[int] = set()
    for item_id, reviewed_at in session.execute(
        select(Review.error_item_id, Review.reviewed_at)
        .join(ErrorItem, ErrorItem.id == Review.error_item_id)
        .join(Question, Question.id == ErrorItem.question_id)
        .where(ErrorItem.learner_id == learner_id, Question.learner_id == learner_id,
               ready_for_practice(Question.id))
    ):
        if window_start <= _to_tz(reviewed_at, tzinfo) < now_local + timedelta(days=1):
            practiced.add(item_id)

    repeated = 0
    for wrong_count, restored_at in session.execute(
        select(ErrorItem.wrong_count, ErrorItem.restored_at).join(Question, Question.id == ErrorItem.question_id).where(
            ErrorItem.learner_id == learner_id,
            Question.learner_id == learner_id,
            ErrorItem.review_status != REVIEW_STATUS_ARCHIVED,
            ready_for_practice(Question.id),
        )
    ):
        if wrong_count >= 2 or restored_at is not None:
            repeated += 1

    return Glance(practiced_this_week=len(practiced), repeated_wrong=repeated)
