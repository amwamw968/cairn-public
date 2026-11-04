"""语音回合短指令层：停 / 跳过 / 提示 / 继续直接执行，不进闲聊；命中不了走原路由。

真机实测（2026-10-31）驱动：孩子打断喊「停一下停一下」，ASR 认成「请小声一下。」
落进 chat，回了一句「嗯，我在。」。护栏：

- 短指令在 ``route`` **之前**命中并执行；动作全部复用 ``mcp_server`` 既有函数；
- 「停」不出任何新话（流水线见了 stop 轮不进 TTS，只补 tts stop）；
- 「提示」只念 content，answer 结构上进不了 PracticeItem，测试钉死；
- 「请小声一下」这类 ASR 噪声不得误命中停；
- ``CAIRN_VOICE_INTENTS=0`` 时整层关闭，行为与从前完全一致。
"""

from __future__ import annotations

import contextlib
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
from cairn.voice import commands, turn as turn_module
from cairn.voice.turn import run_turn
from cairn.voice.xiaozhi.voice import VoicePipeline

# 固定时钟：句首的「第N题」按「今天已完成 + 1」算，不钉住就会跟着日历漂。
FIXED_NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
EXPECTED_ASK = "第一题，英语。请拼出对应的英文单词：苹果。"


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
    content: str = "苹果",
    answer: str = "apple",
) -> int:
    """建一道题 + 一条错题（新题，今天就能取到），返回 error_item_id。"""
    with store.factory() as session:
        question = Question(
            learner_id=store.learner_id,
            subject=subject,
            grade_level="primary-3",
            question_type="听写",
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


# --- 词表与匹配（含 ASR 噪声容错） ----------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("停一下停一下", commands.VoiceCommand.STOP),
        ("停一下。", commands.VoiceCommand.STOP),
        ("请先停下", commands.VoiceCommand.STOP),
        ("别说了", commands.VoiceCommand.STOP),
        ("跳过", commands.VoiceCommand.SKIP),
        ("下一题", commands.VoiceCommand.SKIP),
        ("换一题", commands.VoiceCommand.SKIP),
        ("我不会", commands.VoiceCommand.SKIP),
        ("我不会这道题", commands.VoiceCommand.SKIP),
        ("我不知道", commands.VoiceCommand.SKIP),
        ("跳过吧", commands.VoiceCommand.SKIP),
        # 闲聊不被劫持：「不知道」只是句中一部分，意图级匹配不命中。
        ("我不知道今天天气", None),
        # 「怎么办 / 怎么答」已按评审从 HINT 词表移除，正常提问不受影响。
        ("这道题我怎么办", None),
        ("怎么办", None),
        ("给我个提示", commands.VoiceCommand.HINT),
        ("给我点提示", commands.VoiceCommand.HINT),
        ("提示一下", commands.VoiceCommand.HINT),
        ("怎么说", commands.VoiceCommand.HINT),
        ("继续", commands.VoiceCommand.CONTINUE),
        ("我会了", commands.VoiceCommand.CONTINUE),
        ("知道了", commands.VoiceCommand.CONTINUE),
        # ASR 把「停一下」认成「请小声一下」：含噪声词，但「小声」不在停词表里。
        ("请小声一下。", None),
        ("今天天气怎么样", None),
        ("嗯", None),
        # 真机实测（2026-10-31）：「停一下」被 ASR 认成近音的「听一下」→ 模糊命中 STOP。
        ("听一下", commands.VoiceCommand.STOP),
        # 真机实测：「给我个提示」被完全听成「幽谷多奇石」→ 与任何关键词都差得远，不命中。
        ("幽谷多奇石", None),
    ],
)
def test_match_command_table(text, expected) -> None:
    assert commands.match_command(text) is expected


def test_fuzzy_only_on_short_sentences() -> None:
    """长句不启用模糊：句子里偶然带个近音短语不许被劫持成指令。"""
    long_text = "我不知道我今天要不要听一下音乐"
    assert len(commands._clean(long_text)) > commands.FUZZY_MAX_CHARS
    assert commands.match_command(long_text) is None


def test_fuzzy_hit_logs_recognized_and_keyword() -> None:
    """命中日志写清「听成了什么 ≈ 关键词（相似度）」，供真机日志直接定位。"""
    lines: list[str] = []
    command = commands.match_command("听一下", log=lines.append)

    assert command is commands.VoiceCommand.STOP
    assert lines == ["[cmd] 模糊命中 听一下≈停一下 (0.67)"]


def test_match_command_ignores_long_sentences() -> None:
    """长句不认指令：正常说话里偶然带个「不会」不该被劫持。"""
    long_text = "这道题老师上课讲过可是我还是不会做怎么办呢"
    assert len(long_text) > commands.MAX_COMMAND_CHARS
    assert commands.match_command(long_text) is None


