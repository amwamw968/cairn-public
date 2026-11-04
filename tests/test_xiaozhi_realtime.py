"""realtime 连续推流的服务端端点检测（VAD）与下行 24 kHz 对齐。

真机实测（2026-10-31，小智固件 2.5.3）驱动：
- 板子以 ``mode=realtime`` 连续推 Opus（24 秒 421 包），**永远不发 listen stop**
  ——服务端必须自己断句：按 30 ms 帧算 RMS，说话后持续 ≥0.5s 低能量即这句结束；
- 板子喇叭是 24 kHz：hello 回复声明 16k 时串口报
  ``Server sample rate 16000 does not match device output sample rate 24000``，
  下行必须按 24k 编码，16k 的 TTS 源在服务端重采样。

不碰真板子：状态机用假编解码器，e2e 用假设备客户端 + 假流水线。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import math
import os
import re
import struct
import wave

import pytest

from cairn.voice.asr import ASREmptyResultError
from cairn.voice.xiaozhi import audio, wire
from cairn.voice.xiaozhi.server import (
    BARGE_GRACE_MS,
    WS_PATH,
    XiaozhiServer,
    barge_grace_ms_from_env,
    session_idle_seconds_from_env,
)
from cairn.voice.xiaozhi.voice import ASR_FALLBACK, SESSION_CLOSE_SPOKEN, VoicePipeline
from cairn.voice.xiaozhi.ws import (
    DOWNLINK_PARAMS,
    UPLINK_PARAMS,
    VAD_MAX_UTTERANCE_MS,
    BargeSettings,
    VadSettings,
    Conversation,
    barge_settings_from_env,
    vad_settings_from_env,
)

TIMEOUT = 3.0
OPUS_READY, OPUS_REASON = audio.available()
needs_opus = pytest.mark.skipif(not OPUS_READY, reason=f"没有 libopus：{OPUS_REASON}")

SPOKEN = "好的，我们开始。"


# --- 脚手架：按包内容出声的假编解码 -------------------------------------------


class LoudCodec:
    """接口与 ``audio.Codec`` 一致：b"loud" 开头的包出正弦声，否则数字静音。

    正弦幅度 6000，RMS ≈ 6000 / 1.414 / 32768 ≈ 0.13，远高于阈值 0.0025；
    静音包是全零，RMS = 0，远低于阈值。
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1, frame_ms: int = 60):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.frame_samples = sample_rate * frame_ms // 1000
        self.frame_bytes = self.frame_samples * channels * 2

    def decode(self, packet: bytes) -> bytes:
        if not packet:
            return b""
        if packet.startswith(b"loud"):
            return b"".join(
                struct.pack(
                    "<h",
                    int(6000 * math.sin(2 * math.pi * 440 * i / self.sample_rate)),
                )
                for i in range(self.frame_samples)
            )
        return b"\x00\x00" * self.frame_samples

    def encode(self, pcm: bytes) -> bytes:
        assert len(pcm) == self.frame_bytes, (len(pcm), self.frame_bytes)
        return b"OPUS" + len(pcm).to_bytes(2, "big")

    def frames(self, pcm: bytes):
        for offset in range(0, len(pcm), self.frame_bytes):
            chunk = pcm[offset : offset + self.frame_bytes]
            if len(chunk) < self.frame_bytes:
                chunk = chunk + b"\x00" * (self.frame_bytes - len(chunk))
            yield self.encode(chunk)


LOUD = b"loud" + b"\x00" * 16
QUIET = b"quiet" + b"\x00" * 15


def realtime_session(**kwargs) -> Conversation:
    session = Conversation("s1", codec_factory=LoudCodec, **kwargs)
    session.on_text('{"type":"listen","state":"start","mode":"realtime"}')
    return session


def burst(session: Conversation, packet: bytes, count: int) -> None:
    for _ in range(count):
        session.on_audio(packet)


