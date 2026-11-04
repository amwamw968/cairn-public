"""M1 一轮对话：四个意图各走各的工具、chat 真不走、spoken 里没有 next_step。

护栏对应卡片的三个「关键」与评审的三条 CHANGES：
- 要题 / 进度 / 题库必须各自调到 ``cairn.mcp_server`` 里对应的那个函数；
- 判分 M1 没接，必须明说「判分还没接」，**不许静默换成一道新题**；
- chat 分支不调工具；``spoken`` 与 ``next_step`` 是两份东西；
- 库坏了给退出码 1 不裸抛栈；单轮出错不打断会话。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cairn import mcp_server
from cairn.models import (
    REVIEW_STATUS_NEW,
    ErrorItem,
    Learner,
    Question,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.voice import cli as cli_module
from cairn.voice import turn as turn_module
from cairn.voice.turn import run_turn

# 固定时钟：句首的「第N题」按「今天已完成 + 1」算，不钉住就会跟着日历漂。
FIXED_NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
ASK_ENGLISH = "给我出一道英语题"
EXPECTED_ASK = "第一题，英语。请拼出对应的英文单词：苹果。"
ASK_PROGRESS = "还剩几道"
ASK_BANK = "题库里有多少题"
ASK_GRADE = "这道题对不对"


def _clock() -> datetime:
    return FIXED_NOW


@pytest.fixture()
def store(tmp_path):
    """文件库 + 一个孩子 + 一道英语听写题（MCP 每次调用另开 Session，不能用内存库）。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'cairn.db'}")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        learner = Learner(name="小明", grade_level="primary-3")
        session.add(learner)
        session.commit()
        learner_id = learner.id
    try:
        yield SimpleNamespace(factory=factory, learner_id=learner_id, path=tmp_path)
    finally:
        engine.dispose()


def add_item(
    store,
    *,
    subject: str = "英语",
    question_type: str = "听写",
    content: str = "苹果",
    answer: str = "apple",
) -> int:
    """建一道题 + 一条错题（新题，今天就能取到），返回 error_item_id。"""
    with store.factory() as session:
        question = Question(
            learner_id=store.learner_id,
            subject=subject,
            grade_level="primary-3",
            question_type=question_type,
            content=content,
            answer=answer,
        )
        session.add(question)
        session.commit()
        item = ErrorItem(
            learner_id=store.learner_id,
            question_id=question.id,
            review_status=REVIEW_STATUS_NEW,
        )
        session.add(item)
        session.commit()
        return item.id


@pytest.fixture()
def with_item(store):
    add_item(store)
    return store


def test_practice_goes_through_tool(monkeypatch, with_item) -> None:
    """practice 分支确实调到工具，且把话里的学科带了过去。"""
    calls: list[str | None] = []
    real = mcp_server._next_question_reply

    def spy(session, learner_id, **kwargs):
        calls.append(kwargs.get("subject"))
        return real(session, learner_id, **kwargs)

    monkeypatch.setattr(mcp_server, "_next_question_reply", spy)
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, ASK_ENGLISH, clock=_clock)

    assert calls == ["英语"]
    assert turn.kind == "question"


def test_practice_spoken_equals_tool_output(with_item) -> None:
    """念出来的句子与 MCP 工具逐字相同：voice 层没有第二套话术。"""
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, ASK_ENGLISH, clock=_clock)
        tool_said = mcp_server.next_question(
            session, with_item.learner_id, subject="英语", clock=_clock
        )
    assert turn.spoken == tool_said == EXPECTED_ASK


def test_spoken_excludes_next_step(with_item) -> None:
    """念给孩子的话里不许出现给模型的约束（那会被 TTS 念成噪音）。"""
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, ASK_ENGLISH, clock=_clock)

    assert turn.next_step == mcp_server.QUESTION_NEXT_STEP
    assert turn.next_step not in turn.spoken
    for token in ("submit_answer", "next_question", "必须", "不要", "不得"):
        assert token not in turn.spoken


def test_chat_does_not_call_tool(monkeypatch, with_item) -> None:
    """闲聊分支不碰工具：工具被换成「一调就抛」，这一轮照样走完。"""

    def boom(*args, **kwargs):
        raise AssertionError("chat 分支不许调工具")

    monkeypatch.setattr(mcp_server, "_next_question_reply", boom)
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, "嗯", clock=_clock)

    assert turn.kind == "chat"
    assert turn.next_step is None
    assert turn.spoken == turn_module.CHAT_REPLY


def test_practice_empty_bank_says_no_question(store) -> None:
    """没题时念的是工具原话，约束也来自工具——voice 不改写。"""
    with store.factory() as session:
        turn = run_turn(session, store.learner_id, "下一题", clock=_clock)

    assert turn.kind == "no_question"
    assert turn.spoken == mcp_server.NO_DUE_SENTENCE
    assert turn.next_step == mcp_server.NO_QUESTION_NEXT_STEP


