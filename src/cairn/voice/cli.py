"""交互式命令行：M1 打字，M2 说活。

    python -m cairn.voice          # M1：一行字进
    python -m cairn.voice --mic    # M2：麦克风进

两条路共用同一个回合（``run_turn``），区别只在「话从哪儿来」「话怎么出去」：

    人说 → 录音断句 → ASR 转文字 ─┐
    人打字 ────────────────────┴→ run_turn → 念出来的话（TTS / 终端）
                                          → [约束] 只进终端，绝不念

M1 的终端格式不变，念出来的话带说话人标签打一行，有约束就单独再打一行。

M2 的循环：录一句 → 转写 → 回合 → 播报 ``spoken``，然后接着听。循环里的失败
分两种：某一轮出错（ASR 挂了 / 工具抛了）只报「这一轮没处理好：…」接着听；
麦克风本身打不开就直接停（继续转只会刷屏），提示去看权限设置。

库口径与 web / mcp_server 一致：``--db-url`` > ``CAIRN_DB_URL`` > 临时库。
**缺省是临时库**，跑完就删，绝不碰生产 ``cairn.db``；启动第一行会打印实际用的路径。

失败只说话、不抛栈（都进 stderr）：
建库/迁移失败 → ``数据库不可用：<原因>`` 且退出码 1；
某一轮出错 → ``这一轮没处理好：<原因>`` 然后继续；
Ctrl-C → 换行、退出码 0。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from cairn import mcp_server
from cairn.models import init_db, make_engine, make_session_factory
from cairn.voice.asr import transcribe
from cairn.voice.mic import DEVICE_ERROR, Utterance, listen
from cairn.voice.tts import speak
from cairn.voice.turn import run_turn

PROMPT = "你> "
HEARD_PROMPT = "你(语音)> "
SPEAKER = "Cairn> "
CONSTRAINT_PREFIX = "[约束] "
MIC_PREFIX = "[麦克风] "
EXIT_WORDS = ("exit", "quit")
# 两类失败的交代都走 stderr：stdout 只留「念给孩子的话」，管道/日志里不混错误。
DB_UNAVAILABLE_PREFIX = "数据库不可用："
TURN_FAILED_PREFIX = "这一轮没处理好："
MIC_BANNER = "语音模式：说一句，等播报完再说下一句；Ctrl-C 退出。"
MIC_STOPPED = "麦克风用不了，语音模式先停下；修好权限或换设备后再跑 --mic。"


def _temp_db_url() -> tuple[str, tempfile.TemporaryDirectory[str]]:
    """一次性临时库：进程退出即删，不落仓库、不落生产路径。"""
    holder: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
        prefix="cairn-voice-"
    )
    return f"sqlite:///{Path(holder.name) / 'cairn.db'}", holder


def _mic_turn(factory, learner_id: int, utterance: Utterance) -> None:
    """一轮语音的「转写之后」：ASR → 回合 → 播报。单轮失败只报一句。"""
    try:
        text = transcribe(utterance.wav or b"")
    except Exception as exc:  # ASR 挂了不影响下一句
        print(f"{TURN_FAILED_PREFIX}{exc}", file=sys.stderr)
        return
    print(f"{HEARD_PROMPT}{text}")
    try:
        with factory() as session:
            turn = run_turn(session, learner_id, text)
    except Exception as exc:  # 单轮失败不打断会话
        print(f"{TURN_FAILED_PREFIX}{exc}", file=sys.stderr)
        return
    print(f"{SPEAKER}{turn.spoken}")
    if turn.next_step:
        # 约束只给模型看，只打印不念——念出来孩子只会听到噪音。
        print(f"{CONSTRAINT_PREFIX}{turn.next_step}")
    try:
        speak(turn.spoken)
    except Exception as exc:  # speak 内部已兜底；再兜一层，播报失败不许打断循环
        print(f"{TURN_FAILED_PREFIX}{exc}", file=sys.stderr)


def _mic_loop(factory, learner_id: int) -> None:
    """录 → 转 → 回合 → 播，直到 Ctrl-C。

    ``listen()`` 自己已经把设备异常翻译成 ``device_error``；这种情况直接收摊——
    继续循环只会每秒刷一遍同样的错误。其余失败（ASR / 工具）都只影响一轮。
    """
    print(MIC_BANNER)
    try:
        while True:
            utterance = listen()
            if utterance.reason == DEVICE_ERROR:
                print(MIC_STOPPED, file=sys.stderr)
                return
            if utterance.wav is None:
                print(f"{MIC_PREFIX}{utterance.detail}")
                continue
            if utterance.truncated:
                print(f"{MIC_PREFIX}{utterance.detail}")
            _mic_turn(factory, learner_id, utterance)
    except KeyboardInterrupt:
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cairn.voice",
        description="Cairn 语音入口：M1 打字回合 / M2 麦克风回合",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="缺省读 CAIRN_DB_URL，再缺省建一个临时库（不碰生产库）",
    )
    parser.add_argument(
        "--mic",
        action="store_true",
        help="语音模式：麦克风 → 断句 → ASR → 回合 → 播报（M2）",
    )
    args = parser.parse_args(argv)

    holder = None
    db_url = args.db_url or os.environ.get("CAIRN_DB_URL")
    if not db_url:
        db_url, holder = _temp_db_url()

    engine = None
    try:
        engine = make_engine(db_url)
        init_db(engine)
        factory = make_session_factory(engine)
        learner_id = mcp_server._ensure_learner(factory)
    except Exception as exc:  # 库坏/路径不可写/迁移失败：给一句人话，别裸抛栈
        print(f"{DB_UNAVAILABLE_PREFIX}{exc}", file=sys.stderr)
        if engine is not None:
            engine.dispose()
        if holder is not None:
            holder.cleanup()
        return 1

    try:
        if args.mic:
            print(f"cairn voice M2: db={db_url} learner={learner_id}")
            _mic_loop(factory, learner_id)
            return 0

        print(f"cairn voice M1: db={db_url} learner={learner_id}")
        print(f"输入一行话回车；输入 {' / '.join(EXIT_WORDS)} 退出。")
        while True:
            try:
                line = input(PROMPT)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if line.strip().lower() in EXIT_WORDS:
                break
            if not line.strip():
                continue
            try:
                with factory() as session:
                    turn = run_turn(session, learner_id, line)
            except Exception as exc:  # 单轮失败不打断会话：报一句，接着听下一句
                print(f"{TURN_FAILED_PREFIX}{exc}", file=sys.stderr)
                continue
            print(f"{SPEAKER}{turn.spoken}")
            if turn.next_step:
                print(f"{CONSTRAINT_PREFIX}{turn.next_step}")
    finally:
        engine.dispose()
        if holder is not None:
            holder.cleanup()
    return 0