def tone_wav(seconds: float, rate: int = 16000) -> bytes:
    """一段 440 Hz 正弦的 WAV（假 TTS 的产物，16 kHz 源）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return buffer.getvalue()


def null_factory():
    return contextlib.nullcontext(None)


class FakeTurn:
    spoken = SPOKEN
    next_step = ""
    kind = "answer"


# --- 端点检测：状态机 ---------------------------------------------------------


def test_realtime_loud_silence_loud_yields_two_utterances():
    """响 300ms → 静 600ms → 响 300ms（→ 静 600ms）：必须切出两段 utterance。"""
    session = realtime_session()

    burst(session, LOUD, 5)  # 5 × 60ms = 300ms 说话
    assert session.take_endpoint() is False, "说话中不许切句"

    burst(session, QUIET, 10)  # 10 × 60ms = 600ms 尾静音（≥ 500ms）
    assert session.take_endpoint() is True, "持续 600ms 低能量必须判定这句结束"
    first = session.take_utterance()
    assert first is not None
    assert first.seconds >= 0.8, f"第一句要含说话段+尾静音，实际 {first.seconds:.2f}s"
    assert session.take_endpoint() is False, "事件只能被消费一次"
    assert session.take_utterance() is None, "缓冲必须已清空"

    burst(session, LOUD, 5)
    burst(session, QUIET, 10)
    assert session.take_endpoint() is True, "同一条连接要能连续切第二句"
    second = session.take_utterance()
    assert second is not None
    assert session.take_utterance() is None


def test_short_silence_does_not_cut_utterance():
    """说话中只静了 300ms（< 500ms）不许切——词间停顿不是句尾。"""
    session = realtime_session()

    burst(session, LOUD, 5)
    burst(session, QUIET, 5)  # 300ms
    assert session.take_endpoint() is False

    burst(session, LOUD, 5)  # 继续说话：尾静音计数应归零
    burst(session, QUIET, 10)
    assert session.take_endpoint() is True
    utterance = session.take_utterance()
    assert utterance is not None
    assert utterance.seconds >= 1.1, "中间 300ms 停顿的音频不能丢"


def test_pure_silence_20s_clears_buffer_without_endpoint():
    """连续 20s 无语音：清缓冲重来，不切 utterance、不抛异常。"""
    session = realtime_session(vad=VadSettings(idle_seconds=20.0))

    burst(session, QUIET, 334)  # 334 × 60ms = 20.04s，越过 20s 线
    assert session.take_endpoint() is False
    assert session.pending_seconds < 0.1, "越线瞬间缓冲必须已被清空"

    # 清空之后从头听：说话照常能切句
    burst(session, LOUD, 5)
    burst(session, QUIET, 10)
    assert session.take_endpoint() is True
    assert session.take_utterance() is not None


def test_idle_clear_restarts_the_20s_window():
    """清空之后不再累计旧的静音时长：再来 10s 静音不该触发第二次清空判据。"""
    session = realtime_session(vad=VadSettings(idle_seconds=20.0))

    burst(session, QUIET, 334)  # 20.04s → 触发清空
    burst(session, QUIET, 166)  # 新窗口里再积 ~10s，没满
    assert session.take_endpoint() is False
    assert session.pending_seconds > 5, "新窗口的静音留在缓冲里等下文"


# --- 单句最长时长硬上限（2026-10-31 评审修复） ---------------------------------


def test_utterance_hard_cap_forces_cut_at_30s_and_keeps_remainder():
    """连续 40s 高能量（一口气不换气）：恰好在上限处切出 1 条，剩余进下一句。

    钉住三件事：切出的这句时长就是上限（29.9–30.1s）；切句后缓冲有界
    （不是把 40s 全留着，也不是清空丢掉剩余 10s）；日志有一条强制切句记录。
    """
    logs: list[str] = []
    session = Conversation("s1", codec_factory=LoudCodec, log=logs.append)
    session.on_text('{"type":"listen","state":"start","mode":"realtime"}')

    burst(session, LOUD, 667)  # 667 × 60ms = 40.02s 连续说话
    assert session.take_endpoint() is True, "超过单句上限必须强制切句"
    first = session.take_utterance()
    assert first is not None
    assert 29.9 <= first.seconds <= 30.1, f"切点应在上限处，实际 {first.seconds:.2f}s"

    # 缓冲有界：只剩 40.02 - 30.00 = 10.02s 的剩余音频，不能更多也不能为空
    frame = 16000 * 2  # 16 kHz 单声道 s16le 每秒字节
    assert 0 < len(session._pcm) <= int(frame * 10.5), (
        f"剩余音频必须留在缓冲里进入下一句且有界：{len(session._pcm)} 字节"
    )

    # 剩余音频接着当下一句：补 600ms 静音照常能切出第二句
    burst(session, QUIET, 10)
    assert session.take_endpoint() is True
    second = session.take_utterance()
    assert second is not None
    assert second.seconds >= 10.0, f"剩余 ~10s 不能丢，实际 {second.seconds:.2f}s"

    [line] = [line for line in logs if "强制切句" in line]
    assert "30000ms" in line and "已收 30.00 s" in line, line


def test_max_utterance_ms_env_override_cuts_long_speech_into_pieces():
    """上限覆盖成 500ms：1.8s 连续说话被切成多条，证明参数真的生效。"""
    session = realtime_session(
        vad=vad_settings_from_env({"CAIRN_VAD_MAX_UTTERANCE_MS": "500"})
    )

    burst(session, LOUD, 30)  # 30 × 60ms = 1.8s 连续说话（分批推，切句要靠新帧驱动）
    pieces = []
    for _ in range(6):
        burst(session, LOUD, 5)  # 每批 0.3s
        while session.take_endpoint():
            piece = session.take_utterance()
            assert piece is not None
            pieces.append(piece)
    assert len(pieces) >= 3, f"0.5s 上限下 1.8s 必须切出多条，实际 {len(pieces)} 条"
    for piece in pieces:
        assert 0.45 <= piece.seconds <= 0.55, f"每条都应≈上限，实际 {piece.seconds:.2f}s"


def test_max_utterance_default_and_startup_log():
    """默认 30000；启动 [vad] 日志随其他参数一起打印单句上限。"""
    assert VAD_MAX_UTTERANCE_MS == 30000
    assert vad_settings_from_env({}).max_utterance_ms == 30000
    assert (
        vad_settings_from_env({"CAIRN_VAD_MAX_UTTERANCE_MS": "45000"}).max_utterance_ms
        == 45000
    )

    logs: list[str] = []

    async def main():
        server = XiaozhiServer(
            host="127.0.0.1",
            port=0,
            ws_url="ws://127.0.0.1/xiaozhi/ws",
            log=logs.append,
            vad=VadSettings(max_utterance_ms=45000),
        )
        await server.start()
        await server.close()

    asyncio.run(main())
    [line] = [line for line in logs if line.startswith("[vad]")]
    assert "单句上限=45000ms" in line, line


# --- hello：下行 24 kHz，上行仍 16k -------------------------------------------


def test_hello_reply_declares_24k_downlink_and_keeps_16k_uplink():
    session = Conversation("s1", codec_factory=LoudCodec)
    [reply] = session.on_text(
        json.dumps(
            {
                "type": "hello",
                "version": 1,
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }
        )
    )
    assert reply["audio_params"]["sample_rate"] == 24000, "板子喇叭是 24k，回复必须声明它"
    assert reply["audio_params"]["format"] == "opus"
    assert reply["audio_params"]["channels"] == 1
    assert session.params["sample_rate"] == 16000, "上行解码仍按设备声明的 16k"
    assert session.output_params["sample_rate"] == 24000
    # 常量本身就要分开：上行 16k、下行 24k
    assert UPLINK_PARAMS["sample_rate"] == 16000
    assert DOWNLINK_PARAMS["sample_rate"] == 24000


def test_hello_reply_defaults_to_24k_even_without_device_params():
    session = Conversation("s1", codec_factory=LoudCodec)
    [reply] = session.on_text('{"type":"hello","version":1}')
    assert reply["audio_params"]["sample_rate"] == 24000


def test_uplink_rate_mismatch_still_falls_back_to_16k_with_reason():
    """设备声明的上行率不是 16k：回落 16k 并留原因（下行照旧 24k）。"""
    session = Conversation("s1", codec_factory=LoudCodec)
    [reply] = session.on_text(
        json.dumps(
            {
                "type": "hello",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 48000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }
        )
    )
    assert session.params["sample_rate"] == 16000
    assert "48000" in session.params_reason
    assert reply["audio_params"]["sample_rate"] == 24000


# --- 下行：24 kHz 编码 ---------------------------------------------------------


@needs_opus
def test_downlink_opus_frames_decode_at_24k():
    """24 kHz Codec 编码的帧解回来就是 24k 口径：60ms = 1440 样本 = 2880 字节。"""
    codec = audio.Codec(24000, 1, 60)
    assert codec.frame_samples == 1440
    pcm = b"".join(
        struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / 24000)))
        for i in range(codec.frame_samples * 2)
    )
    packets = list(codec.frames(pcm))
    assert len(packets) == 2
    decoded = codec.decode(packets[0])
    assert len(decoded) == 24000 * 60 // 1000 * 2, "解出来的必须是 24k 口径的帧"
    assert any(decoded)


@needs_opus
def test_resample_16k_to_24k_preserves_duration_and_content():
    """16k → 24k 线性重采样：时长精确 ×1.5，内容还是那段声音。"""
    rate = 16000
    samples = [int(6000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)]
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    out = audio.resample(pcm, from_rate=16000, to_rate=24000)
    out_samples = struct.unpack(f"<{len(out) // 2}h", out)
    assert len(out_samples) == 24000, "1 秒进来必须 1 秒出去（24000 样本）"
    assert audio.resample(pcm, from_rate=16000, to_rate=16000) is pcm, "同率原样返回"
    peak = max(abs(value) for value in out_samples)
    assert 5000 < peak <= 6000, "重采样不许削掉声音"
    with pytest.raises(audio.AudioError):
        audio.resample(pcm, from_rate=16000, to_rate=0)
    with pytest.raises(audio.AudioError):
        audio.resample(pcm, from_rate=16000, to_rate=24000, channels=2)


@needs_opus
def test_downlink_pipeline_encodes_at_24k():
    """流水线下行：编码器按 output_params（24k）建，帧口径是 24k 的。"""
    pipeline = VoicePipeline(log=lambda *_: None, codec_factory=audio.Codec)
    codec = pipeline._encoder(dict(DOWNLINK_PARAMS))
    assert codec.sample_rate == 24000
    pcm = audio.resample(
        audio.wav_to_pcm(tone_wav(0.3), sample_rate=16000, channels=1),
        from_rate=16000,
        to_rate=24000,
    )
    packets = list(codec.frames(pcm))
    assert len(packets) == 5, "0.3s @ 24k / 60ms = 5 帧"
    for packet in packets:
        decoded = audio.Codec(24000, 1, 60).decode(packet)
        assert len(decoded) == 24000 * 60 // 1000 * 2


# --- e2e：realtime 连续推流跑完两轮 -------------------------------------------


async def read_server_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first, second = await reader.readexactly(2)
    assert not second & 0x80
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    return opcode, await reader.readexactly(length)


class FakeDevice:
    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(cls, port: int) -> "FakeDevice":
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {WS_PATH} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Device-Id: 80b54ecd0f64\r\n"
            "\r\n"
        )
        writer.write(request.encode("latin-1"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        assert b"101" in head, f"握手失败：{head!r}"
        return cls(reader, writer)

    async def send_json(self, payload: dict) -> None:
        await self.send_frame(wire.OP_TEXT, json.dumps(payload).encode("utf-8"))

    async def send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        head = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            head.append(0x80 | length)
        else:
            head.append(0x80 | 126)
            head += length.to_bytes(2, "big")
        self._writer.write(bytes(head) + mask + body)
        await self._writer.drain()

    async def recv(self) -> tuple[int, object]:
        opcode, payload = await asyncio.wait_for(read_server_frame(self._reader), TIMEOUT)
        if opcode == wire.OP_TEXT:
            return opcode, json.loads(payload)
        return opcode, payload

    async def recv_until_stop(self, limit: int = 400) -> list[tuple[int, object]]:
        seen: list[tuple[int, object]] = []
        for _ in range(limit):
            item = await self.recv()
            seen.append(item)
            if (
                item[0] == wire.OP_TEXT
                and isinstance(item[1], dict)
                and item[1].get("state") == "stop"
            ):
                return seen
        raise AssertionError(f"{limit} 条消息内没等到 tts stop")

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except ConnectionError:
            pass


def run_realtime(scenario, *, server_kwargs: dict | None = None, **pipeline_overrides):
    """起一台带假流水线的 realtime 服务；``pipeline_overrides`` 覆盖默认假件，
    ``server_kwargs`` 传给 ``XiaozhiServer``（空闲窗口 / 打断参数等）。"""

    async def main():
        logs: list[str] = []
        pipeline_kwargs = dict(
            log=logs.append,
            codec_factory=LoudCodec,
            session_factory=null_factory,
            transcriber=lambda wav: "你好小智",
            responder=lambda session, learner, text: FakeTurn(),
            synthesizer=lambda text: tone_wav(0.2),
        )
        pipeline_kwargs.update(pipeline_overrides)
        pipeline = VoicePipeline(**pipeline_kwargs)
        server = XiaozhiServer(
            host="127.0.0.1",
            port=0,
            ws_url="ws://127.0.0.1/xiaozhi/ws",
            log=logs.append,
            pipeline=pipeline,
            codec_factory=LoudCodec,
            **(server_kwargs or {}),
        )
        await server.start()
        try:
            return await scenario(server), logs
        finally:
            await server.close()

    return asyncio.run(main())


def test_realtime_e2e_two_rounds_without_listen_stop():
    """realtime 推流、一条 stop 都不发：两轮「响→静」各自跑完 tts 序列。

    同时钉住：下行文本里只有 turn.spoken，端点判定的内部状态绝不外泄。
    """
    heard: list[bytes] = []

    def transcribe(wav: bytes) -> str:
        heard.append(wav)
        return "你好小智"

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            rounds = []
            for _ in range(2):
                for _ in range(5):
                    await device.send_frame(wire.OP_BINARY, LOUD)
                for _ in range(10):
                    await device.send_frame(wire.OP_BINARY, QUIET)
                rounds.append(await device.recv_until_stop())
            return rounds
        finally:
            await device.close()

    (rounds), logs = run_realtime(scenario, transcriber=transcribe)

    assert len(heard) == 2, f"两轮各送一次 ASR，实际 {len(heard)} 次"
    for messages in rounds:
        assert messages[0][1]["type"] == "tts" and messages[0][1]["state"] == "start"
        assert messages[-1][1]["type"] == "tts" and messages[-1][1]["state"] == "stop"
    texts = [
        payload
        for messages in rounds
        for opcode, payload in messages
        if opcode == wire.OP_TEXT
    ]
    for message in texts:
        dumped = json.dumps(message, ensure_ascii=False)
        assert dumped.find("endpoint") == -1 and dumped.find("True") == -1
        if message.get("state") == "sentence_start":
            assert message["text"] == SPOKEN, "下行话只能来自 turn.spoken"
    assert any("hello" in line or "vad" in line for line in logs)


def test_server_start_logs_effective_vad_settings():
    """启动日志要打印实际生效的端点检测参数。"""
    logs: list[str] = []

    async def main():
        server = XiaozhiServer(
            host="127.0.0.1",
            port=0,
            ws_url="ws://127.0.0.1/xiaozhi/ws",
            log=logs.append,
            vad=VadSettings(threshold=0.01, frame_ms=20, tail_ms=400, idle_seconds=5.0),
        )
        await server.start()
        await server.close()

    asyncio.run(main())
    [line] = [line for line in logs if line.startswith("[vad]")]
    assert "0.01" in line and "20ms" in line and "400ms" in line and "5.0s" in line


# --- 环境变量覆盖 -------------------------------------------------------------


def test_vad_settings_from_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("CAIRN_VAD_THRESHOLD", "0.004")
    monkeypatch.setenv("CAIRN_VAD_FRAME_MS", "20")
    monkeypatch.setenv("CAIRN_VAD_TAIL_MS", "300")
    monkeypatch.setenv("CAIRN_VAD_IDLE_SECONDS", "10.5")
    assert vad_settings_from_env() == VadSettings(
        threshold=0.004, frame_ms=20, tail_ms=300, idle_seconds=10.5
    )


def test_vad_settings_defaults_match_constants():
    assert vad_settings_from_env({}) == VadSettings()


def test_vad_settings_rejects_broken_values(monkeypatch):
    monkeypatch.setenv("CAIRN_VAD_THRESHOLD", "abc")
    with pytest.raises(ValueError, match="CAIRN_VAD_THRESHOLD"):
        vad_settings_from_env()


def test_env_overrides_change_endpointing_behaviour():
    """tail_ms=100 时 120ms 静音就切句（默认 500ms 不会）——参数确实生效。"""
    session = realtime_session(vad=vad_settings_from_env({"CAIRN_VAD_TAIL_MS": "100"}))
    burst(session, LOUD, 5)
    burst(session, QUIET, 2)  # 120ms
    assert session.take_endpoint() is True


# --- 会话窗口 / 唤醒词回声静默 / 打断（barge-in） -------------------------------
#
# 真机实测（2026-10-31 13:50）驱动：唤醒词回声被当成一句话，助手对着空气道歉。
# 三件事：回声静默重置、300s 空闲退出、播报可打断。


async def device_burst(device, packet: bytes, count: int) -> None:
    for _ in range(count):
        await device.send_frame(wire.OP_BINARY, packet)


async def kick_off_turn(device) -> None:
    """响 300ms → 静 600ms 让端点检测切句，等到本轮 tts start。"""
    await device_burst(device, LOUD, 5)
    await device_burst(device, QUIET, 10)
    while True:
        opcode, payload = await device.recv()
        if opcode == wire.OP_TEXT and payload.get("type") == "tts":
            assert payload["state"] == "start", f"没等到播报开始：{payload}"
            return


async def drain_until_stop(device, limit: int = 400) -> list[tuple[int, object]]:
    seen: list[tuple[int, object]] = []
    for _ in range(limit):
        item = await device.recv()
        seen.append(item)
        if item[0] == wire.OP_TEXT and item[1].get("state") == "stop":
            return seen
    raise AssertionError(f"{limit} 条消息内没等到 tts stop")


def sentence_texts(messages: list[tuple[int, object]]) -> list[str]:
    return [
        payload.get("text")
        for _, payload in messages
        if isinstance(payload, dict) and payload.get("state") == "sentence_start"
    ]


# --- 任务一：唤醒词回声不道歉（静默重置） ---------------------------------------


def test_echo_does_not_speak_and_keeps_connection():
    """e2e：0.84s「回声」（响一下就静）+ ASR 空 → 零下行；连接活着，下轮照常。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await device_burst(device, LOUD, 5)
            await device_burst(device, QUIET, 9)  # 0.84s，< 1.5s 判据
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(device.recv(), 0.3), "回声不许有任何下行"
            # 连接保持：再来一轮真实提问，照常回答
            await kick_off_turn(device)
            return await drain_until_stop(device)
        finally:
            await device.close()

    heard: list[bytes] = []
    transcriptions = ["", "你好小智"]

    def transcribe(wav: bytes) -> str:
        heard.append(wav)
        return transcriptions[len(heard) - 1]

    messages, logs = run_realtime(scenario, transcriber=transcribe)
    assert len(heard) == 2, "回声那轮也要走 ASR（只是结果为空）"
    assert any("忽略（短音频无文字）" in line for line in logs), logs
    assert messages[-1][1]["state"] == "stop", "第二轮要正常收尾"


