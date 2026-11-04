"""0.2 卡12：Cairn 的 MCP 工具服务。

不依赖 Rui、不联网：库里是中文样例数据，MCP 侧用 mcp 2.x 的 client 对
内存 transport 跑一次（`Client(server)` 不监听端口）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from mcp.client.client import Client
from mcp.types import TextContent

from cairn import mcp_server
from cairn.mcp_server import (
    NO_DUE_SENTENCE,
    NO_ITEM_SENTENCE,
    ToolState,
    normalize_answer,
    today_progress,
)
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
from cairn.models.review import Review

STEM = "小明有 3 个苹果，妈妈又给了他 5 个，一共有几个？"
ANALYSIS = "先把 3 和 5 加起来。"


@pytest.fixture()
def store(tmp_path):
    """文件库 + 一个 learner：MCP 每次调用单开 Session，所以不能用纯内存库。"""
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


def add_item(
    store,
    *,
    subject: str = "数学",
    question_type: str = "应用题",
    content: str = STEM,
    answer: str | None = "8",
    analysis: str | None = ANALYSIS,
    due_at: datetime | None = None,
    confidence: float | None = None,
) -> int:
    """建一道题 + 一条错题，返回 error_item_id（due_at=None 视为新题）。"""
    with store.factory() as session:
        question = Question(
            learner_id=store.learner_id,
            subject=subject,
            grade_level="primary-3",
            question_type=question_type,
            content=content,
            answer=answer,
            analysis=analysis,
        )
        session.add(question)
        session.commit()
        item = ErrorItem(
            learner_id=store.learner_id,
            question_id=question.id,
            review_status=REVIEW_STATUS_NEW if due_at is None else REVIEW_STATUS_REVIEWING,
            next_due_at=due_at,
            # 已排期的题必然复习过；新题两者都为空（线上新录错题就是这个形状）。
            last_reviewed_at=None if due_at is None else due_at - timedelta(days=1),
            confidence=confidence,
        )
        session.add(item)
        session.commit()
        return item.id


def call_next(store, **kwargs) -> str:
    with store.factory() as session:
        return mcp_server.next_question(session, store.learner_id, **kwargs)


def call_submit(store, item_id: int, answer: str, **kwargs) -> str:
    with store.factory() as session:
        return mcp_server.submit_answer(
            session, store.learner_id, item_id, answer, **kwargs
        )


def call_progress(store, **kwargs) -> str:
    with store.factory() as session:
        return today_progress(session, store.learner_id, **kwargs)


def call_overview(store, **kwargs) -> str:
    with store.factory() as session:
        return mcp_server.bank_overview(session, store.learner_id, **kwargs)


# --- AC2 取题话术 -------------------------------------------------------


def test_next_question_reads_stem_verbatim(store):
    add_item(store)
    sentence = call_next(store)
    assert sentence == f"第一题，数学。{STEM}"
    # 题干里的数字与单位一个都不能少
    assert "3 个苹果" in sentence and "5 个" in sentence


def test_next_question_without_subject_uses_stored_subject(store):
    add_item(store, subject="语文", content="chūn tiān", answer="春天", analysis="一横一横…")
    assert call_next(store) == "第一题，语文。请写出对应的汉字：chūn tiān。"


def test_next_question_subject_filter(store):
    add_item(store, subject="数学")
    add_item(store, subject="英语", question_type="单词", content="未来", answer="future")
    assert call_next(store, subject="英语").startswith("第一题，英语。请拼出对应的英文单词：未来")
    assert call_next(store, subject="数学").startswith("第一题，数学。")
    # 英文别名也认
    assert call_next(store, subject="math").startswith("第一题，数学。")


def test_next_question_english_prompt(store):
    add_item(
        store,
        subject="英语",
        question_type="单词",
        content="将来；未来",
        answer="future",
        analysis=None,
    )
    sentence = call_next(store)
    assert sentence == "第一题，英语。请拼出对应的英文单词：将来；未来。"
    assert "future" not in sentence  # 答案不出现在取题句里


def test_next_question_chinese_prompt(store):
    add_item(store, subject="语文", content="chūn tiān", answer="春天", analysis=None)
    sentence = call_next(store)
    assert sentence == "第一题，语文。请写出对应的汉字：chūn tiān。"
    assert "春天" not in sentence  # 答案不出现在取题句里


def test_next_question_nothing_due(store):
    assert call_next(store) == NO_DUE_SENTENCE == "今天的题都做完了"
    assert call_next(store, subject="数学") == NO_DUE_SENTENCE


def test_next_question_ignores_future_due(store):
    add_item(store, due_at=utcnow() + timedelta(days=3))
    assert call_next(store) == NO_DUE_SENTENCE


def test_next_question_number_follows_completed_today(store):
    first = add_item(store, content="1 + 1 = ?", answer="2")
    add_item(store, content="2 + 2 = ?", answer="4")
    assert call_submit(store, first, "二") == "答对了"
    assert call_next(store) == "第二题，数学。2 + 2 = ?"


def test_next_question_remembers_what_it_asked(store):
    add_item(store)
    state = ToolState()
    call_next(store, state=state)
    with store.factory() as session:
        asked = session.scalars(select(ErrorItem)).one()
    assert state.last_error_item_id == asked.id


# --- AC3 判分话术 + 四档 rating ----------------------------------------


def test_submit_correct_says_answered_and_grades_good(store):
    item_id = add_item(store)
    assert call_submit(store, item_id, "八") == "答对了"
    with store.factory() as session:
        review = session.scalars(select(Review)).one()
        item = session.get(ErrorItem, item_id)
    assert review.rating == "good"
    assert review.error_item_id == item_id
    assert item.review_status == "reviewing"  # 首次评分 new -> reviewing
    assert item.stability is not None and item.next_due_at is not None


def test_submit_wrong_gives_hint_and_grades_again(store):
    item_id = add_item(store)
    sentence = call_submit(store, item_id, "9")
    assert sentence == f"再想想，提示：{ANALYSIS}"
    with store.factory() as session:
        review = session.scalars(select(Review)).one()
        item = session.get(ErrorItem, item_id)
    assert review.rating == "again"
    assert item.wrong_count == 2  # 建卡时默认 1，again +1
    assert item.last_wrong_at is not None


def test_submit_wrong_without_analysis_uses_default_hint(store):
    item_id = add_item(store, analysis=None)
    assert call_submit(store, item_id, "9") == f"再想想，提示：{mcp_server.DEFAULT_HINT}"


def test_submit_accepts_spoken_number_with_measure_word(store):
    item_id = add_item(store, content="一共有几个苹果？", answer="8")
    assert call_submit(store, item_id, "八个") == "答对了"


def test_submit_falls_back_to_last_asked_item(store):
    item_id = add_item(store)
    state = ToolState()
    call_next(store, state=state)
    # 模型拿不到 id，编了一个 999
    assert call_submit(store, 999, "8", state=state) == "答对了"
    with store.factory() as session:
        assert session.scalars(select(Review)).one().error_item_id == item_id


def test_submit_other_learners_item_is_not_graded(store):
    with store.factory() as session:
        stranger = Learner(name="别人")
        session.add(stranger)
        session.commit()
        stranger_id = stranger.id
        question = Question(
            learner_id=stranger_id,
            subject="数学",
            grade_level="primary-3",
            question_type="应用题",
            content="别人的题",
            answer="1",
        )
        session.add(question)
        session.commit()
        foreign = ErrorItem(learner_id=stranger_id, question_id=question.id)
        session.add(foreign)
        session.commit()
        foreign_id = foreign.id
    assert call_submit(store, foreign_id, "1") == NO_ITEM_SENTENCE
    with store.factory() as session:
        assert session.scalars(select(Review)).all() == []


def test_submit_unknown_item_without_state(store):
    assert call_submit(store, 999, "8") == NO_ITEM_SENTENCE


def test_submit_empty_answer_is_not_graded(store):
    item_id = add_item(store)
    assert call_submit(store, item_id, "  ") == "我还没听清你的答案，再说一遍好吗"
    with store.factory() as session:
        assert session.scalars(select(Review)).all() == []



# --- AC4 中文数字归一化 -------------------------------------------------


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("八", "8"),
        ("十五", "15"),
        ("一点五", "1.5"),
        ("一百", "100"),
        ("一百零五", "105"),
        ("二十", "20"),
        ("１５", "15"),
    ],
)
def test_normalize_answer(spoken, expected):
    assert normalize_answer(spoken) == expected


def test_normalize_answer_makes_spoken_and_written_equal():
    assert normalize_answer("八") == normalize_answer("8")
    assert normalize_answer(" 8 个。") == normalize_answer("8")
    assert normalize_answer("一点五") == normalize_answer("1.5")
    assert normalize_answer("8") != normalize_answer("9")


# --- AC5 进度话术 -------------------------------------------------------


def test_today_progress_counts_plan_done_remaining(store):
    due = utcnow() - timedelta(minutes=5)
    ids = [add_item(store, content=f"{i} + {i} = ?", answer=str(i * 2), due_at=due) for i in (1, 2, 3, 4, 5, 6)]
    for item_id, spoken in zip(ids[:2], ("2", "4")):
        assert call_submit(store, item_id, spoken) == "答对了"
    assert call_progress(store, clock=utcnow) == "今天可练 6 道，已完成 2 道，还剩 4 道。"


def test_today_progress_new_items_count_as_playable(store):
    """新录的题 next_due_at 为空，也必须算进今天可练（线上漏报的就是这一支）。"""
    for i in (1, 2, 3):
        add_item(store, content=f"{i} + {i} = ?", answer=str(i * 2))
    sentence = call_progress(store)
    assert sentence == "今天可练 3 道，已完成 0 道，还剩 3 道。"
    assert "还没有安排题目" not in sentence


def test_today_progress_after_one_review(store):
    ids = [
        add_item(store, content=f"{i} + {i} = ?", answer=str(i * 2))
        for i in (1, 2, 3)
    ]
    assert call_submit(store, ids[0], "2") == "答对了"
    assert call_progress(store) == "今天可练 3 道，已完成 1 道，还剩 2 道。"
    assert call_submit(store, ids[1], "4") == "答对了"
    assert call_progress(store) == "今天可练 3 道，已完成 2 道，还剩 1 道。"


def test_today_progress_empty_bank_vs_all_done(store):
    empty = call_progress(store)
    assert empty == "题库里还没有错题"

    item_id = add_item(store)
    assert call_submit(store, item_id, "八") == "答对了"
    finished = call_progress(store)
    assert finished == "今天的题都做完了"
    assert empty != finished


def test_today_progress_future_only_is_done_for_today(store):
    add_item(store, due_at=utcnow() + timedelta(days=3))
    assert call_progress(store) == "今天的题都做完了"


# --- AC6 工具注册与参数 schema（内存 transport）-------------------------


def run_async(coro):
    return asyncio.run(coro)


def test_tools_registered_with_expected_schema(store):
    add_item(store)
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return await client.list_tools()

    tools = {tool.name: tool for tool in run_async(scenario()).tools}
    assert set(tools) == {
        "next_question",
        "submit_answer",
        "today_progress",
        "bank_overview",
    }

    subject = tools["next_question"].input_schema["properties"]["subject"]
    assert {"string", "null"} == {branch["type"] for branch in subject["anyOf"]}

    submitted = tools["submit_answer"].input_schema
    assert set(submitted["properties"]) == {"error_item_id", "learner_answer"}
    assert submitted["properties"]["error_item_id"]["type"] == "integer"
    assert submitted["properties"]["learner_answer"]["type"] == "string"
    assert set(submitted["required"]) == {"error_item_id", "learner_answer"}

    assert tools["today_progress"].input_schema["properties"] == {}
    assert tools["bank_overview"].input_schema["properties"] == {}
    assert all(tool.description for tool in tools.values())


def test_call_tool_returns_one_spoken_line(store):
    item_id = add_item(store)
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            asked = await client.call_tool("next_question", {})
            graded = await client.call_tool(
                "submit_answer",
                {"error_item_id": item_id, "learner_answer": "八"},
            )
            progress = await client.call_tool("today_progress", {})
        return asked, graded, progress

    asked, graded, progress = run_async(scenario())
    for result in (asked, graded, progress):
        assert result.is_error is False
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextContent)

    assert asked.content[0].text == f"第一题，数学。{STEM}"
    assert graded.content[0].text == "答对了"
    assert progress.content[0].text == "今天的题都做完了"


def test_call_tool_fallback_when_nothing_asked(store):
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return await client.call_tool(
                "submit_answer", {"error_item_id": 404, "learner_answer": "8"}
            )

    result = run_async(scenario())
    assert result.content[0].text == NO_ITEM_SENTENCE


# --- 卡15 题库概况（bank_overview）------------------------------------


def test_bank_overview_empty(store):
    sentence = call_overview(store)
    assert "还没有错题" in sentence
    assert sentence == mcp_server.EMPTY_BANK_SENTENCE
    assert "0 道" not in sentence


def test_bank_overview_counts_by_subject(store):
    add_item(store, subject="数学", content="1 + 1 = ?", answer="2")
    add_item(store, subject="数学", content="2 + 2 = ?", answer="4")
    add_item(
        store,
        subject="英语",
        question_type="单词",
        content="apple",
        answer="苹果",
        analysis=None,
    )
    sentence = call_overview(store)
    assert "题库里一共 3 道错题" in sentence
    assert "数学 2 道" in sentence
    assert "英语 1 道" in sentence
    assert "语文" not in sentence  # 没有该学科就不报
    assert "没有待确认的题。" in sentence  # confidence 为空的题不算待复核


def test_bank_overview_pending_count(store):
    add_item(store, content="1 + 1 = ?", answer="2", confidence=0.5)
    add_item(store, content="2 + 2 = ?", answer="4", confidence=0.5)
    add_item(store, content="3 + 3 = ?", answer="6")  # 已确认，不计入
    sentence = call_overview(store)
    assert "另有 2 道待你确认。" in sentence


def test_bank_overview_today_due_and_completed(store):
    due = utcnow() - timedelta(minutes=5)
    ids = [
        add_item(store, content=f"{i} + {i} = ?", answer=str(i * 2), due_at=due)
        for i in (1, 2, 3)
    ]
    assert call_submit(store, ids[0], "2") == "答对了"
    sentence = call_overview(store)
    # 已作答的那道 next_due_at 已推到将来，今天到期只剩 2 道（与 today_progress 同源）
    assert "今天到期 2 道，已完成 1 道。" in sentence


def test_bank_overview_is_one_plain_sentence(store):
    add_item(store, confidence=0.5)
    sentence = call_overview(store)
    assert "\n" not in sentence
    assert "{" not in sentence and "}" not in sentence
    assert "error_item" not in sentence and "question" not in sentence
    assert sentence.endswith("。")


def test_bank_overview_tool_returns_one_text_content(store):
    add_item(store)
    server = mcp_server.build_server(store.factory, store.learner_id)

    async def scenario():
        async with Client(server) as client:
            return await client.call_tool("bank_overview", {})

    result = run_async(scenario())
    assert result.is_error is False
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    assert "题库里一共 1 道错题：数学 1 道。" in result.content[0].text


# --- AC7/AC8 启动入口 + DB 口径 ----------------------------------------


def test_default_db_url_matches_web(store):
    assert mcp_server.DEFAULT_DB_URL == "sqlite:///./cairn.db"


def test_main_reads_env_db_url_and_serves_streamable_http(tmp_path, monkeypatch, capsys):
    db_file = tmp_path / "env.db"
    monkeypatch.setenv("CAIRN_DB_URL", f"sqlite:///{db_file}")
    monkeypatch.delenv(mcp_server.TOKEN_ENV, raising=False)
    captured: dict[str, object] = {}

    def fake_run(self, transport="stdio", **kwargs):
        captured["transport"] = transport
        captured.update(kwargs)

    monkeypatch.setattr(mcp_server.MCPServer, "run", fake_run)
    mcp_server.main(["--transport", "http", "--port", "18130"])

    assert captured["transport"] == "streamable-http"
    # 默认只监听回环（安全默认），不再是 0.0.0.0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 18130
    assert captured["streamable_http_path"] == "/mcp"
    assert db_file.exists()
    out = capsys.readouterr().out
    assert "18130" in out and "streamable-http" in out and f"db=sqlite:///{db_file}" in out
    assert "listen=http://127.0.0.1:18130/mcp" in out
    assert "token=未设置，仅限本机访问" in out  # 未设令牌 → 明确只限本机


def test_default_host_is_loopback():
    """安全默认：不传 --host 时只监听 127.0.0.1，而不是 0.0.0.0。"""
    assert mcp_server.DEFAULT_HOST == "127.0.0.1"
    assert mcp_server.is_loopback("127.0.0.1") is True
    assert mcp_server.is_loopback("localhost") is True
    assert mcp_server.is_loopback("0.0.0.0") is False
    assert mcp_server.is_loopback("203.0.113.10") is False


def test_main_refuses_non_loopback_without_token(tmp_path, monkeypatch):
    """没有令牌却要监听网卡：拒绝启动（局域网不能无鉴权暴露写接口）。"""
    monkeypatch.delenv(mcp_server.TOKEN_ENV, raising=False)
    monkeypatch.setenv("CAIRN_DB_URL", f"sqlite:///{tmp_path / 'refuse.db'}")
    with pytest.raises(SystemExit):
        mcp_server.main(["--transport", "http", "--host", "0.0.0.0"])


def test_main_enables_token_for_non_loopback(tmp_path, monkeypatch, capsys):
    """设了令牌且显式 --host：允许对外监听，并在日志里报告令牌已启用。"""
    monkeypatch.setenv(mcp_server.TOKEN_ENV, "unit-test-token")
    monkeypatch.setenv("CAIRN_DB_URL", f"sqlite:///{tmp_path / 'tok.db'}")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mcp_server.MCPServer, "run", lambda self, **kw: captured.update(kw)
    )
    mcp_server.main(["--transport", "http", "--host", "0.0.0.0", "--port", "18130"])

    assert captured["host"] == "0.0.0.0"
    out = capsys.readouterr().out
    assert "listen=http://0.0.0.0:18130/mcp" in out
    assert "token=已启用" in out


def test_shared_token_verifier_accepts_and_rejects():
    """函数级验证：正确令牌返回 AccessToken，错误/空令牌返回 None。"""
    verifier = mcp_server.SharedTokenVerifier("s3cret-token")
    assert run_async(verifier.verify_token("s3cret-token")).token == "s3cret-token"
    assert run_async(verifier.verify_token("wrong")) is None
    assert run_async(verifier.verify_token("")) is None


def test_streamable_http_requires_bearer_token(store):
    """真实中间件验证（内存 ASGI，不占端口）：无令牌/错令牌 401，带令牌 200。"""
    from starlette.testclient import TestClient

    add_item(store)
    verifier = mcp_server.SharedTokenVerifier("s3cret-token")
    auth = mcp_server.build_auth_settings("127.0.0.1", 18130)
    server = mcp_server.build_server(
        store.factory, store.learner_id, auth=auth, token_verifier=verifier
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, host="127.0.0.1"
    )
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }

    with TestClient(app, base_url="http://127.0.0.1:18130") as client:
        assert client.post("/mcp", json=body, headers=headers).status_code == 401
        wrong = client.post(
            "/mcp", json=body, headers={**headers, "Authorization": "Bearer nope"}
        )
        assert wrong.status_code == 401
        ok = client.post(
            "/mcp",
            json=body,
            headers={**headers, "Authorization": "Bearer s3cret-token"},
        )
        assert ok.status_code == 200


# --- 0.2.1 无状态 HTTP：重启后旧客户端不带 session 也能调 -------------


def test_streamable_http_app_is_stateless(store):
    """HTTP 传输必须是无状态：读 SDK 公开入口 session_manager 的开关位。"""
    server = mcp_server.build_server(store.factory, store.learner_id)
    server.streamable_http_app(
        streamable_http_path="/mcp",
        host="127.0.0.1",
        **mcp_server.http_transport_options(),
    )
    manager = server.session_manager  # SDK 公开属性（不是 _session_manager）
    assert manager.stateless is True
    assert manager.json_response is True


def test_main_passes_stateless_http_flags(tmp_path, monkeypatch):
    """main 起 streamable-http 时把无状态开关交给 SDK，而不是只写在测试里。"""
    monkeypatch.delenv(mcp_server.TOKEN_ENV, raising=False)
    monkeypatch.setenv("CAIRN_DB_URL", f"sqlite:///{tmp_path / 'stateless.db'}")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mcp_server.MCPServer, "run", lambda self, **kwargs: captured.update(kwargs)
    )
    mcp_server.main(["--transport", "http"])
    assert captured["transport"] == "streamable-http"
    assert captured["stateless_http"] is True
    assert captured["json_response"] is True


def test_two_independent_requests_share_no_session(store):
    """两个请求互不共享 session：都不带 session id，各自 200 且拿到同一句话。"""
    import httpx

    add_item(store)
    server = mcp_server.build_server(store.factory, store.learner_id)
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        host="127.0.0.1",
        **mcp_server.http_transport_options(),
    )
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "bank_overview", "arguments": {}},
    }

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:18130"
            ) as client:
                first = await client.post("/mcp", json=body, headers=headers)
                second = await client.post("/mcp", json=body, headers=headers)
        return first, second

    first, second = run_async(scenario())
    for response in (first, second):
        assert response.status_code == 200
        assert "mcp-session-id" not in response.headers  # 服务端不签发 session

    first_text = first.json()["result"]["content"][0]["text"]
    second_text = second.json()["result"]["content"][0]["text"]
    assert first_text == second_text
    assert "题库里一共 1 道错题" in first_text


def test_main_creates_the_default_learner(tmp_path, monkeypatch):
    db_file = tmp_path / "fresh.db"
    monkeypatch.setenv("CAIRN_DB_URL", f"sqlite:///{db_file}")
    monkeypatch.setattr(mcp_server.MCPServer, "run", lambda self, **kwargs: None)
    mcp_server.main(["--transport", "stdio"])

    engine = make_engine(f"sqlite:///{db_file}")
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            learners = session.scalars(select(Learner)).all()
    finally:
        engine.dispose()
    assert [learner.name for learner in learners] == ["default"]
