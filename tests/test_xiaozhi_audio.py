"""M3b：音频进出打通——用假设备 + 假编解码 + 假 ASR/TTS 跑完整轮，不碰真板子。

三组：
- ``Codec`` / WAV 互转的单元测试（真有 libopus 才跑，缺库 skip 并打印原因）；
- ``tts.synthesize`` 的命令拼装与失败分级（假 runner 写文件，不真出声）；
- 一条真 WebSocket 连接上的完整往返回路（假设备客户端 + 假流水线）。

最后一组是本卡的验收核心：``listen start`` → 二进制帧 → ``listen stop`` →
``tts start / sentence_start / 帧 / stop``，以及 ``next_step`` 绝不出现在下行文本里。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes.util
import io
import json
import math
import os
import struct
import sys
import wave

import pytest

from cairn.voice import tts as tts_module
from cairn.voice.xiaozhi import audio, wire
from cairn.voice.xiaozhi.server import WS_PATH, XiaozhiServer
from cairn.voice.xiaozhi.voice import (
    ASR_FALLBACK,
    MIN_UTTERANCE_SECONDS,
    TURN_FALLBACK,
    VoicePipeline,
    fallback_tone_pcm,
)
from cairn.voice.xiaozhi import voice
from cairn.voice.xiaozhi.ws import DOWNLINK_PARAMS, Conversation

TIMEOUT = 3.0
OPUS_READY, OPUS_REASON = audio.available()
needs_opus = pytest.mark.skipif(not OPUS_READY, reason=f"没有 libopus：{OPUS_REASON}")

SPOKEN = "今天我们练数学，先来一道加法。"
NEXT_STEP = "必须调用 submit_answer 判分，不许自己说对错。"


# --- 脚手架：假编解码 ---------------------------------------------------------


class FakeCodec:
    """接口与 ``audio.Codec`` 一致，但不碰 libopus。

    解码时每个包吐一帧「静音」（非零样本，便于用时长得出的确有数据）；
    编码时把包长写进包头，测试据此数帧。
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1, frame_ms: int = 60):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.frame_samples = sample_rate * frame_ms // 1000
        self.frame_bytes = self.frame_samples * channels * 2
        self.decoded = 0
        self.encoded = 0

    def decode(self, packet: bytes) -> bytes:
        if not packet:
            return b""
        self.decoded += 1
        return b"\x01\x00" * self.frame_samples

    def encode(self, pcm: bytes) -> bytes:
        assert len(pcm) == self.frame_bytes, (len(pcm), self.frame_bytes)
        self.encoded += 1
        return b"OPUS" + len(pcm).to_bytes(2, "big")

    def frames(self, pcm: bytes):
        for offset in range(0, len(pcm), self.frame_bytes):
            chunk = pcm[offset : offset + self.frame_bytes]
            if len(chunk) < self.frame_bytes:
                chunk = chunk + b"\x00" * (self.frame_bytes - len(chunk))
            yield self.encode(chunk)


def wav_bytes(seconds: float, *, rate: int = 16000, channels: int = 1) -> bytes:
    """一段正弦波 WAV，给假 TTS 用。"""
    frames = int(rate * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        samples = b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(frames)
        )
        handle.writeframes(samples)
    return buffer.getvalue()


# --- 脚手架：假设备 -----------------------------------------------------------


def masked_frame(opcode: int, payload: bytes = b"") -> bytes:
    mask = os.urandom(4)
    body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    head = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        head.append(0x80 | length)
    elif length < 0x10000:
        head.append(0x80 | 126)
        head += length.to_bytes(2, "big")
    else:
        head.append(0x80 | 127)
        head += length.to_bytes(8, "big")
    return bytes(head) + mask + body


async def read_server_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first, second = await reader.readexactly(2)
    assert not second & 0x80, "服务端帧不应掩码"
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    return opcode, await reader.readexactly(length)


