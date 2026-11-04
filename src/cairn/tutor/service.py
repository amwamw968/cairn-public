"""四段式讲解服务：问思路 → 三层提示 → 解析（回讲 recap 留给下一卡）。

为什么第一步绝不给答案（PNAS Bastani et al. RCT，近千名高中生）：
练习期可随时看答案的组 +48%，但收走 AI 后的独立考试 **−17%**；带
"先提示后答案"护栏的组 +127% 且考试无差异。所以讲解不是"念解析"，
是"逼孩子想"——护栏在这里落成三道机械闸：

1. **prompt 约束**：每段都附"绝不出现答案"的系统指令（只对 LLM 有效，
   不可信任）；
2. **确定性泄漏检测**（``leak.is_leak``，归一化包含比对）：``ask_thought``
   与 ``hint_1/2/3`` 的产出必过；``explain`` 本来就要给答案，不查；
3. **泄漏处理**：重生成一次（附更强指令）；仍泄漏 → 该段换成预置兜底
   文案，消息记 ``leak_blocked=1``。兜底文案只拼题干，构造上安全。

LLM 只生成文本，**不参与对错判定**——判分在 ``grading/``，本模块不碰。
LLM 任何异常（无网络/无 key/超时/坏响应）都降级到同段模板文案，
绝不向上抛（接口不许 500）。
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cairn.models.question import Question
from cairn.models.tutor import TutorMessage, TutorSession
from cairn.tutor import leak, llm
from cairn.workflow.review import ready_for_practice

# 阶段推进的唯一顺序；recap（回讲）属于下一卡，列出仅为占位说明。
STAGES: tuple[str, ...] = ("ask_thought", "hint_1", "hint_2", "hint_3", "explain")
NEXT_CARD_STAGE = "recap"

_ANSWER_ALLOWED_STAGES = frozenset({"explain"})


class TutorError(RuntimeError):
    """讲解服务错误总类。"""


class QuestionNotFound(TutorError):
    """题目不存在（或不属于这个 learner）。"""


class SessionNotFound(TutorError):
    """讲解会话不存在（或不属于这个 learner）。"""


class StageExhausted(TutorError):
    """已经讲完（explain 送达）还要求推进下一段。"""


class TutorConflict(TutorError):
    """The question or session changed while the model was responding."""


_SYSTEM_PROMPT = (
    "你是一位耐心的小学家教老师，正在辅导孩子订正错题。"
    "你的任务是引导孩子自己想出答案，绝不能替他做。"
    "铁律：回复里绝对不允许出现这道题的最终答案或它的任何等价形式；"
    "语气口语化，一次只说一两句话。"
)

_EXPLAIN_SYSTEM_PROMPT = (
    "你是一位耐心的小学家教老师，孩子已经选择查看完整解析。"
    "请根据提供的题目、标准答案和参考解析，先给出答案，再分步骤解释为什么；"
    "不要编造题目中没有的信息，语气清楚、适合孩子理解。"
)

_REGEN_SUFFIX = (
    "\n\n（重申：上一条回复泄露了最终答案，这是严重错误。"
    "重写这条回复，全程用问句和引导，最终答案的位置一律写成「？」，"
    "连部分得数都不要替孩子算出来。）"
)


def _prompt(stage: str, question: Question) -> str:
    """每段的 user prompt。answer 只给 hint_2/hint_3/explain：
    问思路与一层提示用不到它，少给一分就少一分泄漏面。"""
    content = question.content or ""
    answer = question.answer or ""
    analysis = question.analysis or ""
    if stage == "ask_thought":
        return (
            f"题目：「{content}」\n"
            "请只向孩子提一个问题：问他觉得第一步该先做什么。"
            "不要给任何提示、不要分析题目、不要出现任何得数。"
        )
    if stage == "hint_1":
        return (
            f"题目：「{content}」\n"
            "给第一层提示：只指方向（该先看哪里、先注意什么），"
            "不教具体方法，不出现任何得数。"
        )
    if stage == "hint_2":
        return (
            f"题目：「{content}」（标准答案你自己知道：{answer}，绝不可写出）\n"
            "给第二层提示：指方法（先做什么转换、用什么办法），"
            "不替孩子算，不出现任何得数。"
        )
    if stage == "hint_3":
        return (
            f"题目：「{content}」（标准答案你自己知道：{answer}，绝不可写出）\n"
            "给第三层提示：给半成品——把铺垫（如单位换算、算式开头）写出来，"
            "最后一步留给孩子，答案位置一律写「？」。"
        )
    # explain
    tail = f"参考解析：{analysis}" if analysis else ""
    return (
        f"题目：「{content}」\n"
        f"标准答案是「{answer}」。现在孩子已经提示过了，"
        "请给出完整讲解：包括答案、每一步怎么做、为什么。"
        f"{tail}"
    )


def _fallback_text(stage: str, question: Question) -> str:
    """预置兜底文案：只拼题干与（仅 explain）答案/解析，构造上无泄漏面。"""
    content = (question.content or "").strip()
    answer = (question.answer or "").strip()
    analysis = (question.analysis or "").strip()
    if stage == "ask_thought":
        return f"先看看这道题：「{content}」。你觉得第一步该先做什么？说说你的想法。"
    if stage == "hint_1":
        return (
            f"提示一：先别急着算。把题再读一遍：「{content}」，"
            "想一想它到底在问什么、给了你哪些条件。"
        )
    if stage == "hint_2":
        return (
            "提示二：想想用什么方法。把已知条件一条条列出来，"
            "看看哪两个能先结合起来算一步。"
        )
    if stage == "hint_3":
        return (
            "提示三：把第一步写出来试试——先算出第一步的结果"
            "（写成「第一步 = 」），再拿这个结果往下走一步。"
        )
    # explain：这一段本来就允许出现答案
    if answer:
        body = f"完整解析：这道题的答案是 {answer}。"
        if analysis:
            body += f"\n{analysis}"
        return body
    if analysis:
        return f"完整解析：{analysis}"
    return "这道题在题库里没有存标准答案。请和家长一起对照课本把解析看一遍。"


def _generate(stage: str, question: Question) -> tuple[str, int]:
    """生成一段 tutor 文案，返回 ``(text, leak_blocked)``。

    护栏段（ask_thought / hint_1/2/3）：泄漏 → 换强指令重生成一次 →
    仍泄漏 → 预置兜底文案 + ``leak_blocked=1``。explain 段本来就要给
    答案，不做泄漏检查。LLM 异常一律降级到兜底文案（``leak_blocked=0``，
    那不是泄漏，是没网）。
    """
    guarded = stage not in _ANSWER_ALLOWED_STAGES
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT if guarded else _EXPLAIN_SYSTEM_PROMPT},
        {"role": "user", "content": _prompt(stage, question)},
    ]
    try:
        text = llm.chat(messages)
        if not guarded or not leak.is_leak(text, question.answer):
            return text, 0
        # 第一次泄漏：附强指令重生成一次
        retry = list(messages)
        retry[-1] = {"role": "user", "content": messages[-1]["content"] + _REGEN_SUFFIX}
        text = llm.chat(retry)
        if not leak.is_leak(text, question.answer):
            return text, 0
        return _fallback_text(stage, question), 1
    except llm.LLMError:
        return _fallback_text(stage, question), 0


def _question(db: Session, learner_id: int, question_id: int) -> Question:
    question = db.scalar(select(Question).where(
        Question.id == question_id, Question.learner_id == learner_id,
        ready_for_practice(Question.id),
    ))
    if question is None:
        raise QuestionNotFound(f"题目 {question_id} 不存在")
    return question


def _tutor_session(db: Session, learner_id: int, session_id: int) -> TutorSession:
    ts = db.get(TutorSession, session_id)
    if ts is None or ts.learner_id != learner_id:
        raise SessionNotFound(f"讲解会话 {session_id} 不存在")
    return ts


def _snapshot(question: Question) -> tuple[str, str | None, str | None]:
    return question.content, question.answer, question.analysis


def _lock_and_recheck_question(db: Session, learner_id: int, question: Question,
                               expected: tuple[str, str | None, str | None]) -> None:
    """Take the SQLite write lock only after inference, then recheck the input."""
    changed = db.execute(update(Question).where(
        Question.id == question.id, Question.learner_id == learner_id,
    ).values(content=Question.content).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise QuestionNotFound("题目不存在")
    db.refresh(question)
    _question(db, learner_id, question.id)
    if _snapshot(question) != expected:
        raise TutorConflict("题目刚刚被修改，请刷新后重试")


def _claim_stage(db: Session, learner_id: int, ts: TutorSession, previous: str, target: str) -> None:
    claimed = db.execute(update(TutorSession).where(
        TutorSession.id == ts.id, TutorSession.learner_id == learner_id,
        TutorSession.stage == previous,
    ).values(stage=target).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise TutorConflict("讲解已在其他位置更新，请刷新后重试")


def _add_tutor_message(
    db: Session, ts: TutorSession, stage: str, text: str, leak_blocked: int
) -> TutorMessage:
    message = TutorMessage(
        session_id=ts.id,
        role="tutor",
        stage=stage,
        text=text,
        leak_blocked=leak_blocked,
    )
    db.add(message)
    ts.stage = stage
    db.commit()
    return message


def start_tutor(db: Session, learner_id: int, question_id: int) -> TutorMessage:
    """建会话并送达第一段 ``ask_thought``（只提问，不给任何提示）。"""
    question = _question(db, learner_id, question_id)
    expected = _snapshot(question)
    text, leak_blocked = _generate(STAGES[0], question)
    _lock_and_recheck_question(db, learner_id, question, expected)
    ts = TutorSession(learner_id=learner_id, question_id=question.id, stage=STAGES[0])
    db.add(ts)
    db.flush()  # 拿到 ts.id，消息外键要用
    return _add_tutor_message(db, ts, STAGES[0], text, leak_blocked)


def next_tutor(
    db: Session, learner_id: int, session_id: int, child_text: str | None = None
) -> TutorMessage:
    """推进一段；孩子的话与讲解结果在同一事务保存。

    已经到 ``explain`` 再调 → ``StageExhausted``（web 层映射 409）。
    """
    ts = _tutor_session(db, learner_id, session_id)
    if ts.stage not in STAGES or STAGES.index(ts.stage) >= STAGES.index("explain"):
        raise StageExhausted("这一题已经讲完了，没有下一段")
    question = _question(db, learner_id, ts.question_id)
    previous = ts.stage
    expected = _snapshot(question)
    next_stage = STAGES[STAGES.index(previous) + 1]
    text, leak_blocked = _generate(next_stage, question)
    _lock_and_recheck_question(db, learner_id, question, expected)
    _claim_stage(db, learner_id, ts, previous, next_stage)
    if child_text and child_text.strip():
        db.add(
            TutorMessage(
                session_id=ts.id,
                role="child",
                stage=previous,
                text=child_text.strip(),
                leak_blocked=0,
            )
        )
    return _add_tutor_message(db, ts, next_stage, text, leak_blocked)


def reveal_tutor(db: Session, learner_id: int, session_id: int) -> TutorMessage:
    """跳到 ``explain``（孩子说"我不想了/直接告诉我"）。

    已在 explain 时幂等：不重讲、不新增消息，把上次那条再返回。
    """
    ts = _tutor_session(db, learner_id, session_id)
    question = _question(db, learner_id, ts.question_id)
    if ts.stage == "explain":
        last = db.scalars(
            select(TutorMessage)
            .where(TutorMessage.session_id == ts.id, TutorMessage.stage == "explain")
            .order_by(TutorMessage.id.desc())
            .limit(1)
        ).first()
        if last is not None:
            return last
    previous = ts.stage
    expected = _snapshot(question)
    text, leak_blocked = _generate("explain", question)
    _lock_and_recheck_question(db, learner_id, question, expected)
    try:
        _claim_stage(db, learner_id, ts, previous, "explain")
    except TutorConflict:
        db.refresh(ts)
        if ts.stage == "explain":
            last = db.scalar(select(TutorMessage).where(
                TutorMessage.session_id == ts.id, TutorMessage.stage == "explain",
            ).order_by(TutorMessage.id.desc()).limit(1))
            if last is not None:
                return last
        raise
    return _add_tutor_message(db, ts, "explain", text, leak_blocked)
