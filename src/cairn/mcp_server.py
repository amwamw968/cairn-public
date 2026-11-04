"""MCP 工具服务：让 Rui（house-ear）不改源码就能用 Cairn 的题库（0.2 卡12）。

四条工具，一律返回**一句可以直接念出来的中文**（Rui 的 TTS 会原样播报）：

    next_question(subject?)                      -> 第N题，数学。<题干原文>
    submit_answer(error_item_id, learner_answer) -> 答对了 / 再想想，提示：…
    today_progress()                             -> 今天可练 N 道，已完成 M 道，还剩 K 道。
    bank_overview()                              -> 题库里一共 N 道错题：… 今天到期 …

设计约束（见 docs/RUI-INTEGRATION-RESEARCH.md 与 docs/RUI-DEPLOY.md）：

- 题干**原文照念**：`content` 一字不改地拼进句子，不改写、不省略数字与单位。
- 判分与网页同源：走 `cairn.practice_service.submit_and_judge`（judge 确定性层），
  对 → good，错 → again，低置信度不写 review、进家长复核队列。
- 取题复用 `cairn.practice_service.fetch_practice`，进度复用 `cairn.stats_service`。
- 题库概况复用同一条统计链路：错题按 `error_item` + `question.subject` 聚合，
  待复核用 `graduation_service.pending_review_queue`，今日到期/完成用
  `stats_service.build_stats(...).completion`（与 `today_progress` 同源）。
- `next_question` 的 text 只有那句话（卡片要求），模型拿不到 error_item_id，
  所以进程内记住「最近问出的题」；`submit_answer` 的 id 不合法时回落到它。
- **反幻觉约束有三层，且都不依赖客户端配合**（卡 B，见 docs/PLAN.md）：
  ① `SERVER_INSTRUCTIONS`（initialize 下发，客户端可能忽略）；
  ② 四个工具的 description（每次 list_tools 都会带回去）；
  ③ **每次调用的 `structuredContent.next_step`**——这一层跟着工具返回走，一定进模型上下文。
  约束只写在 `structuredContent` 里、不写进 `content[0].text`：后者会被 Rui 的 TTS 原样念给孩子。

DB 口径与 `cairn.web` 一致：`CAIRN_DB_URL` 优先，缺省 `sqlite:///./cairn.db`。
启动示例：`python -m cairn.mcp_server --transport http --port 18130`
（streamable-http，默认只监听 127.0.0.1:18130，路径 /mcp，供 NAS 上与 Rui 同机访问）。

**HTTP 传输是无状态的**（``stateless_http=True`` + ``json_response=True``）：服务端不签发
也不校验 ``Mcp-Session-Id``，每个请求自带一次完整调用。这样容器重启后旧客户端手里的
会话信息不会作废，不必重新注册或重拉工具清单——这正是 0.2.1 修的
``device.unreachable``（重启后旧 session 调不通）的根因。

鉴权（防局域网伪造评分）：默认回环监听、不开鉴权；一旦设置环境变量
``CAIRN_MCP_TOKEN``，HTTP 传输就要求每个请求带 ``Authorization: Bearer <token>``。
要监听非回环地址（``--host 0.0.0.0`` 等）必须设置该令牌，否则进程拒绝启动——
用 mcp 2.x 自带的 ``token_verifier`` + ``AuthSettings``（内部装 BearerAuth 中间件），
不自己写 HTTP 层。
"""

from __future__ import annotations

import argparse
import hmac
import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cairn import graduation_service, practice_service, stats_service
from cairn.entry_service import ValidationError
from cairn.models import (
    ErrorItem,
    Learner,
    Question,
    init_db,
    make_engine,
    make_session_factory,
)
from cairn.models.learner import utcnow

DEFAULT_DB_URL = "sqlite:///./cairn.db"
# 默认只监听回环：Rui 与 Cairn 在 NAS 上是 host 网络，同机访问走 127.0.0.1 即可。
# 要跨机暴露必须显式传 --host（并设置 CAIRN_MCP_TOKEN，否则拒绝启动，见 main）。
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18130
DEFAULT_PATH = "/mcp"
DEFAULT_TRANSPORT = "http"
TOKEN_ENV = "CAIRN_MCP_TOKEN"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# HTTP 传输固定无状态：服务端不记账 session id，重启后旧客户端可直接调。
# json_response 必须一起开——SDK 的无状态分支不走 SSE 长连接，直接用一次性 JSON 响应，
# 每个请求独立、可以拿普通 HTTP 客户端（curl / Rui 的 HTTP 工具）调。
STATELESS_HTTP = True
JSON_RESPONSE = True