class FakeDevice:
    """假设备：握手 + 收发 JSON / 二进制，行为贴近小智固件。"""

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
            "Client-Id: fake-device\r\n"
            "\r\n"
        )
        writer.write(request.encode("latin-1"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        assert b"101" in head, f"握手失败：{head!r}"
        return cls(reader, writer)

    async def send_json(self, payload: dict) -> None:
        await self.send_frame(wire.OP_TEXT, json.dumps(payload).encode("utf-8"))

    async def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        self._writer.write(masked_frame(opcode, payload))
        await self._writer.drain()

    async def recv(self) -> tuple[int, object]:
        opcode, payload = await asyncio.wait_for(read_server_frame(self._reader), TIMEOUT)
        if opcode == wire.OP_TEXT:
            return opcode, json.loads(payload)
        return opcode, payload

    async def send_utterance(self, packets: int = 5) -> None:
        """一个常听的窗口：start → N 个假包 → stop。"""
        await self.send_json({"type": "listen", "state": "start", "mode": "manual"})
        for _ in range(packets):
            await self.send_frame(wire.OP_BINARY, b"\xf8\xff\xfe" * 20)
        await self.send_json({"type": "listen", "state": "stop"})

    async def recv_until_stop(self, limit: int = 400) -> list[tuple[int, object]]:
        """读到 ``tts stop`` 为止（含）；超条数直接失败，避免挂死。"""
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

    async def silent_for(self, seconds: float = 0.3) -> None:
        """断言这段时间内服务端一个字节都没发。"""
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(self.recv(), seconds)

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except ConnectionError:
            pass


def run_voice(scenario, **pipeline_kwargs):
    """起一台带假流水线的服务；返回 ``(scenario 的返回值, 日志行)``。"""

    async def main():
        logs: list[str] = []
        pipeline = VoicePipeline(log=logs.append, codec_factory=FakeCodec, **pipeline_kwargs)
        server = XiaozhiServer(
            host="127.0.0.1",
            port=0,
            ws_url="ws://127.0.0.1/xiaozhi/ws",
            log=logs.append,
            pipeline=pipeline,
            codec_factory=FakeCodec,
        )
        await server.start()
        try:
            result = await scenario(server)
        finally:
            await server.close()
        return result, logs

    return asyncio.run(main())


def null_factory():
    """没有数据库也能跑：``with null_factory() as session`` 给一个 None。"""
    return contextlib.nullcontext(None)


class FakeTurn:
    def __init__(
        self,
        spoken: str = SPOKEN,
        next_step: str | None = NEXT_STEP,
        kind: str = "question",
    ):
        self.spoken = spoken
        self.next_step = next_step
        self.kind = kind


def control(messages: list[tuple[int, object]], state: str) -> list[dict]:
    """挑出指定 state 的文本消息。"""
    return [
        payload
        for opcode, payload in messages
        if opcode == wire.OP_TEXT and isinstance(payload, dict) and payload.get("state") == state
    ]


def frames(messages: list[tuple[int, object]]) -> list[bytes]:
    return [payload for opcode, payload in messages if opcode == wire.OP_BINARY]


def sentence_text(messages: list[tuple[int, object]]) -> str:
    sentences = control(messages, "sentence_start")
    assert sentences, "没有 tts sentence_start"
    return sentences[0]["text"]


def text_messages(messages: list[tuple[int, object]]) -> list[dict]:
    return [payload for opcode, payload in messages if opcode == wire.OP_TEXT]


# --- Codec / WAV 互转 ---------------------------------------------------------


def test_frames_pads_tail_and_counts():
    codec = FakeCodec()
    pcm = b"\x00\x01" * (codec.frame_samples + 100)  # 1.1 帧
    packets = list(codec.frames(pcm))
    assert len(packets) == 2, "尾帧必须补一帧，不能把最后 100 个样本丢掉"
    assert codec.encoded == 2


def test_wav_roundtrip_rejects_wrong_rate():
    data = wav_bytes(0.1, rate=22050)
    with pytest.raises(audio.AudioError) as excinfo:
        audio.wav_to_pcm(data, sample_rate=16000)
    assert "22050" in str(excinfo.value)


def test_wav_to_pcm_reads_samples():
    pcm = audio.wav_to_pcm(wav_bytes(0.1))
    assert len(pcm) == 1600 * 2


@needs_opus
def test_opus_codec_roundtrip_16k_60ms():
    codec = audio.Codec(16000, 1, 60)
    assert codec.frame_samples == 960
    assert codec.frame_bytes == 1920

    pcm = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
        for i in range(codec.frame_samples * 3)
    )
    packets = list(codec.frames(pcm))
    assert len(packets) == 3
    assert all(0 < len(packet) < codec.frame_bytes for packet in packets), "Opus 必须比 PCM 小"

    decoded = codec.decode(packets[0])
    assert len(decoded) == codec.frame_bytes
    assert any(decoded), "解出来不该是全零"


@needs_opus
def test_opus_codec_rejects_bad_params():
    with pytest.raises(audio.AudioError):
        audio.Codec(44100, 1, 60)
    with pytest.raises(audio.AudioError):
        audio.Codec(16000, 3, 60)


def test_frame_samples_rejects_fractional():
    # 44100 Hz 下 7 ms 是 308.7 个样本，取整会让每帧慢慢漂移，必须拒绝。
    with pytest.raises(audio.AudioError):
        audio.frame_samples(44100, 7)


# --- tts.synthesize -----------------------------------------------------------


class FakeSay:
    """假 runner：按 ``-o`` 的参数写一段真 WAV，或按设定失败。"""

    def __init__(self, *, returncode: int = 0, write: bool = True):
        self.returncode = returncode
        self.write = write
        self.argv: list[str] = []

    def __call__(self, argv, **kwargs):
        self.argv = list(argv)
        target = argv[argv.index("-o") + 1]
        if self.write and self.returncode == 0:
            with open(target, "wb") as handle:
                handle.write(wav_bytes(0.2))

        class Result:
            pass

        result = Result()
        result.returncode = self.returncode
        result.stderr = b"boom" if self.returncode else b""
        return result


def test_synthesize_asks_for_16k_wav_and_returns_bytes():
    runner = FakeSay()
    data = tts_module.synthesize("你好", voice="Tingting", say_path="/usr/bin/say", runner=runner)

    assert runner.argv[:3] == ["/usr/bin/say", "-v", "Tingting"]
    assert runner.argv[runner.argv.index("--data-format") + 1] == "LEI16@16000"
    assert runner.argv[-2:] == ["--", "你好"], "句子必须放在 -- 之后，不能被当成参数"
    assert len(audio.wav_to_pcm(data)) > 0


def test_synthesize_raises_on_failure():
    with pytest.raises(tts_module.TTSError) as excinfo:
        tts_module.synthesize("你好", say_path="/usr/bin/say", runner=FakeSay(returncode=1))
    assert "退出码 1" in str(excinfo.value)


def test_synthesize_raises_when_say_missing(monkeypatch):
    # 找不到 say：给的是「没有 say 命令」，不是「退出码」。
    monkeypatch.setattr(tts_module.shutil, "which", lambda name: None)
    with pytest.raises(tts_module.TTSError) as missing:
        tts_module.synthesize("你好", say_path=None, runner=FakeSay())
    assert "say" in str(missing.value)


def test_synthesize_rejects_empty_text():
    with pytest.raises(tts_module.TTSError):
        tts_module.synthesize("   ")


# --- 会话状态机 ---------------------------------------------------------------


def test_hello_alignment_keeps_device_frame_duration():
    """下行声明 24 kHz（板子喇叭），帧长可以跟设备走；上行解码仍按设备声明的 16k。"""
    session = Conversation("s1", codec_factory=FakeCodec)
    [reply] = session.on_text(
        json.dumps(
            {
                "type": "hello",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 20,
                },
            }
        )
    )
    # 真机实测（2026-10-31）：板子喇叭 24k，hello 回复必须声明 24000，否则下行变调。
    assert reply["audio_params"]["sample_rate"] == 24000
    assert reply["audio_params"]["frame_duration"] == 20
    assert session.params_reason == ""
    assert session.params["sample_rate"] == 16000, "上行解码仍按设备声明的 16k"


