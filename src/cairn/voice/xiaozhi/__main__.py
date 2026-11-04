"""``python -m cairn.voice.xiaozhi --port 4411``：一个端口同时跑 OTA 与 WebSocket。

启动会打印监听地址、本机可 curl 的 OTA 地址、固件该连的 WS 地址，以及这一轮接的
ASR / TTS / 数据库。板子那侧怎么把 OTA 指过来（改路由 / 改 DNS / 刷固件）不在本服务范围内。

**启动即预检音频**：没有 libopus / opuslib 时直接退出并给出安装命令，不静默降级——
一个「连得上但永远没声音」的服务比启动失败更难查。

环境变量（都可被命令行覆盖）：

    CAIRN_DB_URL         生产库地址；不设则建临时库（跑完即删，不碰 cairn.db）
    CAIRN_ASR_URL        默认 http://127.0.0.1:8040
    CAIRN_ASR_KEY_FILE   默认 ~/.hermes/keys/qwen_asr.key
    CAIRN_TTS_VOICE      say 的音色名（不设用系统默认）
    CAIRN_LEARNER_ID     指定孩子；不设则取库里 id 最小的（没有就建一个）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from cairn import mcp_server
from cairn.models import init_db, make_engine, make_session_factory
from cairn.voice import asr
from cairn.voice.xiaozhi import audio, ota
from cairn.voice.xiaozhi.server import XiaozhiServer, local_ip
from cairn.voice.xiaozhi.voice import VoicePipeline

DEFAULT_PORT = 4412
ENV_LEARNER_ID = "CAIRN_LEARNER_ID"


def _log(*parts: object) -> None:
    """带时间戳的一行日志。每轮耗时（asr_ms/turn_ms/tts_ms）就打在 stdout，方便直接算延迟。"""
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}]", *parts, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cairn.voice.xiaozhi",
        description="小智固件的协议接入：OTA 端点 + WebSocket（M3b：Opus 进出 + 回合 + TTS）",
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"监听端口，默认 {DEFAULT_PORT}"
    )
    parser.add_argument(
        "--ws-url",
        default=None,
        help="返回给固件的地址；默认 ws://<本机IP>:<端口>/xiaozhi/ws",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="缺省读 CAIRN_DB_URL，再缺省建临时库（不碰生产库）",
    )
    parser.add_argument(
        "--learner-id",
        type=int,
        default=None,
        help=f"指定孩子 id；缺省读 {ENV_LEARNER_ID}，再缺省取库里最小的",
    )
    return parser


def _open_db(db_url: str | None):
    """建库 + 迁移 + 取孩子。失败抛异常，由调用方翻成人话。"""
    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)
    return engine, factory


def _temp_db_url() -> tuple[str, tempfile.TemporaryDirectory[str]]:
    holder: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
        prefix="cairn-xiaozhi-"
    )
    return f"sqlite:///{Path(holder.name) / 'cairn.db'}", holder


async def _serve(args: argparse.Namespace) -> int:
    ok, reason = audio.available()
    if not ok:
        print(f"音频编解码不可用，服务不启动：{reason}", file=sys.stderr)
        return 1

    holder = None
    db_url = args.db_url or os.environ.get("CAIRN_DB_URL")
    if not db_url:
        db_url, holder = _temp_db_url()

    engine = None
    try:
        engine, factory = _open_db(db_url)
        learner_id = args.learner_id
        if learner_id is None:
            env_learner = os.environ.get(ENV_LEARNER_ID)
            learner_id = int(env_learner) if env_learner else mcp_server._ensure_learner(factory)
    except Exception as exc:  # 库坏 / 路径不可写 / 迁移失败：给一句人话
        print(f"数据库不可用：{exc}", file=sys.stderr)
        if engine is not None:
            engine.dispose()
        if holder is not None:
            holder.cleanup()
        return 1

    asr_url, asr_key, asr_model = asr._settings(None, None, None)
    pipeline = VoicePipeline(
        session_factory=factory,
        learner_id=learner_id,
        log=_log,
    )
    server = XiaozhiServer(
        host=args.host,
        port=args.port,
        ws_url=args.ws_url,
        log=_log,
        pipeline=pipeline,
    )
    try:
        await server.start()
    except OSError as exc:
        print(f"端口用不了：{args.host}:{args.port}（{exc}）", file=sys.stderr)
        engine.dispose()
        if holder is not None:
            holder.cleanup()
        return 1

    print(
        f"[ota] 监听 {args.host}:{server.port}  路径 {ota.OTA_PATHS[0]}（GET 与 POST 都应答）",
        flush=True,
    )
    print(f"[ota] 本机可打 http://{local_ip()}:{server.port}{ota.OTA_PATHS[0]}", flush=True)
    print(f"[ws]  固件将连到 {server.ws_url}", flush=True)
    # 只打密钥文件路径，不打内容。
    print(f"[voice] ASR {asr_url} model={asr_model} key={asr_key}", flush=True)
    voice_name = os.environ.get("CAIRN_TTS_VOICE") or "系统默认"
    print(f"[voice] TTS macOS say（16 kHz 单声道）voice={voice_name}", flush=True)
    print(f"[voice] 库 {db_url} learner={learner_id}", flush=True)
    print("Ctrl-C 退出。", flush=True)

    try:
        await server.serve_forever()
    finally:
        await server.close()
        engine.dispose()
        if holder is not None:
            holder.cleanup()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