def test_long_empty_asr_still_apologizes():
    """超过 1.5s 仍识别为空 = 真实说话没听清 → 照旧道歉（现有兜底不许动）。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await device_burst(device, LOUD, 40)  # 2.4s 说话
            await device_burst(device, QUIET, 10)
            return await device.recv_until_stop()
        finally:
            await device.close()

    messages, logs = run_realtime(scenario, transcriber=lambda wav: "")
    assert SPOKEN not in sentence_texts(messages)
    assert any("asr_empty" in line for line in logs), "走的是原来的空识别兜底"
    assert any("忽略（短音频无文字）" not in line for line in logs)


def test_echo_resets_idle_window():
    """e2e：空等超过窗口后回声到达 → 计时重置，窗口从回声重新起算。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await asyncio.sleep(0.6)  # 空等超过窗口（0.5s）；没有帧来就不判定
            # 回声：响 300ms + 静 540ms → 切句 → ASR 空 → 静默忽略 + 计时重置
            await device_burst(device, LOUD, 5)
            await device_burst(device, QUIET, 9)
            await asyncio.sleep(0.05)
            await device_burst(device, QUIET, 2)  # 距重置 ~0.1s < 0.5s，不许关
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(device.recv(), 0.15)
            await asyncio.sleep(0.1)
            await device_burst(device, QUIET, 2)  # ~0.3s < 0.5s，还不许关
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(device.recv(), 0.15)
            await asyncio.sleep(0.3)  # 累计 > 0.5s
            await device_burst(device, QUIET, 2)  # 这一包触发空闲退出
            return await drain_until_stop(device)
        finally:
            await device.close()

    messages, logs = run_realtime(
        scenario,
        transcriber=lambda wav: "",
        server_kwargs={"idle_seconds": 0.5},
    )
    assert SESSION_CLOSE_SPOKEN in sentence_texts(messages), "结束语要播出来"
    assert any(
        line.startswith("[session]") and "回到待命" in line for line in logs
    ), logs