def test_hello_alignment_falls_back_when_rate_differs():
    session = Conversation("s1", codec_factory=FakeCodec)
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
    assert reply["audio_params"] == DOWNLINK_PARAMS
    assert "48000" in session.params_reason


def test_hello_alignment_falls_back_with_reason():
    session = Conversation("s1", codec_factory=FakeCodec)
    [reply] = session.on_text(
        json.dumps(
            {
                "type": "hello",
                "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1},
            }
        )
    )
    assert reply["audio_params"] == DOWNLINK_PARAMS
    assert "pcm" in session.params_reason
    assert "_fallback_reason" not in reply["audio_params"], "回落原因不能下发"


def test_frames_outside_window_are_counted_not_decoded():
    session = Conversation("s1", codec_factory=FakeCodec)
    session.on_audio(b"BEFORE")
    assert session.stray_packets == 1
    assert session.pending_seconds == 0.0

    session.on_text('{"type":"listen","state":"start"}')
    session.on_audio(b"INSIDE")
    assert session.pending_seconds == pytest.approx(0.06, rel=0.01)

    utterance = session.take_utterance()
    assert utterance is not None
    assert utterance.packets == 1
    assert audio.wav_to_pcm(utterance.wav)
    assert session.take_utterance() is None, "同一段音频不能被送去两次"


