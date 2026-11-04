"""把 OTA、`/debug/mute` 与 WebSocket 挂在同一个端口上：板子只需要记住一个地址。

固件的 OTA 地址与 WS 地址是两件事，但 M3a 让它们同端口——少一个转发、少一次解释。
alpha 只放**一个** WS 连接：第二个在握手前回 ``503 {"error":"busy"}``，
不发 101，这样固件日志里是「服务端在忙」而不是「连上又断」。

**这条连接永远不静默关闭**：请求头坏了回 4xx，处理函数抛了回 500，路径不认识回 404，
全都是 JSON。板子那侧只有 ``Connection closed before response headers were received``
这一句话可用，我们把原因留在自己的日志里。

M3b 在这条连接上加了一条支路：``listen stop`` 之后把这段音频交给 ``voice.VoicePipeline``
（ASR → 回合 → TTS），推送独立成一个 task，所以读循环还能继续收 ``abort``。
**所有写都过 ``_send_lock``**：控制消息和 Opus 帧来自两个 task，不加锁会在帧中间
插进一条 JSON，板子会把它当成音频包解。
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import ipaddress
import os
import socket
import struct
import sys
import time
import uuid
from collections.abc import Callable

from cairn.voice import asr as asr_module
from cairn.voice.xiaozhi import mute, ota, wire
from cairn.voice.xiaozhi import voice as voice_module
from cairn.voice.xiaozhi.ws import (
    BargeSettings,
    Conversation,
    VadSettings,
    barge_settings_from_env,
    tts_stop,
    vad_settings_from_env,
)

WS_PATH = "/xiaozhi/ws"

# 会话窗口：连续这么久没有有效语音就播结束语、关连接，回到只认唤醒词的待命。
# 板子在 realtime 模式下会持续推流，所以空闲判定挂在收帧循环上即可。
# 环境变量 ``CAIRN_SESSION_IDLE_SECONDS`` 可覆盖，默认 300 秒。
SESSION_IDLE_SECONDS = 300.0
# 打断（barge-in）宽限期：无 AEC，播报开头这一段的上行能量必是我们自己的回声，
# 一律忽略（不进滑窗）。之后上行滑窗高能占比达到判据（见 ws.BargeSettings，
# ``CAIRN_BARGE_MS`` / ``CAIRN_BARGE_RATIO``）才认定孩子在插话；
# ``CAIRN_BARGE_GRACE_MS`` 可覆盖宽限期，默认 600 毫秒。
BARGE_GRACE_MS = 600


def barge_grace_ms_from_env(environ: dict[str, str] | None = None) -> int:
    """读 ``CAIRN_BARGE_GRACE_MS``（播报开始后多少毫秒内的上行一律忽略）；没配用默认。"""
    env = os.environ if environ is None else environ
    raw = env.get("CAIRN_BARGE_GRACE_MS")
    if raw is None:
        return BARGE_GRACE_MS
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"CAIRN_BARGE_GRACE_MS={raw!r} 不是合法的 int。") from None
    if value <= 0:
        raise ValueError(f"CAIRN_BARGE_GRACE_MS={raw!r} 必须为正数。")
    return value


def session_idle_seconds_from_env(environ: dict[str, str] | None = None) -> float:
    """读 ``CAIRN_SESSION_IDLE_SECONDS``；没配用默认，配坏了报错不悄悄回落。"""
    env = os.environ if environ is None else environ
    raw = env.get("CAIRN_SESSION_IDLE_SECONDS")
    if raw is None:
        return SESSION_IDLE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"CAIRN_SESSION_IDLE_SECONDS={raw!r} 不是合法的 float。"
        ) from None
    if value <= 0:
        raise ValueError(f"CAIRN_SESSION_IDLE_SECONDS={raw!r} 必须为正数。")
    return value


# 一条请求最多占多久（头 + 体）。1KB 的 OTA 报文在局域网上是毫秒级；
# 10 秒还没读完就是发不全（板子 WiFi 省电下建连+发请求实测能到 2.2 秒，2 秒会误判），照常应答——OTA 只读 body 里几个字段，
# 体没读完不该让板子拿不到地址，也不该让板子等到自己超时。
REQUEST_TIMEOUT = 10.0

# 本机在局域网里的地址：固件只能连可达的 IP，127.0.0.1 对它没意义。
#
# 不能只问「默认路由走哪张卡」：开发机常挂着 TUN（代理 / 异地组网），那条路由的
# 源地址是 198.18.x.x 或 100.64.x.x，板子根本连不到。所以先枚举网卡，只认
# RFC1918 上的有线/无线网卡；实在没有才退回默认路由，再没有就是环回。
_PROBE = ("192.0.2.1", 9)  # TEST-NET-1，只为让内核选出口网卡，不真发包
_SIOCGIFADDR = 0xC0206921 if sys.platform == "darwin" else 0x8915
_PREFERRED_PREFIXES = ("en", "eth", "wlan", "wl")
_SKIPPED_PREFIXES = (
    "lo", "utun", "tun", "tap", "gif", "stf", "awdl", "llw", "nan",
    "bridge", "vmenet", "vmnet", "ap", "anpi",
)
_LAN_NETS = (
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
)


def _interface_addresses() -> list[tuple[str, str]]:
    """枚举本机 IPv4（``SIOCGIFADDR`` 在 macOS 与 Linux 上都能用）。"""
    found: list[tuple[str, str]] = []
    for _index, name in socket.if_nameindex():
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            request = struct.pack("16s24x", name.encode()[:15])
            raw = fcntl.ioctl(probe.fileno(), _SIOCGIFADDR, request, True)
        except OSError:  # 没配 IPv4 的网卡（AWS Wi-Fi、utun）会直接报错
            continue
        finally:
            probe.close()
        found.append((name, socket.inet_ntoa(raw[20:24])))
    return found


def _is_lan(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in _LAN_NETS)


def local_ip() -> str:
    candidates = _interface_addresses()
    for name, ip in candidates:
        if name.startswith(_PREFERRED_PREFIXES) and _is_lan(ip):
            return ip
    for name, ip in candidates:
        if not name.startswith(_SKIPPED_PREFIXES) and _is_lan(ip):
            return ip

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(_PROBE)
        routed = probe.getsockname()[0]
    except OSError:  # 没有默认路由（离线开发机）时退回家环
        return "127.0.0.1"
    finally:
        probe.close()
    return "127.0.0.1" if routed.startswith("127.") else routed


class XiaozhiServer:
    """一个 asyncio TCP 服务，手上有两条路由：OTA 与 WS。

    ``ws_url`` 不传就按「本机 IP + 实际监听端口」生成；用 ``port=0``（测试）
    时以真正绑上的端口为准。
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 4412,
        ws_url: str | None = None,
        request_timeout: float = REQUEST_TIMEOUT,
        log=print,
        pipeline: voice_module.VoicePipeline | None = None,
        codec_factory=None,
        vad: VadSettings | None = None,
        idle_seconds: float | None = None,
        barge: BargeSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.host = host
        self.port = port
        self._ws_url = ws_url
        self.request_timeout = request_timeout
        self._log = log
        self._pipeline = pipeline
        # 测试接缝：假编解码器；None 时 ws.Conversation 用真的 audio.Codec。
        self._codec_factory = codec_factory
        # 端点检测参数：启动时读一次环境变量，连上的会话共用同一份（可注入覆盖）。
        self._vad = vad or vad_settings_from_env()
        # 会话窗口、打断判据与宽限期：启动时读一次环境变量，可注入覆盖。
        self._idle_seconds = (
            session_idle_seconds_from_env() if idle_seconds is None else idle_seconds
        )
        self._barge = barge or barge_settings_from_env()
        self._barge_grace_ms = barge_grace_ms_from_env()
        # 播报诊断：下一次写 [barge] 诊断日志的时刻（None = 没在播报）。
        self._barge_diag_at: float | None = None
        self._clock = clock
        self._last_activity = 0.0  # 最后一次有人说话（切出一句）的时刻
        self._speaking_since: float | None = None  # 本次播报开始推送的时刻
        self._busy = False
        self._server: asyncio.AbstractServer | None = None
        self._turn_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()

    @property
    def ws_url(self) -> str:
        if self._ws_url:
            return self._ws_url
        return f"ws://{local_ip()}:{self.port}{WS_PATH}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        vad = self._vad
        self._log(
            f"[vad] 端点检测生效参数：阈值={vad.threshold} 帧长={vad.frame_ms}ms "
            f"尾静音={vad.tail_ms}ms 空闲超时={vad.idle_seconds}s 单句上限={vad.max_utterance_ms}ms"
        )
        self._log(
            f"[session] 空闲退出窗口 {self._idle_seconds:g}s"
            f"（连续无有效语音即播结束语回到待命）"
        )
        self._log(
            f"[barge] 打断判定：宽限 {self._barge_grace_ms}ms，"
            f"滑窗 {self._barge.window_ms}ms 高能占比 ≥ {self._barge.ratio:.0%}"
        )
        self._log(
            f"[asr] prompt={'on' if asr_module.prompt_setting() else 'off'}"
            f"（领域提示词随识别请求下发；服务端不认识时自动退化重试）"
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    # --- 连接生命周期 -----------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """每条连接一个任务；任何协议错误都只关这一条连接。"""
        try:
            await self._dispatch(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # 对端先走了，正常
        except wire.WireError as exc:
            self._log(f"[ws] 线协议错误：{exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 一条连接不能把整个服务带走
            self._log(f"[ws] 未预期错误：{type(exc).__name__}: {exc}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _dispatch(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await wire.read_request(reader, timeout=self.request_timeout)
        except wire.WireError as exc:
            # 请求行都没读出来也要回一句：一个字节都不回，板子只能记「连上又断」。
            self._log(f"[http] 请求读不下来（{exc}），回 {exc.status}")
            await self._respond(
                writer, exc.status, {"error": "bad request", "message": str(exc)}
            )
            return

        if request.note:
            self._log(f"[http] {request.method} {request.path}：{request.note}")

        if request.path == WS_PATH:
            await self._serve_ws(request, reader, writer)
            return

        handler = self._route(request.path)
        if handler is None:
            self._log(f"[http] {request.method} {request.path} -> 404")
            await self._respond(writer, 404, {"error": "not found", "path": request.path})
        else:
            try:
                await handler(request, writer)
            except Exception as exc:  # 处理函数的锅，不许变成「静默断开」
                self._log(
                    f"[http] {request.method} {request.path} 处理失败："
                    f"{type(exc).__name__}: {exc}"
                )
                await self._respond(
                    writer, 500, {"error": "internal server error", "path": request.path}
                )

        if request.leftover:
            # 对端还在发、我们却直接 close → RST 可能把刚写好的响应一起丢掉。
            await wire.drain_quietly(reader)

    def _route(self, path: str):
        """路径 → 处理函数；认不出来返回 ``None``（调用方回 404 JSON）。"""
        if path in ota.OTA_PATHS:
            return self._handle_ota
        if path in mute.MUTE_PATHS:
            return self._handle_mute
        return None

    async def _handle_ota(
        self, request: wire.Request, writer: asyncio.StreamWriter
    ) -> None:
        """OTA：GET 与 POST 同响应（GET 是手测用的，固件发 POST）。"""
        if request.method not in ("GET", "POST"):
            await self._respond(writer, 405, {"error": "method not allowed"})
            return
        self._log(f"[ota] {ota.describe(request.headers, request.body)} -> {self.ws_url}")
        await self._respond(writer, 200, ota.build_payload(self.ws_url))

    async def _handle_mute(
        self, request: wire.Request, writer: asyncio.StreamWriter
    ) -> None:
        """``GET /debug/mute``：固件补丁每轮都问，M3a 固定回答「未静音」。"""
        if request.method != "GET":
            await self._respond(writer, 405, {"error": "method not allowed"})
            return
        self._log(f"[mute] device={request.header('device-id') or '-'} -> muted=false")
        await self._respond(writer, 200, mute.build_status())

    async def _respond(
        self, writer: asyncio.StreamWriter, status: int, payload: dict
    ) -> None:
        """写一条 JSON 响应；对端已经走了就记一行，不往上抛。"""
        try:
            await wire.write_response(writer, status, payload)
        except (ConnectionError, OSError) as exc:
            self._log(f"[http] 回 {status} 时对端已走：{type(exc).__name__}: {exc}")

    # --- WebSocket --------------------------------------------------------

    async def _serve_ws(
        self,
        request: wire.Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            if not wire.is_upgrade(request):
                await self._respond(writer, 400, {"error": "expect websocket upgrade"})
                return
            if self._busy:
                self._log("[ws] 已有连接，拒绝第二个（alpha 只支持 1 个）")
                await self._respond(
                    writer, 503, {"error": "busy", "message": "alpha 只支持 1 个连接"}
                )
                return

            self._busy = True
            session = Conversation(
                uuid.uuid4().hex[:8],
                codec_factory=self._codec_factory,
                vad=self._vad,
                log=self._log,
            )
            self._log(f"[ws] 连接建立 session={session.session_id}")
            self._last_activity = self._clock()
            await wire.accept(writer, request)
        except Exception as exc:  # 握手阶段出错：能回 HTTP 就回，别让板子空等
            self._busy = False
            self._log(f"[ws] 握手失败：{type(exc).__name__}: {exc}")
            await self._respond(writer, 500, {"error": "handshake failed"})
            return

        try:
            await self._read_loop(session, reader, writer)
        finally:
            await self._stop_turn(tell_device=False)
            self._busy = False
            self._log(
                f"[ws] 连接断开 session={session.session_id} "
                f"音频 {session.audio_bytes} 字节 / {session.stray_packets} 个窗口外帧"
            )

    async def _read_loop(
        self,
        session: Conversation,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        fragments = bytearray()
        fragment_opcode: int | None = None
        audio_error_reported = False

        while True:
            frame = await wire.read_frame(reader)

            if frame.opcode == wire.OP_CLOSE:
                await wire.send(writer, wire.encode_close())
                return
            if frame.opcode == wire.OP_PING:
                await wire.send(writer, wire.encode_frame(wire.OP_PONG, frame.payload))
                continue
            if frame.opcode == wire.OP_PONG:
                continue

            if frame.opcode == wire.OP_CONT:
                if fragment_opcode is None:
                    raise wire.WireError("收到没有起始帧的续帧")
                fragments += frame.payload
                if not frame.fin:
                    continue
                opcode, payload = fragment_opcode, bytes(fragments)
                fragments = bytearray()
                fragment_opcode = None
            elif frame.opcode in (wire.OP_TEXT, wire.OP_BINARY):
                if not frame.fin:
                    fragment_opcode = frame.opcode
                    fragments = bytearray(frame.payload)
                    continue
                opcode, payload = frame.opcode, frame.payload
            else:
                raise wire.WireError(f"未知操作码 {frame.opcode:#x}")

            if opcode == wire.OP_BINARY:
                # 打断判定要在解码前 arm：宽限期一过，紧跟着的包就要开始计数。
                self._arm_barge_if_due(session)
                session.on_audio(payload)
                if session.audio_error and not audio_error_reported:
                    # 每条连接只说一次：坏的是环境（缺 libopus），不是这一包。
                    self._log(f"[audio] 解码不可用：{session.audio_error}")
                    audio_error_reported = True
                if session.take_speech_tick():
                    # 有人说话（含唤醒词回声）：空闲计时从这里重新数。
                    self._last_activity = self._clock()
                self._log_barge_diag(session)
                if await self._maybe_barge(session, writer):
                    continue
                # realtime 模式板子永不发 listen stop：端点检测判完这句就交出去。
                # listen stop 路径（手动模式）不受影响——两条路都汇到 take_utterance。
                if session.take_endpoint():
                    if self._speaking_since is not None:
                        # 播报期间的语音没达打断判据（达到了已在上面打断）：
                        # 不另起回合，免得把自己的回声当提问自问自答。
                        self._discard_utterance_while_speaking(session)
                    else:
                        utterance = session.take_utterance()
                        if utterance is not None:
                            await self._start_turn(session, writer, utterance)
                    continue
                # 空闲退出只在「没在播报」时判：播报本身说明刚刚还有人说话。
                if self._speaking_since is None and self._idle_expired():
                    await self._session_close(session, writer)
                    return
                continue

            for message in session.on_text(payload.decode("utf-8", "replace")):
                await self._send_json(writer, message)
            if session.params_reason:
                self._log(f"[hello] {session.params_reason}，回落 16 kHz / 60 ms")
                session.params_reason = ""
            if session.take_abort():
                await self._stop_turn(
                    session_id=session.session_id, writer=writer, tell_device=True
                )

            # 只有 listen stop 才能把缓冲交出去：窗口里插进来的未知文本
            # （mcp / iot / 坏 JSON）不该把半截音频送去识别。
            if session.take_stop():
                utterance = session.take_utterance()
                if utterance is not None:
                    await self._start_turn(session, writer, utterance)

    # --- 一轮语音 ----------------------------------------------------------

    def _sender_for(self, writer):
        """包一层发送回调：记下播报开始/结束推送的时刻，供打断判定与空闲退出用。"""

        async def sender(item) -> None:
            if isinstance(item, dict) and item.get("type") == "tts":
                if item.get("state") == "start":
                    self._speaking_since = self._clock()
                    self._barge_diag_at = self._clock() + 1.0
                elif item.get("state") == "stop":
                    self._speaking_since = None
                    self._barge_diag_at = None
            await self._send(writer, item)

        return sender

    def _arm_barge_if_due(self, session: Conversation) -> None:
        """播报已超过宽限期 → 允许打断判定（arm 之前的上行帧不进滑窗）。"""
        if self._speaking_since is None or session.barge_armed:
            return
        if (self._clock() - self._speaking_since) * 1000 > self._barge_grace_ms:
            session.arm_barge()

    def _log_barge_diag(self, session: Conversation) -> None:
        """播报期间每秒一条 [barge] 诊断：常驻打印，给后续真机调参用。"""
        if (
            self._barge_diag_at is None
            or self._speaking_since is None
            or self._clock() < self._barge_diag_at
        ):
            return
        self._barge_diag_at = self._clock() + 1.0
        peak = session.take_barge_peak_rms()
        self._log(
            f"[barge] 峰值rms={peak:.4f} 高能占比={session.barge_ratio * 100:.0f}%"
        )

    async def _maybe_barge(self, session: Conversation, writer) -> bool:
        """上行滑窗高能占比达到判据且已过宽限 → 打断当前播报。命中返回 True。

        动作：取消播报任务（剩余帧由流水线丢弃并计数）、下发 ``tts stop``、
        **不清上行缓冲**——触发打断的这段音频作为新一句话的开头继续断句。
        """
        if self._speaking_since is None or not session.barge_armed:
            return False
        if session.barge_ratio < self._barge.ratio:
            return False
        started_ms = int((self._clock() - self._speaking_since) * 1000)
        self._speaking_since = None
        await self._stop_turn(session_id=session.session_id, writer=writer, tell_device=True)
        dropped = self._pipeline.dropped_frames if self._pipeline is not None else None
        self._log(f"[barge] 说话开始后 {started_ms} ms 被打断，丢弃 {dropped or 0} 帧")
        return True

    def _discard_utterance_while_speaking(self, session: Conversation) -> None:
        """播报期间切出的语音（多半是自家回声）不另起回合，丢掉并留一行日志。"""
        utterance = session.take_utterance()
        if utterance is not None:
            self._log(
                f"[turn] 播报中的 {utterance.seconds:.2f}s 音频未达打断判据，忽略"
            )

    def _idle_expired(self) -> bool:
        return (self._clock() - self._last_activity) >= self._idle_seconds

    async def _session_close(self, session: Conversation, writer) -> None:
        """空闲超时：收尾语 → 发 close 帧 → 读循环返回，连接关闭回到待命。"""
        await self._stop_turn(session_id=session.session_id, writer=writer, tell_device=True)
        self._log(f"[session] 空闲 {self._idle_seconds:g}s，回到待命")
        if self._pipeline is not None:
            try:
                await self._pipeline.announce(
                    voice_module.SESSION_CLOSE_SPOKEN,
                    session.output_params,
                    self._sender_for(writer),
                    session_id=session.session_id,
                )
            except (ConnectionError, OSError) as exc:
                self._log(f"[session] 结束语没送出去：{type(exc).__name__}: {exc}")
        with contextlib.suppress(ConnectionError, OSError, wire.WireError):
            await wire.send(writer, wire.encode_close())

    async def _start_turn(self, session: Conversation, writer, utterance) -> None:
        """``listen stop`` 或端点检测切句之后把音频交给流水线；新任务取代旧任务。

        先 ``await`` 掉旧任务再起新的：只 ``cancel()`` 不等待的话，旧任务可能刚好
        在写最后一帧，和新一轮的 ``tts start`` 抢顺序。
        """
        if self._pipeline is None:
            self._log(
                f"[turn] 语音链路未配置（没接数据库/ASR），丢掉 {utterance.seconds:.2f}s 音频"
            )
            return
        await self._stop_turn(tell_device=False)
        self._turn_task = asyncio.create_task(
            self._run_turn(session, writer, utterance),
            name=f"turn-{session.session_id}",
        )
    async def _run_turn(self, session: Conversation, writer, utterance) -> None:
        try:
            # 下行按 output_params（24k）编码；上行解码参数只管 ASR 那头，不进这里。
            # sender 包了一层：tts start/stop 时记录播报时刻，供打断判定用。
            await self._pipeline.handle(
                utterance,
                session.output_params,
                self._sender_for(writer),
                session_id=session.session_id,
            )
        except asyncio.CancelledError:
            self._log(f"[turn] 已打断 session={session.session_id}")
            raise
        except (ConnectionError, OSError) as exc:
            self._log(f"[turn] 推送失败（对端可能已走）：{type(exc).__name__}: {exc}")
        except Exception as exc:  # 流水线自己崩了也不能把连接带走
            self._log(f"[turn] 未预期错误：{type(exc).__name__}: {exc}")

    async def _stop_turn(
        self, *, session_id: str = "", writer=None, tell_device: bool = False
    ) -> None:
        """取消正在播报的回合；``tell_device`` 时补一条 ``tts stop``。

        参考实现（``esp32-voice-entry/src/session.py:_interrupt``）在打断时同样补
        ``tts stop``：板子靠它把状态从 speaking 拉回 idle。连接要断的时候就不补了，
        对端已经走了。
        """
        task = self._turn_task
        self._turn_task = None
        self._speaking_since = None
        self._barge_diag_at = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if tell_device and writer is not None:
            # 取消发生在推送中间时，板子的播放器还停在 speaking；一条 stop 收尾。
            with contextlib.suppress(ConnectionError, OSError, wire.WireError):
                await self._send_json(writer, tts_stop(session_id))

    # --- 发送 --------------------------------------------------------------

    async def _send(self, writer: asyncio.StreamWriter, item) -> None:
        """一条下行：dict 走文本帧，bytes 走二进制帧。所有写串行化。

        ``wire.send`` 只负责 ``write + drain``，帧头要在这里套——直接写裸字节，
        板子会把音频包的开头当成帧头解析，整条流从此错位。
        """
        async with self._send_lock:
            if isinstance(item, (bytes, bytearray)):
                await wire.send(writer, wire.encode_frame(wire.OP_BINARY, bytes(item)))
            else:
                await wire.send_json(writer, item)

    async def _send_json(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        await self._send(writer, payload)