def raise_empty_result(wav: bytes) -> str:
    """真机那轮的形状：回声被送去 ASR 后抛 ASREmptyResultError，而不是返回空串。"""
    raise ASREmptyResultError("ASR 没听出文字：这段音频里没有人说话，请再说一遍。")


def test_short_audio_asr_empty_error_is_silent_and_resets_idle():
    """0.84s + ASR 抛 ASREmptyResultError → 与返回空同判：静默重置、空闲计时重置。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await asyncio.sleep(0.6)  # 空等超过窗口（0.5s）；没有帧来就不判定
            # 回声：响 300ms + 静 540ms → 切句 → ASR 抛空结果 → 静默忽略 + 计时重置
            await device_burst(device, LOUD, 5)
            await device_burst(device, QUIET, 9)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(device.recv(), 0.3), "回声抛异常也不许出声"
            await asyncio.sleep(0.05)
            await device_burst(device, QUIET, 2)  # 距重置 ~0.35s < 0.5s，不许关
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(device.recv(), 0.15)
            await asyncio.sleep(0.3)  # 距重置 > 0.5s
            await device_burst(device, QUIET, 2)  # 这一包触发空闲退出
            return await drain_until_stop(device)
        finally:
            await device.close()

    messages, logs = run_realtime(
        scenario,
        transcriber=raise_empty_result,
        server_kwargs={"idle_seconds": 0.5},
    )
    assert any("忽略（短音频无文字）" in line for line in logs), logs
    assert any("ASREmptyResultError" in line for line in logs), "日志要记下真实异常"
    assert SESSION_CLOSE_SPOKEN in sentence_texts(messages), "空闲计时重置后照常到点收尾"


def test_long_audio_asr_empty_error_still_apologizes():
    """5s 音频 + ASR 抛 ASREmptyResultError = 真实说话没识别出来 → 必须说一句话。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await device_burst(device, LOUD, 80)  # 4.8s 说话
            await device_burst(device, QUIET, 10)
            return await device.recv_until_stop()
        finally:
            await device.close()

    messages, logs = run_realtime(scenario, transcriber=raise_empty_result)
    assert ASR_FALLBACK in sentence_texts(messages), "真实失败不许被静默掉"
    assert any("asr_failed" in line for line in logs), logs


