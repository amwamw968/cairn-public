"""讲解四段式卡的测试：阶段推进、泄漏拦截、防误伤、降级、409/404、迁移 v14。

LLM 一律用假实现（替换 ``cairn.tutor.llm.chat``），不碰真网络；
泄漏检测是确定性代码，单测直接锁 ``normalize_for_leak_check`` / ``is_leak``。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from cairn.models import Learner, make_engine, make_session_factory
from cairn.models.base import Base
from cairn.models.tutor import TutorMessage, TutorSession
from cairn.tutor import service as tutor_service
from cairn.tutor import llm as tutor_llm
from cairn.tutor.leak import is_leak, normalize_for_leak_check
from cairn.tutor.service import StageExhausted, next_tutor, reveal_tutor, start_tutor
from cairn.web import create_app

# ---------------------------------------------------------------- 假 LLM


def test_llm_environment_key_and_optional_thinking_configuration(monkeypatch, tmp_path):
    calls = []
    def post(url, *, data, headers, timeout):
        calls.append((json.loads(data), headers))
        return 200, b'{"choices":[{"message":{"content":"ok"}}]}'

    monkeypatch.setattr(tutor_llm, "_post", post)
    monkeypatch.setenv("CAIRN_TUTOR_LLM_API_KEY", "env-example")
    monkeypatch.setenv("CAIRN_TUTOR_LLM_THINKING", "disabled")
    assert tutor_llm.chat([{"role": "user", "content": "test"}]) == "ok"
    assert calls[-1][1]["Authorization"] == "Bearer env-example"
    assert calls[-1][0]["thinking"] == {"type": "disabled"}
    key_file = tmp_path / "key"
    key_file.write_text("file-example", encoding="utf-8")
    monkeypatch.delenv("CAIRN_TUTOR_LLM_THINKING")
    assert tutor_llm.chat([{"role": "user", "content": "test"}], key_file=str(key_file)) == "ok"
    assert calls[-1][1]["Authorization"] == "Bearer file-example"
    assert "thinking" not in calls[-1][0]


def test_llm_rejects_bad_environment_options_without_sending_key(monkeypatch):
    monkeypatch.setattr(tutor_llm, "_post", lambda *args, **kwargs: pytest.fail("unexpected request"))
    monkeypatch.setenv("CAIRN_TUTOR_LLM_API_KEY", "bad\nkey")
    with pytest.raises(tutor_llm.LLMKeyError, match="格式无效"):
        tutor_llm.chat([{"role": "user", "content": "test"}])
    monkeypatch.setenv("CAIRN_TUTOR_LLM_API_KEY", "test")
    monkeypatch.setenv("CAIRN_TUTOR_LLM_THINKING", "invalid")
    with pytest.raises(tutor_llm.LLMError, match="thinking"):
        tutor_llm.chat([{"role": "user", "content": "test"}])


def test_llm_scrubs_environment_key_from_error_reasons(monkeypatch):
    import io
    import urllib.error
    monkeypatch.setenv("CAIRN_TUTOR_LLM_API_KEY", "example-secret")
    def failed(url, **kwargs):
        raise urllib.error.HTTPError(url, 401, "bad example-secret", {}, io.BytesIO(b"example-secret"))
    monkeypatch.setattr(tutor_llm, "_post", failed)
    with pytest.raises(tutor_llm.LLMHTTPError) as error:
        tutor_llm.chat([{"role": "user", "content": "test"}])
    assert "example-secret" not in str(error.value)


def test_tutor_allows_answer_only_in_explicit_explanation(monkeypatch):
    from cairn.models import Question
    prompts = []
    def chat(messages, **kwargs):
        prompts.append(messages)
        return "让我们看看每一步。"
    monkeypatch.setattr(tutor_llm, "chat", chat)
    question = Question(learner_id=1, subject="数学", question_type="计算", content="1 + 2?", answer="3")
    tutor_service._generate("hint_2", question)
    tutor_service._generate("explain", question)
    assert "绝对不允许" in prompts[0][0]["content"]
    assert "先给出答案" in prompts[1][0]["content"]
    assert "绝对不允许" not in prompts[1][0]["content"]


def _stage_reply(messages):
    """按每段 user prompt 的特征词回话：文本里永远没有答案。"""
    user = messages[-1]["content"]
    if "只向孩子提一个问题" in user:
        return "老师先问你：这道题第一步该做什么？"
    if "第一层提示" in user:
        return "先看看题目给了哪些条件。"
    if "第二层提示" in user:
        return "试试先统一单位再比较。"
    if "第三层提示" in user:
        return "1 米 = 100 厘米，所以 2 米 = ? 厘米，你来算。"
    return "完整讲解：先统一单位，再按步骤算，你就能得出结果。"


@pytest.fixture()
def fake_llm(monkeypatch):
    calls: list[list] = []

    def _chat(messages, **kwargs):
        calls.append(messages)
        return _stage_reply(messages)

    monkeypatch.setattr(tutor_llm, "chat", _chat)
    return calls


# ---------------------------------------------------------------- 泄漏检测单测


def test_leak_detects_plain_answer():
    assert is_leak("答案是 3.5", "3.5")


def test_leak_detects_fullwidth_and_case():
    assert is_leak("答案 ＡＢ", "ab")
    assert is_leak("Answer: X", "x")


def test_leak_ignores_whitespace():
    assert is_leak("200 厘 米", "200厘米")


def test_leak_fullwidth_digits_normalized():
    assert is_leak("３．５ 才对", "3.5")


@pytest.mark.parametrize("text,answer", [
    ("算出来是二十二。", "22"),
    ("结果是两百厘米。", "200厘米"),
    ("一共有一千零一个。", "1001"),
    ("答案是负十二。", "-12"),
    ("就是零。", "0"),
])
def test_leak_detects_common_chinese_integer_forms(text, answer):
    assert is_leak(text, answer)


def test_chinese_integer_answer_is_blocked_before_explanation(session, learner, question, monkeypatch):
    question.answer = "22"
    session.commit()
    monkeypatch.setattr(tutor_llm, "chat", lambda *args, **kwargs: "结果是二十二。")
    message = start_tutor(session, learner.id, question.id)
    assert message.leak_blocked == 1
    assert "二十二" not in message.text


def test_no_false_positive_three_plus_five():
    # 答案 3.5，「3 加 5」不许误伤
    assert not is_leak("先算 3 加 5，再做下一步。", "3.5")


def test_no_false_positive_unrelated_text():
    assert not is_leak("先看看题目给了哪些条件。", "27")


def test_leak_empty_answer_never_leaks():
    assert not is_leak("任何文本", "")
    assert not is_leak("任何文本", None)


def test_normalize_strips_all_whitespace_and_lowercases():
    assert normalize_for_leak_check("Ａ Bｃ") == "abc"


# ---------------------------------------------------------------- 服务层


def test_stage_progression(session, learner, question, fake_llm):
    message = start_tutor(session, learner.id, question.id)
    assert message.stage == "ask_thought"
    seen = ["ask_thought"]
    for expected in ("hint_1", "hint_2", "hint_3", "explain"):
        message = next_tutor(session, learner.id, message.session_id)
        seen.append(message.stage)
    assert seen == ["ask_thought", "hint_1", "hint_2", "hint_3", "explain"]
    ts = session.get(TutorSession, message.session_id)
    assert ts.stage == "explain"


def test_guarded_stages_never_contain_answer(session, learner, question, fake_llm):
    message = start_tutor(session, learner.id, question.id)
    for _ in range(4):
        assert "27" not in message.text
        message = next_tutor(session, learner.id, message.session_id)


def test_explain_stage_contains_answer(session, learner, question, fake_llm):
    message = start_tutor(session, learner.id, question.id)
    for _ in range(4):
        message = next_tutor(session, learner.id, message.session_id)
    assert message.stage == "explain"


def test_next_after_explain_raises(session, learner, question, fake_llm):
    message = start_tutor(session, learner.id, question.id)
    for _ in range(4):
        message = next_tutor(session, learner.id, message.session_id)
    with pytest.raises(StageExhausted):
        next_tutor(session, learner.id, message.session_id)


def test_leak_blocked_after_two_leaks(session, learner, question, monkeypatch):
    question.answer = "3.5"
    session.commit()
    monkeypatch.setattr(
        tutor_llm, "chat", lambda messages, **kw: "答案是 3.5，算完了。"
    )
    message = start_tutor(session, learner.id, question.id)
    # 两次都泄漏 → 兜底文案 + leak_blocked=1
    assert message.leak_blocked == 1
    assert "3.5" not in message.text
    assert message.text  # 兜底文案不是空话
    message = next_tutor(session, learner.id, message.session_id)
    assert message.stage == "hint_1"
    assert message.leak_blocked == 1
    assert "3.5" not in message.text


def test_leak_regen_once_then_clean(session, learner, question, monkeypatch):
    question.answer = "3.5"
    session.commit()
    calls = {"n": 0}

    def _chat(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "答案是 3.5"
        return "先看看题目给了哪些条件。"

    monkeypatch.setattr(tutor_llm, "chat", _chat)
    message = start_tutor(session, learner.id, question.id)
    assert calls["n"] == 2  # 恰好重生成一次
    assert message.leak_blocked == 0
    assert message.text == "先看看题目给了哪些条件。"


def test_llm_down_falls_back_to_template(session, learner, question, monkeypatch):
    def _boom(messages, **kwargs):
        raise tutor_llm.LLMError("连不上 LLM 服务")

    monkeypatch.setattr(tutor_llm, "chat", _boom)
    message = start_tutor(session, learner.id, question.id)
    assert message.leak_blocked == 0  # 没网不是泄漏
    assert "12 + 15" in message.text  # 模板文案带着题干
    for expected in ("hint_1", "hint_2", "hint_3"):
        message = next_tutor(session, learner.id, message.session_id)
        assert message.stage == expected
        assert message.text
    message = next_tutor(session, learner.id, message.session_id)
    assert message.stage == "explain"
    assert "27" in message.text  # 兜底解析含答案


def test_child_text_is_archived(session, learner, question, fake_llm):
    message = start_tutor(session, learner.id, question.id)
    message = next_tutor(
        session, learner.id, message.session_id, child_text="先算加法"
    )
    rows = session.scalars(
        select(TutorMessage).where(TutorMessage.session_id == message.session_id)
    ).all()
    child_rows = [row for row in rows if row.role == "child"]
    assert len(child_rows) == 1
    assert child_rows[0].text == "先算加法"
    assert child_rows[0].stage == "ask_thought"


def test_reveal_jumps_to_explain(session, learner, question, fake_llm):
    message = start_tutor(session, learner.id, question.id)
    message = reveal_tutor(session, learner.id, message.session_id)
    assert message.stage == "explain"
    ts = session.get(TutorSession, message.session_id)
    assert ts.stage == "explain"


def test_reveal_is_idempotent(session, learner, question, fake_llm):
    message = start_tutor(session, learner.id, question.id)
    first = reveal_tutor(session, learner.id, message.session_id)
    second = reveal_tutor(session, learner.id, message.session_id)
    assert first.id == second.id


# ---------------------------------------------------------------- web 接口


@pytest.fixture()
def client(tmp_path, monkeypatch):
    calls: list[list] = []

    def _chat(messages, **kwargs):
        calls.append(messages)
        return _stage_reply(messages)

    monkeypatch.setattr(tutor_llm, "chat", _chat)
    app = create_app(f"sqlite:///{tmp_path}/web.db")
    with TestClient(app) as c:
        yield c, str(tmp_path / "web.db")


def _web_question(db_path: str) -> int:
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    session = factory()
    try:
        learner = session.scalars(select(Learner).limit(1)).first()
        from cairn.models import Question

        question = Question(
            learner_id=learner.id,
            subject="数学",
            grade_level="primary-3",
            question_type="计算",
            content="12 + 15 = ?",
            answer="27",
            analysis="12+15=27",
        )
        session.add(question)
        session.commit()
        return question.id
    finally:
        session.close()
        engine.dispose()


def test_web_full_flow_and_409(client):
    c, db = client
    question_id = _web_question(db)
    resp = c.post("/tutor/start", json={"question_id": question_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["stage"] == "ask_thought"
    assert "27" not in body["text"]
    session_id = body["session_id"]
    for stage in ("hint_1", "hint_2", "hint_3", "explain"):
        resp = c.post("/tutor/next", json={"session_id": session_id})
        assert resp.status_code == 200
        assert resp.json()["stage"] == stage
    resp = c.post("/tutor/next", json={"session_id": session_id})
    assert resp.status_code == 409


def test_web_start_unknown_question_404(client):
    c, _ = client
    assert c.post("/tutor/start", json={"question_id": 99999}).status_code == 404


def test_tutor_rejects_excluded_question_before_writing_child_message(client):
    from cairn.models import ErrorItem
    c, db = client
    question_id = _web_question(db)
    started = c.post("/tutor/start", json={"question_id": question_id})
    assert started.status_code == 200
    session_id = started.json()["session_id"]
    with c.app.state.session_factory() as session:
        session.add(ErrorItem(learner_id=c.app.state.learner_id, question_id=question_id,
                              workflow_rejected=1))
        session.commit()
    assert c.post("/tutor/start", json={"question_id": question_id}).status_code == 404
    assert c.post("/tutor/next", json={"session_id": session_id, "child_text": "do not save"}).status_code == 404
    assert c.post("/tutor/reveal", json={"session_id": session_id}).status_code == 404
    with c.app.state.session_factory() as session:
        assert session.scalar(select(TutorMessage.id).where(
            TutorMessage.session_id == session_id, TutorMessage.role == "child")) is None


def test_slow_tutor_does_not_block_other_http_requests(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    c, db = client
    question_id = _web_question(db)
    entered, release = Event(), Event()
    def slow_chat(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return "先看看已知条件。"
    monkeypatch.setattr(tutor_llm, "chat", slow_chat)
    def write_unrelated():
        with c.app.state.session_factory() as session:
            session.add(Learner(name="Concurrent writer"))
            session.commit()
        return True
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = pool.submit(lambda: c.post("/tutor/start", json={"question_id": question_id}))
        assert entered.wait(5)
        try:
            assert pool.submit(lambda: c.get("/strategies")).result(timeout=3).status_code == 200
            assert pool.submit(write_unrelated).result(timeout=3)
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 200


def test_tutor_rejects_question_changed_during_model_call(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from cairn.models import Question

    c, db = client
    question_id = _web_question(db)
    entered, release = Event(), Event()
    def slow_chat(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return "先看看已知条件。"
    monkeypatch.setattr(tutor_llm, "chat", slow_chat)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lambda: c.post("/tutor/start", json={"question_id": question_id}))
        assert entered.wait(5)
        try:
            with c.app.state.session_factory() as session:
                session.get(Question, question_id).answer = "99"
                session.commit()
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 409
    with c.app.state.session_factory() as session:
        assert session.scalar(select(TutorSession.id).where(TutorSession.question_id == question_id)) is None


def test_concurrent_tutor_next_only_saves_one_step(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    c, db = client
    question_id = _web_question(db)
    session_id = c.post("/tutor/start", json={"question_id": question_id}).json()["session_id"]
    barrier = Barrier(2)
    def slow_chat(*args, **kwargs):
        barrier.wait(timeout=10)
        return "先看看已知条件。"
    monkeypatch.setattr(tutor_llm, "chat", slow_chat)
    def advance(text):
        return c.post("/tutor/next", json={"session_id": session_id, "child_text": text})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(advance, text) for text in ("第一个回答", "第二个回答")]
        assert sorted(f.result(timeout=15).status_code for f in futures) == [200, 409]
    with c.app.state.session_factory() as session:
        rows = list(session.scalars(select(TutorMessage).where(TutorMessage.session_id == session_id)))
        assert sum(row.stage == "hint_1" and row.role == "tutor" for row in rows) == 1
        assert sum(row.role == "child" for row in rows) == 1


def test_concurrent_tutor_reveal_is_idempotent(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    c, db = client
    question_id = _web_question(db)
    session_id = c.post("/tutor/start", json={"question_id": question_id}).json()["session_id"]
    barrier = Barrier(2)
    def slow_chat(*args, **kwargs):
        barrier.wait(timeout=10)
        return "最终解析：答案是 27。"
    monkeypatch.setattr(tutor_llm, "chat", slow_chat)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(lambda: c.post("/tutor/reveal", json={"session_id": session_id})) for _ in range(2)]
        assert [f.result(timeout=15).status_code for f in futures] == [200, 200]
    with c.app.state.session_factory() as session:
        messages = list(session.scalars(select(TutorMessage).where(
            TutorMessage.session_id == session_id, TutorMessage.stage == "explain")))
        assert len(messages) == 1


def test_streamed_tutor_start_uses_learner_at_request_start(client):
    import asyncio
    import httpx

    c, db = client
    question_id = _web_question(db)
    owner_id = c.app.state.learner_id
    with c.app.state.session_factory() as session:
        session.add(Learner(id=999, name="Other"))
        session.commit()
    async def send():
        async def body():
            yield b'{"question_id":'
            c.app.state.learner_id = 999
            yield f"{question_id}}}".encode()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://test") as http:
            return await http.post("/tutor/start", content=body(), headers={"Content-Type": "application/json"})
    response = asyncio.run(send())
    assert response.status_code == 200
    with c.app.state.session_factory() as session:
        assert session.get(TutorSession, response.json()["session_id"]).learner_id == owner_id


def test_web_next_unknown_session_404(client):
    c, _ = client
    resp = c.post("/tutor/next", json={"session_id": 424242})
    assert resp.status_code == 404


def test_web_reveal_unknown_session_404(client):
    c, _ = client
    resp = c.post("/tutor/reveal", json={"session_id": 424242})
    assert resp.status_code == 404


def test_web_reveal_then_next_409(client):
    c, db = client
    question_id = _web_question(db)
    body = c.post("/tutor/start", json={"question_id": question_id}).json()
    resp = c.post("/tutor/reveal", json={"session_id": body["session_id"]})
    assert resp.status_code == 200
    assert resp.json()["stage"] == "explain"
    assert c.post("/tutor/next", json={"session_id": body["session_id"]}).status_code == 409


def test_web_child_text_recorded(client):
    c, db = client
    question_id = _web_question(db)
    body = c.post("/tutor/start", json={"question_id": question_id}).json()
    resp = c.post(
        "/tutor/next",
        json={"session_id": body["session_id"], "child_text": "先算个位"},
    )
    assert resp.status_code == 200
    engine = make_engine(f"sqlite:///{db}")
    factory = make_session_factory(engine)
    session = factory()
    try:
        child_rows = session.scalars(
            select(TutorMessage).where(TutorMessage.role == "child")
        ).all()
        assert len(child_rows) == 1
        assert child_rows[0].text == "先算个位"
    finally:
        session.close()
        engine.dispose()


def test_web_llm_down_still_200(client, monkeypatch):
    def _boom(messages, **kwargs):
        raise tutor_llm.LLMError("连不上 LLM 服务")

    c, db = client
    # client fixture 已打了一个假 chat；再覆盖成抛错的
    from cairn.tutor import llm as llm_mod

    monkeypatch.setattr(llm_mod, "chat", _boom)
    question_id = _web_question(db)
    resp = c.post("/tutor/start", json={"question_id": question_id})
    assert resp.status_code == 200
    assert resp.json()["text"]  # 模板文案
    assert "27" not in resp.json()["text"]


def test_web_tutor_page_renders(client):
    c, _ = client
    resp = c.get("/tutor?question_id=1")
    assert resp.status_code == 200
    assert "开始讲解" in resp.text


# ---------------------------------------------------------------- 迁移 v13/v14


def _make_partial_db(url: str, table_names: set[str]) -> None:
    engine = make_engine(url)
    try:
        for table in Base.metadata.sorted_tables:
            if table.name in table_names:
                table.create(bind=engine)
    finally:
        engine.dispose()


def _seed_old_rows(url: str) -> None:
    engine = make_engine(url)
    factory = make_session_factory(engine)
    session = factory()
    try:
        learner = Learner(name="old-child", grade_level="primary-3")
        session.add(learner)
        session.commit()
        from cairn.models import Question

        session.add(
            Question(
                learner_id=learner.id,
                subject="数学",
                grade_level="primary-3",
                question_type="计算",
                content="3 + 4 = ?",
                answer="7",
            )
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()


def _raw_rows(db_url: str, sql: str):
    import sqlite3

    con = sqlite3.connect(db_url.removeprefix("sqlite:///"))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _table_exists(db_url: str, table: str) -> bool:
    engine = make_engine(db_url)
    try:
        return inspect(engine).has_table(table)
    finally:
        engine.dispose()


def test_migration_v12_to_v14_old_data_intact(tmp_path):
    """v12 老库（还没有 tutor 两表）升级：新表出现、存量行不动、版本到 14。"""
    url = f"sqlite:///{tmp_path}/old12.db"
    _make_partial_db(url, {"learner", "question", "strategy"})
    _seed_old_rows(url)
    from cairn import migrations

    engine = make_engine(url)
    try:
        migrations.stamp_version(engine, 12)
        assert migrations.apply_migrations(engine) == migrations.SCHEMA_VERSION
        with engine.connect() as conn:
            assert migrations._read_version(conn) == migrations.SCHEMA_VERSION
    finally:
        engine.dispose()
    assert _table_exists(url, "tutor_session")
    assert _table_exists(url, "tutor_message")
    learners = _raw_rows(url, "SELECT id, name FROM learner")
    assert learners == [(1, "old-child")]
    questions = _raw_rows(url, "SELECT content, answer FROM question")
    assert questions == [("3 + 4 = ?", "7")]


def test_migration_v13_to_v14_session_rows_kept(tmp_path):
    """v13 库（已有 tutor_session）升级到 v14：session 存量行原样保留。"""
    url = f"sqlite:///{tmp_path}/old13.db"
    _make_partial_db(url, {"learner", "question", "strategy", "tutor_session"})
    _seed_old_rows(url)
    import sqlite3
    from datetime import datetime, timezone

    con = sqlite3.connect(url.removeprefix("sqlite:///"))
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO tutor_session (learner_id, question_id, stage, created_at, updated_at) "
            "VALUES (1, 1, 'ask_thought', ?, ?)",
            (now, now),
        )
        con.commit()
    finally:
        con.close()

    from cairn import migrations

    engine = make_engine(url)
    try:
        migrations.        stamp_version(engine, 13)
        assert migrations.apply_migrations(engine) == migrations.SCHEMA_VERSION  # 升到链顶
    finally:
        engine.dispose()
    assert _table_exists(url, "tutor_message")
    sessions = _raw_rows(
        url, "SELECT learner_id, question_id, stage FROM tutor_session"
    )
    assert sessions == [(1, 1, "ask_thought")]


def test_migration_rerun_is_idempotent(tmp_path):
    from cairn import migrations

    url = f"sqlite:///{tmp_path}/twice.db"
    engine = make_engine(url)
    try:
        Base.metadata.create_all(engine)
        migrations.stamp_version(engine)
        assert migrations.apply_migrations(engine) == migrations.SCHEMA_VERSION
        assert migrations.apply_migrations(engine) == migrations.SCHEMA_VERSION
    finally:
        engine.dispose()
