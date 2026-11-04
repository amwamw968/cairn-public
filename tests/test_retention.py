"""0.2 卡4a — 分科记忆目标：数学 0.85、英语/语文 0.90。

断言口径两个层次：
1. 参数层：`desired_retention()` 按学科取 learner 的字段 / 模型默认值；
2. 结果层：同一 NOW、同一评分，数学下次到期 11-03、英语 11-01 —— 差异只能来自
   desired_retention（stability/difficulty 完全相同），证明 FSRS 仍是唯一排期权。
"""

from __future__ import annotations

from datetime import datetime, timezone

from cairn.models import (
    DEFAULT_RETENTION_MATH,
    DEFAULT_RETENTION_OTHER,
    ErrorItem,
    Question,
)
from cairn.models.review import RATING_GOOD
from cairn.review_service import apply_review
from cairn.scheduler import desired_retention, is_math_subject, scheduler_for

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
# fsrs==6.3.2：S=D=0 起步，Good 评级，间隔只由 desired_retention 决定
DUE_MATH = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)   # 0.85
DUE_OTHER = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)  # 0.90


def test_learner_defaults(session, learner):
    """AC5：新建 learner 自带 0.85 / 0.90。"""
    assert learner.desired_retention_math == DEFAULT_RETENTION_MATH == 0.85
    assert learner.desired_retention_other == DEFAULT_RETENTION_OTHER == 0.90


def test_subject_mapping():
    """AC6 参数层：数学（含历史 math 码）→0.85，其余→0.90。"""
    for subject in ("数学", "数学（计算）", "math", "Math"):
        assert is_math_subject(subject)
        assert desired_retention(subject) == 0.85
    for subject in ("英语", "语文", "english", "", None, "科学"):
        assert not is_math_subject(subject)
        assert desired_retention(subject) == 0.90


def test_scheduler_differs_only_by_interval():
    """AC6 结果层：两种 retention 的 S/D 一致，只有下次到期不同。"""
    math = scheduler_for("数学").schedule(RATING_GOOD, now=NOW)
    other = scheduler_for("英语").schedule(RATING_GOOD, now=NOW)

    assert math.due_at == DUE_MATH
    assert other.due_at == DUE_OTHER
    assert math.stability == other.stability
    assert math.difficulty == other.difficulty


def test_review_uses_question_subject(session, learner):
    """AC6 端到端：apply_review 按题目学科选 retention。"""
    math_question = Question(
        learner_id=learner.id,
        subject="数学",
        grade_level="primary-3",
        question_type="计算",
        content="23 × 4 = ?",
        answer="92",
    )
    english_question = Question(
        learner_id=learner.id,
        subject="英语",
        grade_level="primary-3",
        question_type="单词",
        content="苹果",
        answer="apple",
    )
    session.add_all([math_question, english_question])
    session.flush()

    math_item = ErrorItem(learner_id=learner.id, question_id=math_question.id)
    english_item = ErrorItem(learner_id=learner.id, question_id=english_question.id)
    session.add_all([math_item, english_item])
    session.commit()

    apply_review(session, math_item, RATING_GOOD, now=NOW)
    apply_review(session, english_item, RATING_GOOD, now=NOW)

    assert math_item.next_due_at == DUE_MATH
    assert english_item.next_due_at == DUE_OTHER
    assert math_item.stability == english_item.stability


def test_learner_can_override_retention(session, learner):
    """AC6：learner 自己改过的目标值优先于默认（这里 0.97 → 间隔更短）。"""
    learner.desired_retention_math = 0.97
    learner.desired_retention_other = 0.95
    session.commit()

    assert desired_retention("数学", learner) == 0.97
    assert desired_retention("语文", learner) == 0.95
    # 0.97 比 0.85 要求更高留存 → 下次到期更早（10-31 < 11-03）
    assert scheduler_for("数学", learner).schedule(RATING_GOOD, now=NOW).due_at == datetime(
        2026, 10, 31, 12, 0, tzinfo=timezone.utc
    )


def test_out_of_range_retention_falls_back(session, learner):
    """手改坏的库不能让评卷中途抛错：越界值忽略，回落到默认。"""
    learner.desired_retention_math = 1.5
    learner.desired_retention_other = 0.0
    session.commit()

    assert desired_retention("数学", learner) == 0.85
    assert desired_retention("英语", learner) == 0.90