# --- 任务二：会话窗口（空闲退出） -----------------------------------------------


def test_idle_timeout_announces_and_closes():
    """e2e：一轮跑完后连续空闲 → 播「有事再喊我。」→ 服务端主动关连接。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await kick_off_turn(device)
            await drain_until_stop(device)  # 正常一轮
            await asyncio.sleep(0.3)  # 超过 0.2s 窗口
            await device_burst(device, QUIET, 2)  # 静音帧到达 → 空闲判定生效
            messages = await drain_until_stop(device)  # 先收完结束语
            opcode, _payload = await device.recv()
            assert opcode == wire.OP_CLOSE, "结束语播完服务端要主动关连接"
            return messages
        finally:
            await device.close()

    messages, logs = run_realtime(scenario, server_kwargs={"idle_seconds": 0.2})
    assert SESSION_CLOSE_SPOKEN in sentence_texts(messages)
    assert any(
        line.startswith("[session]") and "空闲 0.2s，回到待命" in line for line in logs
    ), logs


def test_session_idle_seconds_env_override_and_startup_log(monkeypatch):
    monkeypatch.setenv("CAIRN_SESSION_IDLE_SECONDS", "12.5")
    assert session_idle_seconds_from_env() == 12.5
    assert session_idle_seconds_from_env({}) == 300.0
    with pytest.raises(ValueError, match="CAIRN_SESSION_IDLE_SECONDS"):
        session_idle_seconds_from_env({"CAIRN_SESSION_IDLE_SECONDS": "abc"})

    logs: list[str] = []

    async def main():
        server = XiaozhiServer(
            host="127.0.0.1",
            port=0,
            ws_url="ws://127.0.0.1/xiaozhi/ws",
            log=logs.append,
        )
        await server.start()
        await server.close()

    asyncio.run(main())
    [line] = [line for line in logs if line.startswith("[session]")]
    assert "12.5s" in line, f"启动日志没打印生效的空闲窗口：{line}"


def test_barge_env_overrides(monkeypatch):
    """CAIRN_BARGE_MS（窗口）/ RATIO / GRACE_MS 三个参数都能覆盖，坏了要报错。"""
    monkeypatch.setenv("CAIRN_BARGE_MS", "450")
    monkeypatch.setenv("CAIRN_BARGE_RATIO", "0.5")
    monkeypatch.setenv("CAIRN_BARGE_GRACE_MS", "800")
    assert barge_settings_from_env() == BargeSettings(window_ms=450, ratio=0.5)
    assert barge_grace_ms_from_env() == 800
    assert barge_settings_from_env({}) == BargeSettings(window_ms=300, ratio=0.4)
    assert barge_grace_ms_from_env({}) == 600
    monkeypatch.setenv("CAIRN_BARGE_MS", "xyz")
    with pytest.raises(ValueError, match="CAIRN_BARGE_MS"):
        barge_settings_from_env()
    monkeypatch.setenv("CAIRN_BARGE_GRACE_MS", "abc")
    with pytest.raises(ValueError, match="CAIRN_BARGE_GRACE_MS"):
        barge_grace_ms_from_env()


# --- 任务三：打断（barge-in）----------------------------------------------------


def test_barge_state_machine_in_conversation():
    """arm 之前的帧不进滑窗；arm 后滑窗滚动、占比按满窗容量算；重置清零。"""
    session = Conversation("s1", codec_factory=LoudCodec)
    session.on_text('{"type":"listen","state":"start","mode":"realtime"}')

    for _ in range(5):
        session.on_audio(LOUD)  # 高能量，但没 arm：不进窗
    assert session.barge_armed is False
    assert session.barge_ratio == 0.0

    session.arm_barge()
    for _ in range(5):
        session.on_audio(LOUD)  # 5 包 = 10 个 30ms 帧，满窗全高能
    assert session.barge_ratio == pytest.approx(1.0)
    assert session.barge_ms == pytest.approx(300.0)
    session.on_audio(QUIET)  # 2 个低能帧进窗、2 个高能帧被挤出
    assert session.barge_ratio == pytest.approx(0.8)

    session.arm_barge()  # 重复 arm = 清窗重计
    assert session.barge_ratio == 0.0

    for _ in range(5):
        session.on_audio(LOUD)
    session.on_text('{"type":"listen","state":"start"}')  # 开新窗重置一切
    assert session.barge_armed is False
    assert session.barge_ratio == 0.0


def test_barge_window_counts_interval_frames():
    """间隔式高能帧（模拟音节：响-静-响-静）也进滑窗。

    4 帧高能 / 满窗 300ms = 40% → 达到判据；只有 2 帧高能 = 20% → 不够。
    （连续帧判据在这里永远凑不齐——这正是它被换掉的原因。）
    """
    session = Conversation(
        "s1",
        codec_factory=lambda *_: LoudCodec(16000, 1, 30),  # 一包 = 一个 30ms 帧
        vad=VadSettings(frame_ms=30),
    )
    session.on_text('{"type":"listen","state":"start","mode":"realtime"}')
    session.arm_barge()

    for _ in range(4):
        session.on_audio(LOUD)
        session.on_audio(QUIET)
    assert session.barge_ratio == pytest.approx(0.4)
    assert session.barge_ms == pytest.approx(120.0)

    session.arm_barge()  # 清窗重计
    session.on_audio(LOUD)
    session.on_audio(QUIET)
    session.on_audio(LOUD)
    session.on_audio(QUIET)
    assert session.barge_ratio == pytest.approx(0.2), "只有 2 帧高能，不够 40%"


def test_barge_peak_rms_accumulates_until_taken():
    """诊断峰值：逐帧取 max，take 走一次就归零重计。"""
    session = Conversation("s1", codec_factory=LoudCodec)
    session.on_text('{"type":"listen","state":"start","mode":"realtime"}')
    burst(session, QUIET, 2)
    assert session.take_barge_peak_rms() == 0.0
    burst(session, LOUD, 1)
    assert session.take_barge_peak_rms() > 0.0
    burst(session, QUIET, 1)
    assert session.take_barge_peak_rms() == 0.0


def test_barge_in_cancels_broadcast_and_keeps_audio():
    """e2e：播报第 700ms 注入 420ms 高能量 → 取消播报 + tts stop + 丢帧 >0，
    且触发打断的音频作为新一句的开头进入下一轮断句。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await kick_off_turn(device)  # 1.0s 长播报开始
            await asyncio.sleep(0.7)  # 过 600ms 宽限期
            await device_burst(device, LOUD, 7)  # 420ms 连续高能量 ≥ 300ms 判据
            await device_burst(device, QUIET, 10)  # 之后安静 600ms → 切出这一句
            interrupted = await drain_until_stop(device)
            _opcode, payload = await device.recv()
            assert payload.get("state") == "start", f"下一轮没开始：{payload}"
            return interrupted
        finally:
            await device.close()

    heard: list[bytes] = []

    def transcribe(wav: bytes) -> str:
        heard.append(wav)
        return "你好小智"

    messages, logs = run_realtime(
        scenario,
        transcriber=transcribe,
        synthesizer=lambda text: tone_wav(1.0),  # 17 帧，给打断留时间
    )

    frames = [payload for opcode, payload in messages if opcode == wire.OP_BINARY]
    assert len(frames) < 17, f"打断后必须停止推帧：发了 {len(frames)}/17"
    assert messages[-1][1]["state"] == "stop", "打断要下发 tts stop 收尾"
    [barge_line] = [line for line in logs if line.startswith("[barge] 说话开始后")]
    match = re.search(r"丢弃 (\d+) 帧", barge_line)
    assert match and int(match.group(1)) > 0, f"必须有丢弃帧数：{barge_line}"

    assert len(heard) == 2, f"打断的音频要进入下一轮 ASR，实际 {len(heard)} 次"
    with wave.open(io.BytesIO(heard[1])) as handle:
        assert handle.getframerate() == 16000
        # 开头可能有切句残留的 1~2 包静音，读 0.3s：里面必须含打断时的高能量音频
        samples = handle.readframes(4800)
    assert any(sample != 0 for sample in samples), "缓冲不许清空：打断音频必须保留"