def test_cli_prints_spoken_and_constraint_separately(tmp_path) -> None:
    """真起子进程：一道英语题 + 一句「嗯」，两行分开。

    管道输入不会回显，所以提示符与回答会挤在同一行；断言按「子串」做，
    只看两件事：念的话带说话人标签、约束单独一行。
    """
    db_path = tmp_path / "cli.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        learner = Learner(name="小明", grade_level="primary-3")
        session.add(learner)
        session.commit()
        question = Question(
            learner_id=learner.id,
            subject="英语",
            grade_level="primary-3",
            question_type="听写",
            content="苹果",
            answer="apple",
        )
        session.add(question)
        session.commit()
        session.add(
            ErrorItem(
                learner_id=learner.id,
                question_id=question.id,
                review_status=REVIEW_STATUS_NEW,
            )
        )
        session.commit()
    engine.dispose()

    env = {**os.environ, "CAIRN_DB_URL": f"sqlite:///{db_path}"}
    proc = subprocess.run(
        [sys.executable, "-m", "cairn.voice"],
        input=f"{ASK_ENGLISH}\n嗯\nexit\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    spoken_lines = [line for line in lines if "Cairn>" in line]
    assert spoken_lines[0].endswith(f"Cairn> {EXPECTED_ASK}")
    assert spoken_lines[1].endswith(f"Cairn> {turn_module.CHAT_REPLY}")

    ask_index = lines.index(spoken_lines[0])
    assert lines[ask_index + 1].startswith("[约束] ")
    assert "[约束]" in lines[ask_index + 1]

    chat_index = lines.index(spoken_lines[1])
    following = lines[chat_index + 1 :]
    assert not any(line.startswith("[约束] ") for line in following)


def test_progress_intent_asks_tool_for_progress(with_item) -> None:
    """「还剩几道」念的是进度那句话，与 today_progress 逐字相同，不是一道新题。"""
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, ASK_PROGRESS, clock=_clock)
        tool_said = mcp_server.today_progress(session, with_item.learner_id, clock=_clock)

    assert turn.kind == turn_module.PROGRESS_KIND
    assert turn.spoken == tool_said
    assert "还剩" in turn.spoken
    assert "请拼出" not in turn.spoken
    assert turn.next_step == mcp_server.PROGRESS_NEXT_STEP


def test_bank_intent_asks_tool_for_overview(with_item) -> None:
    """「题库里有多少题」念的是题库概况，不是一道新题。"""
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, ASK_BANK, clock=_clock)
        tool_said = mcp_server.bank_overview(session, with_item.learner_id, clock=_clock)

    assert turn.kind == turn_module.OVERVIEW_KIND
    assert turn.spoken == tool_said
    assert turn.spoken.startswith("题库里一共 1 道错题")
    assert "请拼出" not in turn.spoken
    assert turn.next_step == mcp_server.OVERVIEW_NEXT_STEP


def test_grade_intent_says_not_wired_and_never_returns_question(
    monkeypatch, with_item
) -> None:
    """判分 M1 没接：明说「判分还没接」，且这条路上取题函数一次都不许被调。"""

    def boom(*args, **kwargs):
        raise AssertionError("判分路径不许取新题")

    monkeypatch.setattr(mcp_server, "_next_question_reply", boom)
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, ASK_GRADE, clock=_clock)

    assert turn.kind == turn_module.GRADING_KIND
    assert turn.spoken == turn_module.GRADING_NOT_WIRED
    assert "判分还没接" in turn.spoken
    # 不是题：不出题、不复述题干、不下判分结论。
    assert "请拼出" not in turn.spoken
    assert "苹果" not in turn.spoken
    assert "答对" not in turn.spoken
    assert turn.next_step is not None
    assert "判分" in turn.next_step
    assert turn.next_step not in turn.spoken


def test_awaiting_answer_does_not_return_new_question(monkeypatch, with_item) -> None:
    """上一轮在等作答、孩子说了句「嗯嗯」：也按判分处理，不许换一道新题。"""

    def boom(*args, **kwargs):
        raise AssertionError("等作答时不许取新题")

    monkeypatch.setattr(mcp_server, "_next_question_reply", boom)
    with with_item.factory() as session:
        turn = run_turn(
            session, with_item.learner_id, "嗯嗯", awaiting_answer=True, clock=_clock
        )

    assert turn.kind == turn_module.GRADING_KIND
    assert turn.spoken == turn_module.GRADING_NOT_WIRED


def test_cli_db_unavailable_exits_1_without_traceback(tmp_path) -> None:
    """库打不开：一句「数据库不可用」，退出码 1，不把栈甩给用户。"""
    bad_path = tmp_path / "no-such-dir" / "cairn.db"
    proc = subprocess.run(
        [sys.executable, "-m", "cairn.voice", "--db-url", f"sqlite:///{bad_path}"],
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert proc.returncode == 1, proc.stderr
    assert cli_module.DB_UNAVAILABLE_PREFIX in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "Traceback" not in proc.stdout


def test_cli_keeps_going_after_one_turn_fails(monkeypatch, with_item, capsys) -> None:
    """某一轮抛异常：报「这一轮没处理好」，然后接着听下一句，会话不崩。"""
    real_run_turn = cli_module.run_turn
    seen: list[str] = []

    def flaky(session, learner_id, text, **kwargs):
        seen.append(text)
        if len(seen) == 1:
            raise RuntimeError("模型抽风")
        return real_run_turn(session, learner_id, text, **kwargs)

    monkeypatch.setattr(cli_module, "run_turn", flaky)
    lines = iter([ASK_ENGLISH, ASK_ENGLISH, "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    exit_code = cli_module.main(["--db-url", f"sqlite:///{with_item.path / 'cairn.db'}"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert seen == [ASK_ENGLISH, ASK_ENGLISH]
    assert f"{cli_module.TURN_FAILED_PREFIX}模型抽风" in captured.err
    assert "Traceback" not in captured.err
    assert f"{cli_module.SPEAKER}{EXPECTED_ASK}" in captured.out