# 取题池的扫描量：只取第一道，但扫描量要够大，否则「整批都是别的学科」时会把
# 指定学科的题误报成「做完了」。fetch_practice 的 limit 只影响截断位置。
POOL_SCAN_LIMIT = 50

NO_DUE_SENTENCE = "今天的题都做完了"
EMPTY_BANK_PROGRESS_SENTENCE = "题库里还没有错题"
# 听写/默写卡（0.2 卡3 起 content 是提示：中文意思或拼音，answer 才是要写的字），
# 取题时把「要做什么」说清；只用 content，答案字符串结构上就进不来。
SPELL_SUBJECT = "英语"
SPELL_PROMPT = "请拼出对应的英文单词："
HANZI_SUBJECT = "语文"
HANZI_PROMPT = "请写出对应的汉字："
NO_ITEM_SENTENCE = "我没找到这道题，我们再要一题吧"
EMPTY_ANSWER_SENTENCE = "我还没听清你的答案，再说一遍好吗"
NO_ANSWER_SENTENCE = "这道题没有标准答案，等家长来看一下"
NEEDS_REVIEW_SENTENCE = "这道题我拿不准，等家长来看一下"
DEFAULT_HINT = "再读一遍题目，一步一步来"

# --- 反幻觉约束（卡 B）--------------------------------------------------
# 这些句子**只放 structuredContent.next_step，不进 content 文本**：content 会被 TTS
# 原样念给孩子，多写一句「必须调用 submit_answer」就变成孩子听到的噪音。
# 但 structuredContent 跟着每次工具返回走，客户端就算完全忽略 instructions 也拿得到。
QUESTION_NEXT_STEP = (
    "这道题还没判分：必须把孩子说的原话交给 submit_answer，拿到它的返回才能说对错；"
    "没有 submit_answer 的返回，不要自己说答对了或答错了。"
    "这道题判完，下一题必须再次调用 next_question，不得凭记忆复述、不得自己出题。"
)
NO_QUESTION_NEXT_STEP = (
    "工具没有题了：直接告诉孩子现在没有题，不要自己出题，也不要凭记忆复述以前出过的题。"
)
GRADED_NEXT_STEP = (
    "判分结果以本次返回为准，不要自行改写或补充；下一题请再次调用 next_question，"
    "不要自己出题。"
)
NO_ITEM_NEXT_STEP = (
    "这次没有判分成功：不要说答对了，重新调用 next_question 取一道题再判。"
)
EMPTY_ANSWER_NEXT_STEP = (
    "这次没有判分：请孩子再说一遍答案，然后重新调用 submit_answer。"
)
NO_ANSWER_NEXT_STEP = (
    "这道题没有标准答案，不能判对错：不要说答对了，交给家长确认。"
)
NEEDS_REVIEW_NEXT_STEP = (
    "这道题判分置信度不足，已进家长复核队列：不要说答对了或答错了；"
    "下一题请再次调用 next_question，不要自己出题。"
)
PROGRESS_NEXT_STEP = (
    "进度只认这次返回的数字，不要自己估算；要出题请调用 next_question。"
)
OVERVIEW_NEXT_STEP = (
    "题库数字只认这次返回的内容，不要自己编；要出题请调用 next_question。"
)

# 题库概况（bank_overview）的话术；0 值不走「0 道」的机械报数。
EMPTY_BANK_SENTENCE = "题库里还没有错题，先用网页或教材库加几道吧。"
NO_PENDING_SENTENCE = "没有待确认的题。"
NO_DUE_TODAY_SENTENCE = "今天没有到期的题。"
# 分科播报顺序：与例子「数学、语文、英语」一致，不认识的学科排在后面按名称排。
OVERVIEW_SUBJECT_ORDER = ("数学", "语文", "英语")

# 学科别名：模型偶尔会把 subject 传成英文，落回库里存的中文名
_SUBJECT_ALIASES = {
    "math": "数学",
    "maths": "数学",
    "mathematics": "数学",
    "english": "英语",
    "chinese": "语文",
    "yuwen": "语文",
}

_TRANSPORTS = {"http": "streamable-http", "sse": "sse", "stdio": "stdio"}