def test_intents_enabled_switch(monkeypatch) -> None:
    monkeypatch.delenv(commands.INTENTS_ENV, raising=False)
    assert commands.intents_enabled() is True
    monkeypatch.setenv(commands.INTENTS_ENV, "1")
    assert commands.intents_enabled() is True
    monkeypatch.setenv(commands.INTENTS_ENV, "0")
    assert commands.intents_enabled() is False


# --- 停 -------------------------------------------------------------------


def test_stop_repeats_invoke_stop_and_never_chat(with_item) -> None:
    """AC1：打断后喊「停一下停一下」→ 停止动作被调用，不出新话、不是 chat。"""
    stopped: list[bool] = []
    with with_item.factory() as session:
        turn = run_turn(
            session,
            with_item.learner_id,
            "停一下停一下",
            clock=_clock,
            on_stop=lambda: stopped.append(True),
        )

    assert stopped == [True]
    assert turn.kind == turn_module.STOP_KIND
    assert turn.spoken == ""
    assert turn.next_step is None
    assert turn.kind != "chat"


# --- 跳过 -----------------------------------------------------------------


def test_skip_calls_next_question_and_reads_prompt(monkeypatch, with_item) -> None:
    """AC2：跳过确实调到 next_question；只有一道题时明说没有别的题，不重复念。

    （2026-10-31 改写：旧行为「单题跳过重念同一道题」正是真机实测的问题，
    新行为见 AC5——同 id 不重念，改念「没有别的题了」。换到新题仍念题干的
    断言移到 test_skip_moves_to_a_different_question。）
    """
    calls: list[int] = []
    real = mcp_server._next_question_reply

    def spy(session, learner_id, **kwargs):
        calls.append(learner_id)
        return real(session, learner_id, **kwargs)

    monkeypatch.setattr(mcp_server, "_next_question_reply", spy)
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, "跳过", clock=_clock)

    assert calls == [with_item.learner_id]
    assert turn.kind == "question"
    assert turn.spoken == turn_module.SKIP_NO_OTHER_SENTENCE
    assert "apple" not in turn.spoken


def test_wonong_means_skip(monkeypatch, with_item) -> None:
    """AC4：「我不会」= 跳过换题。

    理由（按 Lead 卡片口径测死）：与需求词表一致——孩子说「不会」就是答不出，
    留在原题只会卡死；提示与换题二选一里选换题，需要提示的孩子会明说「提示」。
    单题库下没有别的题：念「没有别的题了」，不重复念同一道。
    """
    calls: list[int] = []
    real = mcp_server._next_question_reply

    def spy(session, learner_id, **kwargs):
        calls.append(learner_id)
        return real(session, learner_id, **kwargs)

    monkeypatch.setattr(mcp_server, "_next_question_reply", spy)
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, "我不会", clock=_clock)

    assert calls == [with_item.learner_id]
    assert turn.kind == "question"
    assert turn.spoken == turn_module.SKIP_NO_OTHER_SENTENCE
    assert "apple" not in turn.spoken


def test_wobudao_weather_falls_through_to_chat(with_item) -> None:
    """评审阻塞项：「我不知道今天天气」是闲聊，不许被「不知道」劫持成换题。"""
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, "我不知道今天天气", clock=_clock)

    assert turn.kind == "chat"
    assert turn.spoken == turn_module.CHAT_REPLY


def test_wonot_this_question_means_skip(monkeypatch, with_item) -> None:
    """「我不会这道题」：动作短语（我不会）开头 + 对象尾巴，仍命中 SKIP。"""
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, "我不会这道题", clock=_clock)

    assert turn.kind == "question"
    assert turn.spoken == turn_module.SKIP_NO_OTHER_SENTENCE
    assert "apple" not in turn.spoken


def test_skip_moves_to_a_different_question(monkeypatch, store) -> None:
    """题库里有下一道时，「跳过」念的是**另一道**题的题干提示，answer 不出现。"""
    add_item(store, content="苹果", answer="apple")
    add_item(store, content="香蕉", answer="banana")
    with store.factory() as session:
        turn = run_turn(session, store.learner_id, "跳过", clock=_clock)

    assert turn.kind == "question"
    assert turn.spoken == "第一题，英语。请拼出对应的英文单词：香蕉。"
    assert "香蕉" in turn.spoken
    assert "苹果" not in turn.spoken
    assert "apple" not in turn.spoken
    assert "banana" not in turn.spoken


# --- 提示 -----------------------------------------------------------------


def test_hint_reads_content_and_never_answer(with_item) -> None:
    """AC3：提示只念 content；answer 一个字都不许出现。"""
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, "给我个提示", clock=_clock)

    assert turn.kind == turn_module.HINT_KIND
    assert turn.spoken == f"{turn_module.HINT_PREFIX}苹果"
    assert "苹果" in turn.spoken
    assert "apple" not in turn.spoken