def test_loud_uplink_within_grace_does_not_barge():
    """e2e：播报开始 300ms 内的高能量一律忽略 → 播报完整跑完，不另起回合。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await kick_off_turn(device)
            await device_burst(device, LOUD, 5)  # 宽限期内（<600ms）
            await device_burst(device, QUIET, 15)  # 之后安静，不构成打断
            messages = await drain_until_stop(device)
            await device.send_json({"type": "hello"})  # 连接保持
            opcode, payload = await device.recv()
            assert opcode == wire.OP_TEXT and payload["type"] == "hello"
            return messages
        finally:
            await device.close()

    heard: list[bytes] = []

    def transcribe(wav: bytes) -> str:
        heard.append(wav)
        return "你好小智"

    messages, logs = run_realtime(
        scenario,
        transcriber=transcriber_hook(heard),
        synthesizer=lambda text: tone_wav(1.0),  # 17 帧长播报
    )

    frames = [payload for opcode, payload in messages if opcode == wire.OP_BINARY]
    assert len(frames) == 17, f"宽限期内的能量不许打断，播报要完整：{len(frames)}"
    assert not [line for line in logs if line.startswith("[barge] 说话开始后")], logs
    assert len(heard) == 1, f"播报期间的语音不许另起回合（防自问自答）：{len(heard)}"


def test_interval_syllables_after_grace_interrupt_broadcast():
    """e2e（AC1）：宽限期后响/静交替（模拟音节），300ms 滑窗高能占比达 40% → 打断。

    连续帧判据下这个模式永远凑不齐 300ms——这条测试就是它被换掉的证据。
    """

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await kick_off_turn(device)  # 1.0s 长播报开始
            await asyncio.sleep(0.7)  # 过 600ms 宽限期
            await device_burst(device, LOUD, 1)  # 每包 = 2 个 30ms 帧
            await device_burst(device, QUIET, 1)
            await device_burst(device, LOUD, 1)  # 此时窗内 4 高 2 低 = 40% → 打断
            await device_burst(device, QUIET, 1)
            await device_burst(device, LOUD, 1)
            await device_burst(device, QUIET, 10)  # 之后安静 600ms → 切出这一句
            interrupted = await drain_until_stop(device)
            _opcode, payload = await device.recv()
            assert payload.get("state") == "start", f"下一轮没开始：{payload}"
            return interrupted
        finally:
            await device.close()

    heard: list[bytes] = []

    def transcribe(wav: bytes) -> str:
        heard.append(wav)
        return "你好小智"

    messages, logs = run_realtime(
        scenario,
        transcriber=transcribe,
        synthesizer=lambda text: tone_wav(1.0),  # 17 帧，给打断留时间
    )

    frames = [payload for opcode, payload in messages if opcode == wire.OP_BINARY]
    assert len(frames) < 17, f"间隔式说话必须打断，播报不许推完：发了 {len(frames)}/17"
    assert any(line.startswith("[barge] 说话开始后") for line in logs), logs
    assert len(heard) == 2, f"打断的音频要进入下一轮 ASR，实际 {len(heard)} 次"


def test_two_loud_frames_after_grace_do_not_barge():
    """e2e（AC2）：宽限期后只有 1 包（2 个 30ms 帧）高能 = 20% → 不够，播报完整。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await kick_off_turn(device)
            await asyncio.sleep(0.7)  # 过宽限期
            await device_burst(device, LOUD, 1)  # 2 帧高能 = 20% < 40%
            await device_burst(device, QUIET, 15)
            messages = await drain_until_stop(device)
            await device.send_json({"type": "hello"})  # 连接保持
            opcode, payload = await device.recv()
            assert opcode == wire.OP_TEXT and payload["type"] == "hello"
            return messages
        finally:
            await device.close()

    heard: list[bytes] = []
    messages, logs = run_realtime(
        scenario,
        transcriber=transcriber_hook(heard),
        synthesizer=lambda text: tone_wav(1.0),
    )

    frames = [payload for opcode, payload in messages if opcode == wire.OP_BINARY]
    assert len(frames) == 17, f"占比不够不许打断，播报要完整：{len(frames)}"
    assert not [line for line in logs if line.startswith("[barge] 说话开始后")], logs
    assert len(heard) == 1, "播报期间的切句不许另起回合"


