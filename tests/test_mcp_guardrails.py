"""卡 B：反幻觉约束（Rui 零改动，约束全在服务端）。

生产事故：Rui 第二轮起不再调工具，自己编题、自己宣布「答对了」，库里却只有 1 条 review。
本文件钉住三层防线：initialize 的 instructions、四个工具的 description、以及**每次工具
返回的 structuredContent.next_step**（这层不依赖客户端配合）。

同时钉住「约束不能进 content 文本」——那是 Rui 的 TTS 要念给孩子听的。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from mcp.client.client import Client
from mcp.types import TextContent

from cairn import mcp_server
from cairn.mcp_server import NO_DUE_SENTENCE, NO_ITEM_SENTENCE
from cairn.models import (
    REVIEW_STATUS_NEW,
    REVIEW_STATUS_REVIEWING,
    ErrorItem,
    Learner,
    Question,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.models.learner import utcnow

STEM = "小明有 3 个苹果，妈妈又给了他 5 个，一共有几个？"
ANALYSIS = "先把 3 和 5 加起来。"


@pytest.fixture()
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'cairn.db'}")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        learner = Learner(name="小明")
        session.add(learner)
        session.commit()
        learner_id = learner.id
    try:
        yield SimpleNamespace(factory=factory, learner_id=learner_id, path=tmp_path)
    finally:
        engine.dispose()


def add_item(store, *, due: bool = True, answer: str = "8") -> int:
    """建一道题 + 一条错题，返回 error_item_id（due=True 为今天到期）。"""
    with store.factory() as session:
        question = Question(
            learner_id=store.learner_id,
            subject="数学",
            grade_level="primary-3",
            question_type="应用题",
            content=STEM,
            answer=answer,
            analysis=ANALYSIS,
        )
        session.add(question)
        session.commit()
        item = ErrorItem(
            learner_id=store.learner_id,
            question_id=question.id,
            review_status=REVIEW_STATUS_REVIEWING if due else REVIEW_STATUS_NEW,
            next_due_at=utcnow() - timedelta(minutes=5) if due else None,
            last_reviewed_at=utcnow() - timedelta(days=1) if due else None,
        )
        session.add(item)
        session.commit()
        return item.id


def run_async(coro):
    return asyncio.run(coro)


# --- AC1 initialize 的 instructions ------------------------------------


def test_instructions_constant_has_all_hard_rules():
    text = mcp_server.SERVER_INSTRUCTIONS
    assert "出题只能调用 next_question" in text
    assert "判分只能调用 submit_answer" in text
    assert "禁止自拟题目" in text
    assert "不要说「答对了」" in text
    assert "工具说没有题，就直接告诉孩子现在没有题" in text


def test_initialize_returns_instructions(store):
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return client.instructions

    instructions = run_async(scenario())
    assert instructions == mcp_server.SERVER_INSTRUCTIONS
    assert "next_question" in instructions and "submit_answer" in instructions


# --- AC2 工具 description ----------------------------------------------


def test_tool_descriptions_have_unique_entry_constraint(store):
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return await client.list_tools()

    tools = {tool.name: tool for tool in run_async(scenario()).tools}
    assert "唯一合法的出题入口" in tools["next_question"].description
    assert "禁止自己编题" in tools["next_question"].description
    assert "唯一合法的判分入口" in tools["submit_answer"].description
    assert "不要说答对了" in tools["submit_answer"].description
    assert "唯一合法" in tools["today_progress"].description
    assert "唯一合法" in tools["bank_overview"].description


# --- AC3 返回里带约束，但朗读文本保持干净 -------------------------------


def test_next_question_next_step_stays_out_of_spoken_text(store):
    add_item(store)
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return await client.call_tool("next_question", {})

    result = run_async(scenario())
    spoken = result.content[0].text
    assert isinstance(result.content[0], TextContent)
    assert spoken == f"第一题，数学。{STEM}"
    # 给孩子听的那句话里不能出现工具名或规则字样（TTS 会整段念出来）
    for banned in ("submit_answer", "next_question", "next_step", "必须", "调用"):
        assert banned not in spoken

    structured = result.structured_content
    assert structured["spoken_text"] == spoken
    assert structured["kind"] == "question"
    assert structured["next_step"] == mcp_server.QUESTION_NEXT_STEP
    assert "submit_answer" in structured["next_step"]
    assert "再次调用 next_question" in structured["next_step"]
    assert "不要自己说答对了" in structured["next_step"]


def test_submit_answer_next_step_wins_over_model_claim(store):
    item_id = add_item(store)
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return await client.call_tool(
                "submit_answer", {"error_item_id": item_id, "learner_answer": "八"}
            )

    result = run_async(scenario())
    assert result.content[0].text == "答对了"
    structured = result.structured_content
    assert structured["kind"] == "correct"
    assert structured["error_item_id"] == item_id
    assert structured["next_step"] == mcp_server.GRADED_NEXT_STEP
    assert "判分结果以本次返回为准" in structured["next_step"]


def test_submit_answer_wrong_verdict_is_tagged(store):
    item_id = add_item(store)
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return await client.call_tool(
                "submit_answer", {"error_item_id": item_id, "learner_answer": "9"}
            )

    result = run_async(scenario())
    assert result.content[0].text.startswith("再想想，提示：")
    assert result.structured_content["kind"] == "wrong"


# --- AC4 客户端忽略 instructions，约束仍在 ------------------------------


def test_constraints_survive_client_ignoring_instructions(store, monkeypatch):
    """模拟「客户端完全不读 initialize 的 instructions」：只调工具。"""
    item_id = add_item(store)
    # 把客户端读取 instructions 的能力彻底拿掉（等价于客户端把它丢了）
    monkeypatch.setattr(Client, "instructions", property(lambda self: None))
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            assert client.instructions is None
            asked = await client.call_tool("next_question", {})
            graded = await client.call_tool(
                "submit_answer", {"error_item_id": item_id, "learner_answer": "8"}
            )
            return asked, graded

    asked, graded = run_async(scenario())
    assert "submit_answer" in asked.structured_content["next_step"]
    assert "再次调用 next_question" in asked.structured_content["next_step"]
    assert "本次返回为准" in graded.structured_content["next_step"]


# --- AC5 没题时明说没有 -------------------------------------------------


def test_no_question_next_step_forbids_inventing(store):
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return await client.call_tool("next_question", {})

    result = run_async(scenario())
    assert result.content[0].text == NO_DUE_SENTENCE
    assert result.structured_content["kind"] == "no_question"
    assert result.structured_content["error_item_id"] is None
    assert "不要自己出题" in result.structured_content["next_step"]


def test_unknown_item_never_says_correct(store):
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return await client.call_tool(
                "submit_answer", {"error_item_id": 404, "learner_answer": "8"}
            )

    result = run_async(scenario())
    assert result.content[0].text == NO_ITEM_SENTENCE
    assert result.structured_content["kind"] == "no_item"
    assert "不要说答对了" in result.structured_content["next_step"]


# --- 兜底：每个工具都要带 next_step -------------------------------------


def test_every_tool_returns_next_step(store):
    add_item(store)
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return [
                await client.call_tool(name, {})
                for name in ("next_question", "today_progress", "bank_overview")
            ]

    for result in run_async(scenario()):
        structured = result.structured_content
        assert structured is not None
        assert structured["spoken_text"] == result.content[0].text
        assert structured["next_step"].strip()
        assert "next_question" in structured["next_step"]