def test_geiwodian_tishi_reads_content_and_never_answer(with_item) -> None:
    """「给我点提示」（评审收敛后的 HINT 词表）命中提示，answer 一个字都不出现。"""
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, "给我点提示", clock=_clock)

    assert turn.kind == turn_module.HINT_KIND
    assert "苹果" in turn.spoken
    assert "apple" not in turn.spoken


def test_hint_without_current_question_says_no_due(store) -> None:
    """空库要提示：念工具的「今天的题都做完了」，不编题。"""
    with store.factory() as session:
        turn = run_turn(session, store.learner_id, "提示一下", clock=_clock)

    assert turn.kind == "no_question"
    assert turn.spoken == mcp_server.NO_DUE_SENTENCE


# --- 继续 -----------------------------------------------------------------


def test_continue_unanswered_rereads_prompt(with_item) -> None:
    """继续（还没答）：把当前题的题干提示再念一遍，不出新题。"""
    with with_item.factory() as session:
        turn = run_turn(session, with_item.learner_id, "我会了", clock=_clock)

    assert turn.kind == turn_module.CONTINUE_KIND
    assert turn.spoken == EXPECTED_ASK


def test_continue_after_answered_moves_on(store) -> None:
    """继续（已答）：当前题出了池子，接着出下一题；没题就明说做完了。"""
    item_id = add_item(store)
    with store.factory() as session:
        mcp_server.submit_answer(
            session, store.learner_id, item_id, "apple", clock=_clock
        )
        turn = run_turn(session, store.learner_id, "继续", clock=_clock)

    assert turn.kind == "no_question"
    assert turn.spoken == mcp_server.NO_DUE_SENTENCE


# --- 回归与开关 ------------------------------------------------------------


def test_weather_falls_through_to_chat(with_item) -> None:
    """AC5：不命中任何指令的原话照走原路由（chat），停止动作也不被触发。"""
    stopped: list[bool] = []
    with with_item.factory() as session:
        turn = run_turn(
            session,
            with_item.learner_id,
            "今天天气怎么样",
            clock=_clock,
            on_stop=lambda: stopped.append(True),
        )

    assert turn.kind == "chat"
    assert turn.spoken == turn_module.CHAT_REPLY
    assert stopped == []


def test_env_switch_off_restores_original_routing(monkeypatch, with_item) -> None:
    """AC6：``CAIRN_VOICE_INTENTS=0`` → 短指令层整体关闭，全部走原路由。"""
    monkeypatch.setenv(commands.INTENTS_ENV, "0")
    stopped: list[bool] = []

    with with_item.factory() as session:
        stop_turn = run_turn(
            session,
            with_item.learner_id,
            "停一下停一下",
            clock=_clock,
            on_stop=lambda: stopped.append(True),
        )
        hint_turn = run_turn(session, with_item.learner_id, "给我个提示", clock=_clock)

    # 「停一下」退回 chat；「给我个提示」原路由没认出的也是 chat。
    assert stop_turn.kind == "chat"
    assert stop_turn.spoken == turn_module.CHAT_REPLY
    assert stopped == []
    assert hint_turn.kind == "chat"
    assert hint_turn.spoken == turn_module.CHAT_REPLY


# --- 流水线：stop 轮不进 TTS ----------------------------------------------


def test_pipeline_stop_turn_sends_no_audio_only_tts_stop() -> None:
    """流水线见到 stop 轮：不合成、不推帧，只发一条 tts stop 把板子拉回 idle。"""
    import asyncio

    from cairn.voice.xiaozhi.ws import DOWNLINK_PARAMS

    sent: list[object] = []

    async def sender(item) -> None:
        sent.append(item)

    def boom_synthesize(text: str) -> bytes:
        raise AssertionError("stop 轮不许进 TTS")

    pipeline = VoicePipeline(
        session_factory=lambda: contextlib.nullcontext(SimpleNamespace()),
        transcriber=lambda wav: "停一下停一下",
        responder=lambda session, learner_id, text: turn_module.Turn(
            kind=turn_module.STOP_KIND, spoken="", next_step=None
        ),
        synthesizer=boom_synthesize,
        log=lambda message: None,
        clock=lambda: 0.0,
    )
    utterance = SimpleNamespace(seconds=1.0, packets=10, decode_errors=0, wav=b"x" * 32)

    stats = asyncio.run(
        pipeline.handle(utterance, DOWNLINK_PARAMS, sender, session_id="s1")
    )

    assert stats.kind == "command_stop"
    assert stats.frames == 0
    assert [item for item in sent if isinstance(item, bytes)] == []
    stop_messages = [
        item
        for item in sent
        if isinstance(item, dict)
        and item.get("type") == "tts"
        and item.get("state") == "stop"
    ]
    assert len(stop_messages) == 1