# --- 中文数字 -> 阿拉伯数字（答案比对用）--------------------------------

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_DIGIT_CLASS = "零〇一二三四五六七八九两"
_CN_NUMBER_RE = re.compile(
    rf"([零〇一二三四五六七八九十百千两]+点[{_CN_DIGIT_CLASS}]+|[{_CN_DIGIT_CLASS}十百千]+)"
)

_TRAILING_PUNCT = "。．.，,！!？?；;：:、~～"

# 口语答案尾巴上的量词/单位：孩子说「八个」，标准答案是 8，应当判对。
_MEASURE_WORDS = tuple(
    sorted(
        (
            "分钟",
            "小时",
            "厘米",
            "分米",
            "千米",
            "公斤",
            "千克",
            "米",
            "克",
            "元",
            "角",
            "个",
            "只",
            "支",
            "本",
            "条",
            "块",
            "岁",
            "人",
            "天",
            "秒",
            "页",
            "颗",
            "朵",
            "件",
            "张",
            "棵",
            "匹",
            "头",
            "双",
            "套",
            "遍",
            "次",
            "分",
        ),
        key=len,
        reverse=True,
    )
)

_ORDINAL_DIGITS = "零一二三四五六七八九"


def _cn_int(text: str) -> int:
    """中文整数（十/百/千）→ int。只用于答案里的数字，不做万亿级扩展。"""
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        else:  # 十 / 百 / 千
            total += (current or 1) * _CN_UNITS[char]
            current = 0
    return total + current


def _cn_run(text: str) -> str:
    """把一段连续中文数字折成阿拉伯数字；带「点」的走小数。"""
    if "点" in text:
        whole, frac = text.split("点", 1)
        digits = "".join(str(_CN_DIGITS[char]) for char in frac)
        return f"{_cn_int(whole) if whole else 0}.{digits}"
    return str(_cn_int(text))