def test_abort_clears_buffer_and_flags():
    session = Conversation("s1", codec_factory=FakeCodec)
    session.on_text('{"type":"listen","state":"start"}')
    session.on_audio(b"x")
    assert session.on_text('{"type":"abort"}') == []
    assert session.take_abort() is True
    assert session.take_abort() is False
    assert session.take_utterance() is None


def test_mid_window_text_does_not_flush_utterance():
    """窗口里插进来的未知文本（mcp / iot / 坏 JSON）不能把半截音频交出去。"""
    session = Conversation("s1", codec_factory=FakeCodec)
    session.on_text('{"type":"listen","state":"start"}')
    session.on_audio(b"INSIDE")
    assert session.on_text('{"type":"mcp","payload":{}}') == []
    assert session.take_stop() is False
    assert session.pending_seconds == pytest.approx(0.06, rel=0.01), "音频还得在缓冲里"

    session.on_text('{"type":"listen","state":"stop"}')
    assert session.take_stop() is True
    assert session.take_stop() is False, "stop 事件只能被消费一次"
    utterance = session.take_utterance()
    assert utterance is not None
    assert utterance.packets == 1


# --- 整条 WS 回路 -------------------------------------------------------------


def test_turn_pushes_tts_sequence():
    heard: list[bytes] = []

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_utterance(5)
            return await device.recv_until_stop()
        finally:
            await device.close()

    def transcribe(wav: bytes) -> str:
        heard.append(wav)
        return "来一道数学题"

    messages, logs = run_voice(
        scenario,
        session_factory=null_factory,
        transcriber=transcribe,
        responder=lambda session, learner, text: FakeTurn(),
        synthesizer=lambda text: wav_bytes(0.3),
    )

    assert heard and heard[0].startswith(b"RIFF"), "送去 ASR 必须是 WAV"
    assert [item[1].get("state") for item in messages[:2]] == ["start", "sentence_start"]
    assert messages[-1][1]["state"] == "stop"
    assert len(frames(messages)) == 5, "0.3 秒音频 / 60 ms 一帧 = 5 帧"
    assert sentence_text(messages) == SPOKEN
    assert NEXT_STEP not in json.dumps(text_messages(messages), ensure_ascii=False)
    assert any("next_step=" in line for line in logs), "next_step 必须留在日志里"


def test_asr_failure_speaks_apology():
    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_utterance(5)
            return await device.recv_until_stop()
        finally:
            await device.close()

    def boom(wav):
        raise RuntimeError("ASR 服务没起来")

    messages, _logs = run_voice(
        scenario,
        session_factory=null_factory,
        transcriber=boom,
        responder=lambda *a: pytest.fail("ASR 挂了不该进回合"),
        synthesizer=lambda text: wav_bytes(0.2),
    )

    assert sentence_text(messages) == ASR_FALLBACK
    assert messages[-1][1]["state"] == "stop"


