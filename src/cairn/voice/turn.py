"""一轮对话：路由 → 调工具 → 把「念出来的」和「给模型的」分开交出去。

一轮只做三件事，任何业务判断都不在这里：

1. ``router.route`` 决定走向（闲聊 / 要题 / 判分 / 进度 / 题库）；
2. 走工具时**直接调 ``cairn.mcp_server`` 里已有的函数**，念的句子与约束都从那边拿——
   voice 层不留第二套取题、进度、题库逻辑，MCP 工具改话术这里自动跟着变；
3. 组装 ``Turn``：``spoken`` 给 TTS / 喇叭，``next_step`` 给模型定规矩。

**``spoken`` 里绝不能出现 ``next_step`` 的内容**：前者会被原样念给孩子，
「必须调用 submit_answer」这种句子进耳朵就是噪音。这条在
``cairn.mcp_server`` 里已经成立（约束只进 structuredContent），本层原样透传、
一个字都不拼，测试钉住。

判分（``Intent.GRADE``）M1 **不实现**：判分还需要孩子把答案说出来。所以这一支只念
「判分还没接，请先把答案告诉我」，**绝不**退化成出一道新题——静默答错比报错更糟。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from cairn import mcp_server, practice_service
from cairn.models.learner import utcnow
from cairn.voice import commands
from cairn.voice.commands import VoiceCommand
from cairn.voice.router import Intent, pick_subject, route

# TODO(voice-M2)：闲聊现在是固定一句，接了模型再按上下文回；M1 只要证明链路通。
CHAT_REPLY = "嗯，我在。"

# 判分未接时的固定交代。必须把「为什么没判、接下来做什么」都说清，
# 孩子才不会被一句无关的新题糊弄过去。
GRADING_NOT_WIRED = "判分还没接，请先把答案告诉我。"
GRADING_NOT_WIRED_NEXT_STEP = (
    "判分链路 M1 还没接：不要自己判断对错，也不要用一道新题顶替；"
    "请孩子把答案再说一遍，等接上 submit_answer 再判。"
)

# ``kind`` 取值与 ``mcp_server`` 工具包装层的 structuredContent.kind 同一套值，
# voice 不另造词；判分那条 M1 没有工具，单独给一个不会与工具返回值混淆的 kind。
PROGRESS_KIND = "progress"
OVERVIEW_KIND = "overview"
GRADING_KIND = "grade_pending"

# 短指令三类新 kind：同样不与工具返回值混淆。stop 不出话（流水线见了一律
# 不进 TTS，只补一条 tts stop 收尾，与打断同一路径）。
STOP_KIND = "stop"
HINT_KIND = "hint"
CONTINUE_KIND = "continue"

# 提示指令的开场：只接当前题的 content，answer 结构上进不了 PracticeItem。
HINT_PREFIX = "提示："

# 跳过时没有别的题：明说，不把同一道题再念一遍（真机实测：只有一道题，
# 「跳过」之后同一道题又被原样念了一次）。
SKIP_NO_OTHER_SENTENCE = "没有别的题了，先做这道吧。"


@dataclass(frozen=True)
class Turn:
    """一轮的产物。

    ``kind`` 是「这一轮发生了什么」：闲聊是 ``"chat"``；走工具时直接透传工具返回的
    kind（``"question"`` / ``"no_question"`` …），与 ``structuredContent.kind`` 同一套值。
    ``next_step`` 为 ``None`` 表示这一轮没有要给模型的约束（闲聊），
    调用方就不要往模型上下文里塞空字符串。
    """

    kind: str
    spoken: str
    next_step: str | None


def run_turn(
    session: Session,
    learner_id: int,
    text: str,
    *,
    awaiting_answer: bool = False,
    clock: Callable[[], datetime] = utcnow,
    on_stop: Callable[[], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> Turn:
    """跑一轮：先试短指令，再按 ``route`` 给的枚举分流，不在这里再认一遍关键词。

    短指令层（``commands.match_command``）在 ``route`` 之前：孩子打断后喊的
    「停一下」「跳过」必须直接执行，不能落进闲聊回一句「嗯，我在」。
    ``CAIRN_VOICE_INTENTS=0`` 时整层关闭，行为与从前完全一致。

    要题调的是 ``_next_question_reply`` 而不是公开的 ``next_question``：后者按既有契约
    只返回字符串，``next_step`` 在它内部就丢了。两者念出来的句子是同一份
    （同一个函数），测试里逐字比对过。进度 / 题库用的是公开函数 + 同一对常量，
    与 ``mcp_server`` 里工具包装层的拼法一致。
    """
    if commands.intents_enabled():
        command = commands.match_command(text, log=log)
        if command is not None:
            return _run_command(
                session, learner_id, command, clock=clock, on_stop=on_stop
            )

    intent = route(text, awaiting_answer=awaiting_answer)

    if intent is Intent.CHAT:
        return Turn(kind=Intent.CHAT.value, spoken=CHAT_REPLY, next_step=None)

    if intent is Intent.ASK:
        reply = mcp_server._next_question_reply(
            session,
            learner_id,
            subject=pick_subject(text),
            clock=clock,
        )
        return Turn(kind=reply.kind, spoken=reply.spoken, next_step=reply.next_step)

    if intent is Intent.PROGRESS:
        return Turn(
            kind=PROGRESS_KIND,
            spoken=mcp_server.today_progress(session, learner_id, clock=clock),
            next_step=mcp_server.PROGRESS_NEXT_STEP,
        )

    if intent is Intent.BANK:
        return Turn(
            kind=OVERVIEW_KIND,
            spoken=mcp_server.bank_overview(session, learner_id, clock=clock),
            next_step=mcp_server.OVERVIEW_NEXT_STEP,
        )

    # 剩下的只有判分：M1 不实现，明确说没接，不给新题。
    return Turn(
        kind=GRADING_KIND,
        spoken=GRADING_NOT_WIRED,
        next_step=GRADING_NOT_WIRED_NEXT_STEP,
    )


def _run_command(
    session: Session,
    learner_id: int,
    command: VoiceCommand,
    *,
    clock: Callable[[], datetime],
    on_stop: Callable[[], None] | None,
) -> Turn:
    """执行命中的短指令。动作全部复用 ``mcp_server`` / ``practice_service``，不写第二套。"""
    if command is VoiceCommand.STOP:
        # 播报多半已被 barge-in 停掉；这里只登记停止动作、不出任何新话。
        if on_stop is not None:
            on_stop()
        return Turn(kind=STOP_KIND, spoken="", next_step=None)

    if command is VoiceCommand.SKIP:
        reply = mcp_server._next_question_reply(session, learner_id, clock=clock)
        current = _current_item(session, learner_id, clock)
        if (
            reply.kind != "question"
            or current is None
            or reply.error_item_id != current.error_item_id
        ):
            # 没题可跳，或确实换到了新题：照旧念工具的话。
            return Turn(kind=reply.kind, spoken=reply.spoken, next_step=reply.next_step)
        # 返回的还是当前这道（同 error_item_id）：找当前题之外的下一道；
        # 找不到就明说，绝不把同一道题再念一遍。
        newer = _next_item_after(session, learner_id, current, clock)
        if newer is None:
            return Turn(
                kind=reply.kind,
                spoken=SKIP_NO_OTHER_SENTENCE,
                next_step=reply.next_step,
            )
        ordinal = mcp_server._completed_today(session, learner_id, clock()) + 1
        return Turn(
            kind="question",
            spoken=mcp_server._ask_sentence(ordinal, newer),
            next_step=mcp_server.QUESTION_NEXT_STEP,
        )

    if command is VoiceCommand.HINT:
        item = _current_item(session, learner_id, clock)
        if item is None:
            return Turn(
                kind="no_question",
                spoken=mcp_server.NO_DUE_SENTENCE,
                next_step=mcp_server.NO_QUESTION_NEXT_STEP,
            )
        # PracticeItem 结构上没有 answer 字段：content 是题干/提示语，answer 进不来。
        return Turn(kind=HINT_KIND, spoken=HINT_PREFIX + item.content.strip(), next_step=None)

    # 继续：还没答（题还在池里）就再念一次题干提示；已答就出下一题。
    item = _current_item(session, learner_id, clock)
    if item is None:
        reply = mcp_server._next_question_reply(session, learner_id, clock=clock)
        return Turn(kind=reply.kind, spoken=reply.spoken, next_step=reply.next_step)
    now = clock()
    ordinal = mcp_server._completed_today(session, learner_id, now) + 1
    return Turn(
        kind=CONTINUE_KIND,
        spoken=mcp_server._ask_sentence(ordinal, item),
        next_step=None,
    )


def _current_item(
    session: Session, learner_id: int, clock: Callable[[], datetime]
):
    """「当前题」= 取题池的第一道（与 next_question 同一条链路、同一个口径）。"""
    items = practice_service.fetch_practice(
        session, learner_id, clock(), limit=mcp_server.POOL_SCAN_LIMIT
    )
    return items[0] if items else None


def _next_item_after(
    session: Session, learner_id: int, current, clock: Callable[[], datetime]
):
    """取题池里第一道**不是当前这道**的题；只有当前一道时返回 ``None``。"""
    items = practice_service.fetch_practice(
        session, learner_id, clock(), limit=mcp_server.POOL_SCAN_LIMIT
    )
    for item in items:
        if item.error_item_id != current.error_item_id:
            return item
    return None