def _strip_measure_words(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for word in _MEASURE_WORDS:
            if text.endswith(word) and len(text) > len(word):
                text = text[: -len(word)]
                changed = True
                break
    return text


def normalize_answer(text: str) -> str:
    """把答案折成可比较的键：全角→半角、去空白与句读、中文数字→阿拉伯数字、去尾部量词。

    只用于「判对/判错」，不改写题干，也不回写库。
    """
    folded = unicodedata.normalize("NFKC", text or "")
    folded = re.sub(r"\s+", "", folded)
    folded = _CN_NUMBER_RE.sub(lambda m: _cn_run(m.group(0)), folded)
    folded = folded.strip(_TRAILING_PUNCT)
    folded = _strip_measure_words(folded)
    return folded.strip(_TRAILING_PUNCT).casefold()


def _cn_ordinal(number: int) -> str:
    """1 -> 一、10 -> 十、21 -> 二十一；三位以上直接给阿拉伯数字。"""
    if 0 < number < 10:
        return _ORDINAL_DIGITS[number]
    if number == 10:
        return "十"
    if 10 < number < 20:
        return "十" + _ORDINAL_DIGITS[number - 10]
    if number < 100:
        tens, ones = divmod(number, 10)
        return _ORDINAL_DIGITS[tens] + "十" + (_ORDINAL_DIGITS[ones] if ones else "")
    return str(number)


def _subject_name(subject: str | None) -> str | None:
    clean = (subject or "").strip()
    if not clean:
        return None
    return _SUBJECT_ALIASES.get(clean.casefold(), clean)


def _completed_today(session: Session, learner_id: int, now: datetime) -> int:
    """今天已经作答过的题数（review 账本口径，与 /stats 同一份统计）。"""
    return stats_service.build_stats(session, learner_id, now=now).completion.completed_today


@dataclass
class ToolState:
    """本进程「最近一次问出的题」。

    next_question 只返回一句话（不把 id 念给孩子），模型手上没有 id，
    所以 submit_answer 拿不到合法 id 时回落到这里；进程重启后为空。
    """

    last_error_item_id: int | None = None


@dataclass(frozen=True)
class Reply:
    """一次工具调用的两副面孔：``spoken`` 念给孩子，``next_step`` 给模型定规矩。

    ``next_question`` / ``submit_answer`` 对外仍返回 ``str``（既有调用点与 405 条
    断言不动），工具包装层用 ``_reply_result`` 把 ``Reply`` 展开成
    ``CallToolResult``：文本块只有 ``spoken``，``next_step`` 落在 structuredContent。
    """

    spoken: str
    kind: str
    next_step: str
    error_item_id: int | None = None


def _ask_sentence(ordinal: int, item: practice_service.PracticeItem) -> str:
    """「第N题，学科。…」——英语/语文听写卡把要做什么说清，其他学科照念题干。"""
    prefix = f"第{_cn_ordinal(ordinal)}题，{item.subject}。"
    prompt = ""
    if item.subject == SPELL_SUBJECT:
        prompt = SPELL_PROMPT
    elif item.subject == HANZI_SUBJECT:
        prompt = HANZI_PROMPT
    if not prompt:
        return prefix + item.content
    sentence = prefix + prompt + item.content.strip()
    if sentence.endswith(("。", "！", "？", "…")):
        return sentence
    return sentence + "。"


def next_question(
    session: Session,
    learner_id: int,
    *,
    subject: str | None = None,
    clock: Callable[[], datetime] = utcnow,
    state: ToolState | None = None,
) -> str:
    """取一道到期题，返回一句可直接念的话（题干原文照念）。"""
    return _next_question_reply(
        session, learner_id, subject=subject, clock=clock, state=state
    ).spoken


def _next_question_reply(
    session: Session,
    learner_id: int,
    *,
    subject: str | None = None,
    clock: Callable[[], datetime] = utcnow,
    state: ToolState | None = None,
) -> Reply:
    """取题的完整结果：念出来的句子 + 「这题还没判分」的硬约束。"""
    now = clock()
    items = practice_service.fetch_practice(
        session, learner_id, now, limit=POOL_SCAN_LIMIT
    )
    wanted = _subject_name(subject)
    if wanted is not None:
        items = [item for item in items if item.subject == wanted]
    if not items:
        return Reply(NO_DUE_SENTENCE, "no_question", NO_QUESTION_NEXT_STEP)

    item = items[0]
    if state is not None:
        state.last_error_item_id = item.error_item_id
    ordinal = _completed_today(session, learner_id, now) + 1
    return Reply(
        _ask_sentence(ordinal, item),
        "question",
        QUESTION_NEXT_STEP,
        item.error_item_id,
    )



def _resolve_item_id(
    session: Session,
    learner_id: int,
    error_item_id: int,
    state: ToolState | None,
) -> int | None:
    """模型给的 id 优先；不合法（编的/别的 learner）才回落到最近问出的那道题。"""
    item = session.get(ErrorItem, error_item_id) if isinstance(error_item_id, int) else None
    if item is not None and item.learner_id == learner_id:
        return item.id
    if state is not None:
        return state.last_error_item_id
    return None


def submit_answer(
    session: Session,
    learner_id: int,
    error_item_id: int,
    learner_answer: str,
    *,
    clock: Callable[[], datetime] = utcnow,
    state: ToolState | None = None,
) -> str:
    """判一道题：判定点与网页同源——practice_service.submit_and_judge（judge 层）。

    对 -> good，错 -> again，低置信度不写 review、进家长复核队列
    （FSRS 账本与 pending 语义全在 practice_service / review_service）。
    """
    return _submit_answer_reply(
        session, learner_id, error_item_id, learner_answer, clock=clock, state=state
    ).spoken


def _submit_answer_reply(
    session: Session,
    learner_id: int,
    error_item_id: int,
    learner_answer: str,
    *,
    clock: Callable[[], datetime] = utcnow,
    state: ToolState | None = None,
) -> Reply:
    """判分的完整结果：念出来的句子 + 「以本次返回为准」的硬约束。

    判定只走 practice_service.submit_and_judge（与网页同一条链路），
    本模块不再做第二套答案比对——两条入口对同一道题必须给出同一判定。
    """
    item_id = _resolve_item_id(session, learner_id, error_item_id, state)
    if item_id is None:
        return Reply(NO_ITEM_SENTENCE, "no_item", NO_ITEM_NEXT_STEP)

    if not (learner_answer or "").strip():
        return Reply(EMPTY_ANSWER_SENTENCE, "empty_answer", EMPTY_ANSWER_NEXT_STEP, item_id)

    try:
        revealed, grade = practice_service.submit_and_judge(
            session, learner_id, item_id, learner_answer
        )
    except ValidationError:
        return Reply(NO_ITEM_SENTENCE, "no_item", NO_ITEM_NEXT_STEP)

    if not (revealed.answer or "").strip():
        return Reply(NO_ANSWER_SENTENCE, "no_answer", NO_ANSWER_NEXT_STEP, item_id)

    if grade.needs_review:
        return Reply(
            NEEDS_REVIEW_SENTENCE, "needs_review", NEEDS_REVIEW_NEXT_STEP, item_id
        )
    if grade.correct:
        return Reply("答对了", "correct", GRADED_NEXT_STEP, item_id)
    hint = (revealed.analysis or "").strip() or DEFAULT_HINT
    return Reply(f"再想想，提示：{hint}", "wrong", GRADED_NEXT_STEP, item_id)



def today_progress(
    session: Session,
    learner_id: int,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> str:
    """今天这一批题：与 fetch_practice 的取题池同一口径（新题 + 到期题）。

    空状态分开说：题库里一道题都没有 / 有题但今天已经练完。
    """
    stat = stats_service.practice_today(session, learner_id, now=clock())
    if stat.total_items == 0:
        return EMPTY_BANK_PROGRESS_SENTENCE
    if stat.remaining == 0:
        return NO_DUE_SENTENCE
    return (
        f"今天可练 {stat.playable} 道，已完成 {stat.completed} 道，"
        f"还剩 {stat.remaining} 道。"
    )


def _subject_error_counts(session: Session, learner_id: int) -> list[tuple[str, int]]:
    """该 learner 的错题按学科计数，按播报顺序返回（题目少的学科也不省略）。"""
    rows = session.execute(
        select(Question.subject, func.count(ErrorItem.id))
        .join(ErrorItem, ErrorItem.question_id == Question.id)
        .where(ErrorItem.learner_id == learner_id)
        .group_by(Question.subject)
    ).all()
    counts = {subject: count for subject, count in rows}
    ordered = [subject for subject in OVERVIEW_SUBJECT_ORDER if subject in counts]
    ordered += sorted(subject for subject in counts if subject not in OVERVIEW_SUBJECT_ORDER)
    return [(subject, counts[subject]) for subject in ordered]


def bank_overview(
    session: Session,
    learner_id: int,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> str:
    """题库概况：总数与分科、待复核、今日到期/完成，拼成一段可直接念的话。"""
    counts = _subject_error_counts(session, learner_id)
    total = sum(count for _, count in counts)
    if total == 0:
        return EMPTY_BANK_SENTENCE

    breakdown = "、".join(f"{subject} {count} 道" for subject, count in counts)
    sentences = [f"题库里一共 {total} 道错题：{breakdown}。"]

    pending = len(graduation_service.pending_review_queue(session, learner_id))
    sentences.append(
        f"另有 {pending} 道待你确认。" if pending else NO_PENDING_SENTENCE
    )

    completion = stats_service.build_stats(
        session, learner_id, now=clock()
    ).completion
    due_today = completion.due_today
    completed_today = completion.completed_today
    if due_today and completed_today:
        sentences.append(f"今天到期 {due_today} 道，已完成 {completed_today} 道。")
    elif due_today:
        sentences.append(f"今天到期 {due_today} 道，还没开始做。")
    elif completed_today:
        sentences.append(f"今天没有到期的题，已完成 {completed_today} 道。")
    else:
        sentences.append(NO_DUE_TODAY_SENTENCE)

    # 单段纯文本：句号之间不插换行，便于 TTS 一口气播报。
    return "".join(sentences)


SERVER_INSTRUCTIONS = (
    "Cairn 是孩子的错题本。以下规则任何情况下都不能违反：\n"
    "1. 出题只能调用 next_question。禁止自拟题目，禁止凭记忆复述出过的题。\n"
    "2. 判分只能调用 submit_answer。\n"
    "3. 没有调用 submit_answer、拿到它的返回之前，绝对不要说「答对了」「答错了」"
    "或任何判分结论。\n"
    "4. 工具说没有题，就直接告诉孩子现在没有题，不要自己补一道。\n"
    "5. content 里的句子原文念给孩子听；structuredContent.next_step 是给你的规则，"
    "照着做，不要念出来。\n"
    "6. 题干原文照念：数字和单位不改、不省略，不提前说答案。\n"
    "7. 孩子问还剩几道用 today_progress，问题库总数与各科多少用 bank_overview，"
    "数字只认工具返回。"
)

_TOOL_DESCRIPTIONS = {
    "next_question": "唯一合法的出题入口：题只能从这里取，禁止自己编题或凭记忆复述出过的题。返回的 text 是可以直接念给孩子听的那句话，题干必须原文照念、不要改写、不要提前说答案；structuredContent.next_step 是给你的下一步规则。",
    "submit_answer": "唯一合法的判分入口：对错只能由这次调用决定，没有它的返回就不要说答对了。learner_answer 传孩子说的原话（中文数字可以）。返回的 text 是念给孩子听的那句话。",
    "today_progress": "唯一合法的进度入口：还剩几道只能由这次调用报告，不要自己估算。返回的 text 是可以直接念的话。",
    "bank_overview": "唯一合法的题库概况入口：错题总数与各科数量只能来自这次调用，不要自己编数字。返回的 text 是可以直接念的话。",
}


def _reply_result(reply: Reply) -> CallToolResult:
    """把 ``Reply`` 展开成工具返回：文本块只念给孩子，约束放 structuredContent。

    ``content`` 走 TTS，多一个字都是噪音；``structuredContent`` 是协议里给模型看的
    结构化结果，客户端即使忽略 initialize 的 instructions，也会随这次调用拿到它。
    """
    return CallToolResult(
        content=[TextContent(type="text", text=reply.spoken)],
        structured_content={
            "spoken_text": reply.spoken,
            "kind": reply.kind,
            "error_item_id": reply.error_item_id,
            "next_step": reply.next_step,
        },
    )



class SharedTokenVerifier(TokenVerifier):
    """固定共享令牌校验：只认 ``CAIRN_MCP_TOKEN`` 这一个 bearer 令牌。

    用 mcp 2.x 的 ``TokenVerifier`` 协议实现，交给框架内部的 ``BearerAuthBackend`` /
    ``RequireAuthMiddleware`` 做「无令牌 401 / 令牌不符 401」。比较走
    ``hmac.compare_digest``，避免逐字符比较的时序侧信道。
    """

    def __init__(self, expected: str) -> None:
        if not expected:
            raise ValueError("令牌不能为空")
        self._expected = expected

    async def verify_token(self, token: str) -> AccessToken | None:
        if token and hmac.compare_digest(token, self._expected):
            return AccessToken(token=token, client_id="rui", scopes=[])
        return None


def build_auth_settings(host: str, port: int) -> AuthSettings:
    """Bearer 鉴权所需的 AuthSettings；「受众」用本机监听地址即可。

    ``validate_token_resource=False``：令牌校验由我们自己的 verifier 负责（固定令牌，
    不绑定 resource），这里关掉框架的受众强校验，避免误拒合法令牌。
    """
    base = f"http://{host}:{port}"
    return AuthSettings(
        issuer_url=base,
        resource_server_url=base,
        validate_token_resource=False,
    )


def is_loopback(host: str) -> bool:
    """监听地址是不是只对本机可见（回环）。非回环要暴露到网卡。"""
    clean = (host or "").strip()
    return clean in LOOPBACK_HOSTS or clean.startswith("127.")


def http_transport_options() -> dict[str, bool]:
    """streamable-http 的传输开关，直接展开给 ``MCPServer.run``。

    无状态 + JSON 一次性响应是一对：无状态分支每个请求新建一个 transport、不签
    ``Mcp-Session-Id``，响应因此不能是 SSE 长流，必须 ``json_response=True``。
    """
    return {"stateless_http": STATELESS_HTTP, "json_response": JSON_RESPONSE}


def build_server(
    session_factory: sessionmaker[Session],
    learner_id: int,
    clock: Callable[[], datetime] = utcnow,
    *,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    """把四个工具挂到一个 MCPServer 上（每次调用单开一个 Session，用完就关）。

    ``auth`` 与 ``token_verifier`` 必须成对传入（mcp 2.x 的要求）；都不传就是无鉴权，
    只允许回环监听（由 ``main`` 保证）。
    """
    server = MCPServer(
        name="cairn",
        title="Cairn 错题本",
        instructions=SERVER_INSTRUCTIONS,
        version="0.2",
        auth=auth,
        token_verifier=token_verifier,
    )
    state = ToolState()

    @server.tool(name="next_question", description=_TOOL_DESCRIPTIONS["next_question"])
    def next_question_tool(subject: str | None = None) -> CallToolResult:
        with session_factory() as session:
            reply = _next_question_reply(
                session, learner_id, subject=subject, clock=clock, state=state
            )
        return _reply_result(reply)

    @server.tool(name="submit_answer", description=_TOOL_DESCRIPTIONS["submit_answer"])
    def submit_answer_tool(error_item_id: int, learner_answer: str) -> CallToolResult:
        with session_factory() as session:
            reply = _submit_answer_reply(
                session,
                learner_id,
                error_item_id,
                learner_answer,
                clock=clock,
                state=state,
            )
        return _reply_result(reply)

    @server.tool(name="today_progress", description=_TOOL_DESCRIPTIONS["today_progress"])
    def today_progress_tool() -> CallToolResult:
        with session_factory() as session:
            spoken = today_progress(session, learner_id, clock=clock)
        return _reply_result(Reply(spoken, "progress", PROGRESS_NEXT_STEP))

    @server.tool(name="bank_overview", description=_TOOL_DESCRIPTIONS["bank_overview"])
    def bank_overview_tool() -> CallToolResult:
        with session_factory() as session:
            spoken = bank_overview(session, learner_id, clock=clock)
        return _reply_result(Reply(spoken, "overview", OVERVIEW_NEXT_STEP))


    return server


def _ensure_learner(session_factory: sessionmaker[Session]) -> int:
    """与 cairn.web 同一个口径：库里没有 learner 就建一个，之后一直用它。"""
    with session_factory() as session:
        learner = session.scalars(
            select(Learner).order_by(Learner.id.asc()).limit(1)
        ).first()
        if learner is None:
            learner = Learner(name="default")
            session.add(learner)
            session.commit()
        return learner.id


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m cairn.mcp_server",
        description="Cairn 的 MCP 工具服务（供 NAS 上的 Rui 调用）",
    )
    parser.add_argument(
        "--transport", choices=tuple(_TRANSPORTS), default=DEFAULT_TRANSPORT
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            f"监听地址；缺省 {DEFAULT_HOST}（仅本机可见）。要跨机暴露必须显式指定"
            f"（如 0.0.0.0），且必须设置环境变量 {TOKEN_ENV}，否则拒绝启动。"
        ),
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument(
        "--db-url", default=None, help=f"缺省读 CAIRN_DB_URL，再缺省 {DEFAULT_DB_URL}"
    )
    args = parser.parse_args(argv)

    host = (args.host or DEFAULT_HOST).strip()
    token = (os.environ.get(TOKEN_ENV) or "").strip() or None
    if not is_loopback(host) and not token:
        # 没有令牌就绝不开到网卡：局域网内任何设备都能伪造评分、读题。
        parser.error(
            f"拒绝启动：监听非回环地址 {host!r} 必须设置环境变量 {TOKEN_ENV}"
            f"（否则只能监听 {DEFAULT_HOST}）。"
        )

    db_url = args.db_url or os.environ.get("CAIRN_DB_URL", DEFAULT_DB_URL)
    engine = make_engine(db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    learner_id = _ensure_learner(session_factory)

    transport = _TRANSPORTS[args.transport]
    # stdio 是本地管道，不走 HTTP 鉴权；token 只对 http/sse 生效。
    auth_settings = None
    verifier = None
    if token and transport != "stdio":
        auth_settings = build_auth_settings(host, args.port)
        verifier = SharedTokenVerifier(token)
    server = build_server(
        session_factory,
        learner_id,
        auth=auth_settings,
        token_verifier=verifier,
    )

    _print_token_status(token)
    if transport == "stdio":
        print(f"cairn mcp: transport=stdio db={db_url} learner={learner_id}", flush=True)
        server.run(transport="stdio")
        return
    print(
        f"cairn mcp: transport={transport} listen=http://{host}:{args.port}{args.path} "
        f"db={db_url} learner={learner_id}",
        flush=True,
    )
    # 只有 streamable-http 认这两个开关；sse/stdio 保持原样（不接受多余 kwargs）。
    run_options: dict[str, object] = {}
    if transport == "streamable-http":
        run_options = http_transport_options()
    server.run(
        transport=transport,
        **run_options,
        host=host,
        port=args.port,
        streamable_http_path=args.path,
    )


def _print_token_status(token: str | None) -> None:
    """启动日志第二行：明确令牌是否启用（EVIDENCE 要能一眼看到）。"""
    if token:
        print(
            f"cairn mcp: token=已启用（客户端须带 Authorization: Bearer <{TOKEN_ENV}>）",
            flush=True,
        )
    else:
        print("cairn mcp: token=未设置，仅限本机访问", flush=True)


if __name__ == "__main__":
    main()