def test_turn_failure_speaks_apology():
    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_utterance(5)
            return await device.recv_until_stop()
        finally:
            await device.close()

    def boom(session, learner, text):
        raise RuntimeError("工具抛了")

    messages, _logs = run_voice(
        scenario,
        session_factory=null_factory,
        transcriber=lambda wav: "来一道题",
        responder=boom,
        synthesizer=lambda text: wav_bytes(0.2),
    )

    assert sentence_text(messages) == TURN_FALLBACK
    assert len(frames(messages)) == 4


def test_short_audio_skips_asr_and_speaks_apology():
    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_utterance(1)  # 60 ms
            return await device.recv_until_stop()
        finally:
            await device.close()

    messages, logs = run_voice(
        scenario,
        session_factory=null_factory,
        transcriber=lambda wav: pytest.fail("太短不该送 ASR"),
        responder=lambda *a: pytest.fail("太短不该进回合"),
        synthesizer=lambda text: wav_bytes(0.2),
    )

    assert MIN_UTTERANCE_SECONDS > 0.06
    assert sentence_text(messages) == ASR_FALLBACK
    assert any("too_short" in line for line in logs)


def test_abort_stops_pushing_and_sends_stop():
    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_utterance(5)
            head = []
            while len(head) < 3:  # tts start / sentence / 第一帧
                head.append(await device.recv())
            await device.send_json({"type": "abort", "reason": "wake_word_detected"})
            tail = []
            while True:
                item = await device.recv()
                tail.append(item)
                if isinstance(item[1], dict) and item[1].get("state") == "stop":
                    break
            return head + tail
        finally:
            await device.close()

    messages, logs = run_voice(
        scenario,
        session_factory=null_factory,
        transcriber=lambda wav: "来一道题",
        responder=lambda *a: FakeTurn(),
        synthesizer=lambda text: wav_bytes(1.2),  # 20 帧，够长，来得及打断
    )

    assert len(frames(messages)) < 20, "abort 之后不许继续推帧"
    assert messages[-1][1]["type"] == "tts" and messages[-1][1]["state"] == "stop"
    assert any("已打断" in line for line in logs)