def transcriber_hook(heard):
    def transcribe(wav: bytes) -> str:
        heard.append(wav)
        return "你好小智"

    return transcribe


def test_barge_grace_constant_is_six_hundred_ms():
    """宽限期常量钉在 600ms：无 AEC 设备上避开自家喇叭回声的实测妥协。"""
    assert BARGE_GRACE_MS == 600


def test_barge_diag_printed_every_second_during_broadcast():
    """常驻诊断：播报期间每秒一条 `[barge] 峰值rms=… 高能占比=…%`，供真机调参。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {"type": "listen", "state": "start", "mode": "realtime"}
            )
            await kick_off_turn(device)  # 长播报开始
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2.0
            while loop.time() < deadline:  # 持续推流，触发读循环里的诊断判定
                await device_burst(device, QUIET, 2)
                await asyncio.sleep(0.1)
            return None
        finally:
            await device.close()

    _messages, logs = run_realtime(
        scenario,
        synthesizer=lambda text: tone_wav(2.2),  # 37 帧 ≈ 2s 推送，跨过两个诊断点
    )
    diag = [line for line in logs if line.startswith("[barge] 峰值rms=")]
    assert diag, f"播报期间必须有 [barge] 诊断日志：{logs[-5:]}"
    for line in diag:
        assert re.search(r"峰值rms=\d+\.\d{4} 高能占比=\d+%", line), line
