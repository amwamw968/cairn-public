"""AC5 — due-queue filtering, ordering, limit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cairn.models import ErrorItem
from cairn.queue import due_items

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _mk_item(session, learner, question, due_offset_hours, n):
    item = ErrorItem(
        learner_id=learner.id,
        question_id=question.id,
        review_status="reviewing",
        stability=1.0,
        difficulty=5.0,
        next_due_at=NOW + timedelta(hours=due_offset_hours),
    )
    session.add(item)
    session.commit()
    return item


def test_due_queue_filtering_and_order(session, learner, question):
    # 3 due (overdue / due now), 2 not due
    a = _mk_item(session, learner, question, -3, "a")
    b = _mk_item(session, learner, question, -1, "b")
    c = _mk_item(session, learner, question, 0, "c")
    _mk_item(session, learner, question, +2, "d")
    _mk_item(session, learner, question, +48, "e")

    items = due_items(session, now=NOW)
    assert [i.id for i in items] == [a.id, b.id, c.id], "only due items, ascending by next_due_at"

    # limit returns the most-due first k
    top2 = due_items(session, now=NOW, limit=2)
    assert [i.id for i in top2] == [a.id, b.id]

    # empty window yields nothing
    assert due_items(session, now=NOW - timedelta(hours=10)) == []