def test_mid_window_text_does_not_cut_the_utterance():
    """端到端：窗口中途的未知文本不该触发一次提前的回合，音频要完整送到 stop。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json({"type": "listen", "state": "start"})
            for _ in range(2):
                await device.send_frame(wire.OP_BINARY, b"\xf8\xff\xfe" * 20)
            await device.send_json({"type": "llm", "emotion": "happy"})
            for _ in range(3):
                await device.send_frame(wire.OP_BINARY, b"\xf8\xff\xfe" * 20)
            await device.send_json({"type": "listen", "state": "stop"})
            return await device.recv_until_stop()
        finally:
            await device.close()

    seen_wav: list[bytes] = []

    def transcribe(wav: bytes) -> str:
        seen_wav.append(wav)
        return "来一道题"

    messages, _logs = run_voice(
        scenario,
        session_factory=null_factory,
        transcriber=transcribe,
        responder=lambda *a: FakeTurn(),
        synthesizer=lambda text: wav_bytes(0.2),
    )

    assert len(seen_wav) == 1, "中间的未知文本不该触发一次提前的回合"
    with wave.open(io.BytesIO(seen_wav[0])) as handle:
        assert handle.getnframes() == 16000 * 5 * 60 // 1000, "5 包 60ms 都要在"
    assert messages[-1][1]["state"] == "stop"


def test_no_voice_pipeline_keeps_connection_alive():
    """没接数据库/ASR 时（M3a 的启动方式）连接照常活着，只是不说话。"""

    async def main():
        server = XiaozhiServer(
            host="127.0.0.1", port=0, ws_url="ws://127.0.0.1/xiaozhi/ws", log=lambda *_: None
        )
        await server.start()
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_utterance(5)
            await device.silent_for(0.3)
            await device.send_json({"type": "hello"})
            return await device.recv()
        finally:
            await device.close()
            await server.close()

    opcode, payload = asyncio.run(main())
    assert opcode == wire.OP_TEXT and payload["type"] == "hello"


def test_timing_log_has_all_segments():
    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_utterance(5)
            return await device.recv_until_stop()
        finally:
            await device.close()

    _messages, logs = run_voice(
        scenario,
        session_factory=null_factory,
        transcriber=lambda wav: "来一道题",
        responder=lambda *a: FakeTurn(),
        synthesizer=lambda text: wav_bytes(0.2),
    )

    summary = [line for line in logs if "first_frame_ms=" in line]
    assert summary, f"没有每轮耗时汇总行；日志：{logs}"
    line = summary[0]
    for key in ("asr_ms=", "turn_ms=", "tts_ms=", "first_frame_ms=", "frames=", "total_ms="):
        assert key in line


def test_tts_failure_sends_fallback_tone_sequence():
    """合成挂了不许静默：至少 tts start + tts stop，中间要有音频帧（兜底提示音）。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_utterance(5)
            return await device.recv_until_stop()
        finally:
            await device.close()

    def boom(text):
        raise tts_module.TTSError("say 不在")

    messages, logs = run_voice(
        scenario,
        session_factory=null_factory,
        transcriber=lambda wav: "来一道题",
        responder=lambda *a: FakeTurn(),
        synthesizer=boom,
    )

    assert messages[0][1]["type"] == "tts" and messages[0][1]["state"] == "start"
    assert messages[-1][1]["type"] == "tts" and messages[-1][1]["state"] == "stop"
    assert len(frames(messages)) > 0, "TTS 失败也要有兜底提示音的音频帧，不许静默"
    assert any("tts_fallback" in line for line in logs), f"要有兜底日志；日志：{logs}"
    summary = [line for line in logs if "first_frame_ms=" in line]
    assert summary, f"失败也要有汇总行；日志：{logs}"


@needs_opus
def test_fallback_tone_decodes_to_nonempty_pcm():
    """兜底音频必须是真声音：编得过 Opus、解出来非空，且时长在 0.5~0.8 秒。"""
    pcm = voice.fallback_tone_pcm()
    assert fallback_tone_pcm() is pcm, "兜底音频要模块级缓存，不许每次失败重新算"
    seconds = len(pcm) / 2 / 16000
    assert 0.5 <= seconds <= 0.8, f"兜底提示音 {seconds:.2f}s 超出两声短哔的范围"

    codec = audio.Codec(16000, 1, 60)
    packets = list(codec.frames(pcm))
    assert len(packets) >= 4
    decoded = b"".join(codec.decode(packet) for packet in packets)
    assert len(pcm) <= len(decoded) <= len(pcm) + codec.frame_bytes, "解出来要覆盖整段提示音"
    assert any(decoded), "兜底音频不许是全零的空帧糊弄"


@needs_opus
def test_opus_frames_pads_tail_with_silence():
    """1.5 帧的 PCM 也要出 2 个包：最后一帧补静音，别把半句话吃掉。"""
    codec = audio.Codec(16000, 1, 60)
    pcm = b"\x00\x01" * (codec.frame_samples + 480)
    packets = list(codec.frames(pcm))
    assert len(packets) == 2
    assert len(codec.decode(packets[1])) == codec.frame_bytes


@pytest.mark.skipif(sys.platform != "darwin", reason="只关心 macOS 的 Homebrew 布局")
@needs_opus
def test_dyld_fallback_makes_libopus_discoverable(monkeypatch):
    """uv 直装的 python 不带 Homebrew 的 DYLD 回退路径；删掉环境变量后必须仍能找到。

    本仓实况：跑测试的 ``.venv``（Homebrew python）本来就能找到；真板子用的
    ``.venv-voice``（uv python 3.13）不带——这条用例保证两种解释器殊途同归。
    """
    monkeypatch.delenv("DYLD_FALLBACK_LIBRARY_PATH", raising=False)
    audio._ensure_dyld_fallback()
    assert ctypes.util.find_library("opus") is not None
