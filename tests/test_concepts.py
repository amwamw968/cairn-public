"""0.2 卡4a — 知识点三表：规范名 + 别名 + 一题多挂。

调研结论（docs/CONCEPTS-RESEARCH.md 第二节）：统计要准，必须让「解方程」和
「一元一次方程解法」落到同一个 concept，且一题可以同时归到多个知识点。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from cairn.concept_service import (
    add_alias,
    concepts_for_question,
    ensure_concept,
    get_concept,
    normalize_name,
    split_names,
    tag_question,
)
from cairn.models import (
    CONCEPT_STATUSES,
    CONCEPT_STATUS_ACTIVE,
    CONCEPT_STATUS_IGNORED,
    CONCEPT_STATUS_MERGED,
    Concept,
    ConceptAlias,
    Learner,
    Question,
    question_concept,
)


def _add_question(session, subject: str, content: str) -> Question:
    # 题目必须有归属：这些用例只关心知识点，随便造一个孩子当主人即可。
    owner = Learner(name="test-owner")
    session.add(owner)
    session.flush()
    q = Question(
        learner_id=owner.id,
        subject=subject,
        grade_level="primary-3",
        question_type="计算",
        content=content,
        answer="42",
    )
    session.add(q)
    session.flush()
    return q


def test_tables_shape(session):
    """AC1：三张表在，字段/唯一约束/复合主键与规格一致。"""
    tables = set(inspect(session.get_bind()).get_table_names())
    assert {"concepts", "concept_aliases", "question_concept"} <= tables

    concept_columns = {
        c["name"] for c in inspect(session.get_bind()).get_columns("concepts")
    }
    assert {"id", "name", "subject", "parent_id", "status"} <= concept_columns

    alias_columns = {
        c["name"] for c in inspect(session.get_bind()).get_columns("concept_aliases")
    }
    assert {"id", "concept_id", "alias"} <= alias_columns

    link_columns = {
        c["name"] for c in inspect(session.get_bind()).get_columns("question_concept")
    }
    assert link_columns == {"question_id", "concept_id"}


def test_concept_unique_per_subject(session):
    """同一学科同规范名只能有一行；跨学科可以重名。"""
    session.add(Concept(subject="数学", name="进位"))
    session.commit()

    session.add(Concept(subject="数学", name="进位"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(Concept(subject="语文", name="进位"))
    session.commit()
    assert session.scalar(select(func.count()).select_from(Concept)) == 2


def test_alias_folds_spoken_forms_into_one_concept(session):
    """AC2：『解方程』与『一元一次方程解法』落到同一个 concept。"""
    concept = ensure_concept(session, "数学", "解方程")
    add_alias(session, concept, "一元一次方程解法")
    session.commit()

    via_alias = get_concept(session, "数学", "一元一次方程解法")
    via_name = get_concept(session, "数学", "解方程")

    assert via_alias is not None and via_name is not None
    assert via_alias.id == via_name.id == concept.id
    # ensure_concept 也走别名：同一个叫法不会建出第二个知识点
    assert ensure_concept(session, "数学", "一元一次方程解法").id == concept.id
    assert session.scalar(select(func.count()).select_from(Concept)) == 1
    assert session.scalar(select(func.count()).select_from(ConceptAlias)) == 1


def test_surface_variants_collapse_without_alias(session):
    """AC2：全角空格 / 内部空格 / 大小写只是同一个名字的不同写法。"""
    concept = ensure_concept(session, "数学", "解方程")
    for variant in ("解方程", " 解方程 ", "解 方程", "　解 方程　"):
        assert get_concept(session, "数学", variant).id == concept.id
    # 英文知识点大小写不敏感，但保留词间空格
    english = ensure_concept(session, "英语", "Keep  Cool")
    assert english.name == "keep cool"
    assert get_concept(session, "英语", "keep cool").id == english.id
    session.commit()
    assert session.scalar(select(func.count()).select_from(Concept)) == 2


def test_add_alias_is_idempotent_and_never_steals(session):
    """重复登记同一别名返回原行；别名已属别的知识点则报错（不静默改指向）。"""
    first = ensure_concept(session, "数学", "解方程")
    second = ensure_concept(session, "数学", "方程移项")
    row = add_alias(session, first, "一元一次方程解法")
    session.commit()

    assert add_alias(session, first, "一元一次方程解法").id == row.id
    with pytest.raises(ValueError):
        add_alias(session, second, "一元一次方程解法")
    # 规范名本身不需要别名行
    assert add_alias(session, first, " 解方程 ") is None
    assert session.scalar(select(func.count()).select_from(ConceptAlias)) == 1


def test_question_can_carry_two_concepts(session):
    """AC3：一题挂两个知识点 → 两行关联；重复挂不产生第二行。"""
    q = _add_question(session, "数学", "23 × 4 = ?")
    tag_question(session, q, ["进位", "两位数乘一位数"])
    session.commit()

    assert len(concepts_for_question(session, q.id)) == 2
    assert session.scalar(select(func.count()).select_from(question_concept)) == 2

    tag_question(session, q, ["进位", "进位"])
    session.commit()
    assert session.scalar(select(func.count()).select_from(question_concept)) == 2


def test_tag_by_alias_lands_on_same_concept(session):
    """AC2+AC3：两种叫法挂到两道题上，统计时是同一个知识点。"""
    concept = ensure_concept(session, "数学", "解方程")
    add_alias(session, concept, "一元一次方程解法")

    q1 = _add_question(session, "数学", "x + 3 = 8，求 x")
    q2 = _add_question(session, "数学", "2x - 5 = 7，求 x")
    tag_question(session, q1, ["解方程"])
    tag_question(session, q2, ["一元一次方程解法"])
    session.commit()

    assert [c.id for c in concepts_for_question(session, q1.id)] == [concept.id]
    assert [c.id for c in concepts_for_question(session, q2.id)] == [concept.id]


def test_status_default_active_and_inactive_stops_new_tags(session):
    """AC4：默认 active；merged/ignored 不再挂新题，但行与别名都还在。"""
    active = ensure_concept(session, "数学", "进位")
    assert active.status == CONCEPT_STATUS_ACTIVE
    assert CONCEPT_STATUSES == {"active", "merged", "ignored"}

    merged = ensure_concept(session, "数学", "退位")
    merged.status = CONCEPT_STATUS_MERGED
    add_alias(session, merged, "借位")
    ignored = ensure_concept(session, "数学", "凑十")
    ignored.status = CONCEPT_STATUS_IGNORED
    session.commit()

    q = _add_question(session, "数学", "32 - 18 = ?")
    assert tag_question(session, q, ["进位", "退位", "借位", "凑十"]) == [active]
    session.commit()

    assert [c.id for c in concepts_for_question(session, q.id)] == [active.id]
    # 行和别名都留着，历史统计能回溯
    assert session.scalar(select(func.count()).select_from(Concept)) == 3
    assert get_concept(session, "数学", "借位").id == merged.id
    assert get_concept(session, "数学", "凑十").id == ignored.id


def test_split_names_handles_legacy_delimiters():
    """旧知识点的多值文本：顿号/逗号/分号/斜杠/换行都切，去空去重保序。"""
    assert split_names("解方程、一元一次方程解法， 进位; 退位|凑十/数位 \n 解方程") == [
        "解方程",
        "一元一次方程解法",
        "进位",
        "退位",
        "凑十",
        "数位",
    ]
    assert split_names(" ，、 ") == []
    assert normalize_name("Keep  Cool") == "keep cool"
    assert normalize_name("　进  位　") == "进位"
