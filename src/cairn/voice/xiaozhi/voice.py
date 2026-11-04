"""一轮语音：ASR → ``run_turn`` → TTS → 分帧推给板子，顺手把每段耗时记下来。

分工：

    ws.Conversation      协议状态 + 收帧解码（不知道 ASR/TTS 存在）
    voice.VoicePipeline  一段 WAV → 文字 → 回合 → Opus 帧（不知道 WS 存在）
    server               把两者接起来 + 发送锁 + 打断取消

**``spoken`` 才能进 TTS，``next_step`` 只进日志。** ``next_step`` 里是给模型的约束
（「必须调用 submit_answer」这类），念出来对孩子就是噪音。这里对 ``next_step``
只有一个出口：``log()``；下面所有下发文本都由 ``turn.spoken`` 构造，测试逐字断言。

**失败要说话**：ASR 挂了 / 听不出字 / 音频太短 → 「我没听清，再说一遍好吗」；
回合抛了 → 「我这会儿没处理好」。两句话都走和正常回复完全相同的推送路径，
所以「流程走完」这件事在失败时也成立——板子不会卡在 listening。

**节奏**：参考实现每帧之间 ``sleep(frame_ms * 0.9)``（``esp32-voice-entry/src/session.py:1610``）。
板子的播放缓冲只有一两秒，把三秒音频在几十毫秒内灌进去会丢帧。

**本轮不做流式**：ASR 与 TTS 都是一次性的，首帧延迟会到秒级；日志里的
``asr_ms / tts_ms / first_frame_ms`` 就是为了量这个，下一步换流式后再比。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cairn.voice import asr as asr_module
from cairn.voice import tts as tts_module
from cairn.voice import turn as turn_module
from cairn.voice.turn import run_turn
from cairn.voice.xiaozhi import audio
from cairn.voice.xiaozhi import wire
from cairn.voice.xiaozhi import ws

# 失败也要说出口的两句话。措辞短、能听清、不埋怨孩子。
ASR_FALLBACK = "我没听清，再说一遍好吗？"
TURN_FALLBACK = "我这会儿没处理好。"
# 短于这个时长不送 ASR：60ms 的咳嗽不值得一次 1.5 秒的识别。
MIN_UTTERANCE_SECONDS = 0.2
# 唤醒词回声/环境噪声的判据：短于这个时长且 ASR 没听出字（返回空或抛
# ASREmptyResultError）→ 静默忽略不道歉
# （真机实测：孩子刚喊完「你好小智」，回声被切成 0.54s 的一句话，ASR 为空）。
ECHO_MAX_SECONDS = 1.5
# 会话空闲退出时的结束语（server 走现有 TTS 链路播完再关连接）。
SESSION_CLOSE_SPOKEN = "有事再喊我。"
# 字幕条放不下长句；与参考实现一致截 60 字。
SENTENCE_LIMIT = 60
# 帧间隔 = 帧长 × 0.9（留 10% 余量给网络抖动，参考实现同款）。
PACE_FACTOR = 0.9

# 兜底提示音：TTS 合成失败时让板子响两声短哔，孩子至少知道这一轮有回应，
# 而不是对着空气说话。纯 Python 生成 PCM，不依赖 TTS、不联网、不读外部文件。
TONE_HZ = 880
TONE_BEEP_SECONDS = 0.2
TONE_GAP_SECONDS = 0.1
# 下发的字幕：兜底音频是哔声不是话，字幕要如实，不能假装念了道歉。
FALLBACK_TONE_TEXT = "（出错了）"

_fallback_pcm_cache: bytes | None = None


def fallback_tone_pcm() -> bytes:
    """两声短哔的 16 kHz 单声道 PCM（约 0.6 秒）；模块级缓存，只生成一次。

    采样率固定 16k 单声道：``ws.align_params`` 保证下行参数只会是它，
    所以这里不做参数适配，直接交给现有编码/分帧逻辑。
    """
    global _fallback_pcm_cache
    if _fallback_pcm_cache is None:
        rate = audio.SAMPLE_RATE
        beep = int(rate * TONE_BEEP_SECONDS)
        gap = int(rate * TONE_GAP_SECONDS)
        samples: list[int] = []
        for _ in range(2):
            for i in range(beep):
                samples.append(int(6000 * math.sin(2 * math.pi * TONE_HZ * i / rate)))
            samples.extend([0] * gap)
        _fallback_pcm_cache = struct.pack(f"<{len(samples)}h", *samples)
    return _fallback_pcm_cache

Sender = Callable[[object], Awaitable[None]]


@dataclass(frozen=True)
class RoundStats:
    """一轮的耗时，单位毫秒；``first_frame_ms`` 从 listen stop 算起。"""

    number: int
    asr_ms: int
    turn_ms: int
    tts_ms: int
    first_frame_ms: int | None
    total_ms: int
    frames: int
    audio_ms: int
    kind: str = ""


class VoicePipeline:
    """把「一段 WAV」跑成「一串推送」。所有外部依赖都可注入，测试不碰真 ASR/TTS/库。"""

    def __init__(
        self,
        *,
        session_factory=None,
        learner_id: int | None = None,
        transcriber: Callable[[bytes], str] = asr_module.transcribe,
        responder: Callable[..., object] = run_turn,
        synthesizer: Callable[[str], bytes] = tts_module.synthesize,
        codec_factory: Callable[..., object] = audio.Codec,
        log: Callable[[str], None] = print,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._session_factory = session_factory
        self._learner_id = learner_id
        self._transcribe = transcriber
        self._respond = responder
        self._synthesize = synthesizer
        self._codec_factory = codec_factory
        self._log = log
        self._clock = clock
        self._round = 0
        self._codecs: dict[tuple[int, int, int], object] = {}
        # 最近一次推送被打断时丢弃的帧数（None = 没有被打断的推送）。
        # server 打断后读它写 `[barge]` 日志；每次开始推送都会先置回 None。
        self.dropped_frames: int | None = None

    # --- 对外 --------------------------------------------------------------

    async def handle(
        self, utterance, params: dict, sender: Sender, *, session_id: str = ""
    ) -> RoundStats:
        """跑完一轮并逐条推送。``sender`` 收 dict（控制消息）或 bytes（Opus 帧）。"""
        t0 = self._clock()
        self._round += 1
        number = self._round
        self._log(
            f"[turn] #{number} 收到音频 {utterance.seconds:.2f}s / {utterance.packets} 帧"
            f"（坏包 {utterance.decode_errors}）"
        )

        if utterance.seconds < MIN_UTTERANCE_SECONDS:
            return await self._speak(
                ASR_FALLBACK,
                number=number,
                t0=t0,
                sender=sender,
                params=params,
                session_id=session_id,
                kind="too_short",
                note=f"音频只有 {utterance.seconds:.2f}s，短于 {MIN_UTTERANCE_SECONDS}s",
            )

        try:
            text = await asyncio.to_thread(self._transcribe, utterance.wav)
        except asr_module.ASREmptyResultError as exc:
            # 空结果异常与「返回空文本」同判（真机：回声那轮 ASR 抛的是
            # ASREmptyResultError，走异常分支漏过了回声抑制）：短音频是唤醒词
            # 回声 → 静默重置不出声；长音频是真实说话没听出字 → 照旧道歉。
            asr_ms = self._elapsed(t0)
            if utterance.seconds < ECHO_MAX_SECONDS:
                self._log(
                    f"[turn] #{number} 忽略（短音频无文字）{type(exc).__name__}: {exc}"
                )
                return self._finish(number, t0, "ignored_echo", asr_ms=asr_ms)
            return await self._speak(
                ASR_FALLBACK,
                number=number,
                t0=t0,
                sender=sender,
                params=params,
                session_id=session_id,
                kind="asr_failed",
                note=f"ASR 失败：{type(exc).__name__}: {exc}",
            )
        except Exception as exc:  # ASR 挂了不许把连接带走，说一句话就够
            return await self._speak(
                ASR_FALLBACK,
                number=number,
                t0=t0,
                sender=sender,
                params=params,
                session_id=session_id,
                kind="asr_failed",
                note=f"ASR 失败：{type(exc).__name__}: {exc}",
            )
        asr_ms = self._elapsed(t0)

        text = (text or "").strip()
        if not text:
            if utterance.seconds < ECHO_MAX_SECONDS:
                # 唤醒词回声/环境噪声：不发任何声音。缓冲在 take_utterance 已清，
                # 空闲计时由 server 在切句时重置——孩子刚喊完唤醒词，不该听到道歉。
                self._log(f"[turn] #{number} 忽略（短音频无文字）")
                return self._finish(number, t0, "ignored_echo", asr_ms=asr_ms)
            return await self._speak(
                ASR_FALLBACK,
                number=number,
                t0=t0,
                sender=sender,
                params=params,
                session_id=session_id,
                kind="asr_empty",
                note="ASR 返回空文本",
                asr_ms=asr_ms,
            )
        self._log(f"[asr] #{number} {text!r}（{asr_ms} ms）")
        turn_start = self._clock()

        try:
            turn = await asyncio.to_thread(self._run_turn, text)
        except Exception as exc:  # 工具/库出错：说「没处理好」，不念栈
            return await self._speak(
                TURN_FALLBACK,
                number=number,
                t0=t0,
                sender=sender,
                params=params,
                session_id=session_id,
                kind="turn_failed",
                note=f"回合失败：{type(exc).__name__}: {exc}",
                asr_ms=asr_ms,
            )
        turn_ms = self._elapsed(turn_start)

        spoken = (turn.spoken or "").strip()
        if turn.next_step:
            # 约束只给模型看。这里是它在语音链路上的唯一出口。
            self._log(f"[turn] #{number} next_step={turn.next_step}")
        if turn.kind == turn_module.STOP_KIND:
            # 短指令「停」：不出新话，只补一条 tts stop 收尾（与打断的
            # tell_device 分支同一路径），把板子从 speaking 拉回 idle。
            self._log(f"[turn] #{number} 短指令停止播报，不出新话")
            with contextlib.suppress(ConnectionError, OSError, wire.WireError):
                await sender(ws.tts_stop(session_id))
            return self._finish(
                number, t0, "command_stop", asr_ms=asr_ms, turn_ms=turn_ms, tts_ms=0
            )
        if not spoken:
            return await self._speak(
                TURN_FALLBACK,
                number=number,
                t0=t0,
                sender=sender,
                params=params,
                session_id=session_id,
                kind="empty_spoken",
                note=f"回合 kind={turn.kind} 没有可念的话",
                asr_ms=asr_ms,
                turn_ms=turn_ms,
            )
        self._log(f"[turn] #{number} kind={turn.kind} spoken={spoken!r}（{turn_ms} ms）")

        return await self._speak(
            spoken,
            number=number,
            t0=t0,
            sender=sender,
            params=params,
            session_id=session_id,
            kind=str(turn.kind),
            asr_ms=asr_ms,
            turn_ms=turn_ms,
        )

    # --- 合成 + 推送 -------------------------------------------------------

    async def announce(
        self, text: str, params: dict, sender: Sender, *, session_id: str = ""
    ) -> RoundStats:
        """不走 ASR/回合、直接说一句话（会话空闲退出的结束语）。

        推送链路与正常播报完全相同（tts start → 字幕 → 帧 → stop），
        所以「流程走完」在这里同样成立。
        """
        self._round += 1
        return await self._speak(
            text,
            number=self._round,
            t0=self._clock(),
            sender=sender,
            params=params,
            session_id=session_id,
            kind="session_close",
        )

    async def _speak(
        self,
        text: str,
        *,
        number: int,
        t0: float,
        sender: Sender,
        params: dict,
        session_id: str,
        kind: str,
        note: str = "",
        asr_ms: int = 0,
        turn_ms: int = 0,
    ) -> RoundStats:
        """一句话 → WAV → Opus 帧 → tts start / sentence / 帧 / stop。"""
        if note:
            self._log(f"[turn] #{number} {kind}：{note}")

        tts_start = self._clock()
        try:
            wav = await asyncio.to_thread(self._synthesize, text)
        except Exception as exc:
            # 合成不出来也要「说」点什么：两声短哔走和正常播报完全相同的下行
            # 序列（tts start → 帧 → stop），绝不许静默结束让孩子对着空气说话。
            self._log(
                f"[tts_fallback] #{number} 合成失败，发兜底提示音：{type(exc).__name__}: {exc}"
            )
            return await self._speak_fallback(
                number=number,
                t0=t0,
                sender=sender,
                params=params,
                session_id=session_id,
                kind=kind,
                asr_ms=asr_ms,
                turn_ms=turn_ms,
                tts_start=tts_start,
            )
        tts_ms = self._elapsed(tts_start)

        try:
            # TTS 源固定 16 kHz（say --data-format=LEI16@16000）；下行按 output_params
            # 的采样率（板子喇叭 24 kHz）编码，率不同先重采样，别让板子变速播放。
            pcm = audio.wav_to_pcm(
                wav,
                sample_rate=audio.SAMPLE_RATE,
                channels=int(params["channels"]),
            )
            out_rate = int(params["sample_rate"])
            if out_rate != audio.SAMPLE_RATE:
                pcm = audio.resample(
                    pcm, from_rate=audio.SAMPLE_RATE, to_rate=out_rate
                )
            frames = list(self._encoder(params).frames(pcm))
        except audio.AudioError as exc:
            self._log(f"[tts] #{number} 音频不可用：{exc}")
            return self._finish(number, t0, kind, asr_ms, turn_ms, tts_ms)

        audio_ms = int(
            len(pcm) / 2 / int(params["channels"]) / int(params["sample_rate"]) * 1000
        )

        first_frame_ms = await self._push_audio(
            frames, text, params, sender, session_id, t0
        )
        return self._finish(
            number, t0, kind, asr_ms, turn_ms, tts_ms, first_frame_ms, len(frames), audio_ms
        )

    async def _speak_fallback(
        self,
        *,
        number: int,
        t0: float,
        sender: Sender,
        params: dict,
        session_id: str,
        kind: str,
        asr_ms: int = 0,
        turn_ms: int = 0,
        tts_start: float,
    ) -> RoundStats:
        """合成失败后的兜底：兜底提示音按正常 TTS 同款节奏推下去。

        编码兜底音频本身也挂了（极端情况，比如缺 libopus）就只发
        ``tts start`` + ``tts stop``：板子至少能收到完整的序列收尾，
        不许抛异常打断连接，也不许一个字节都不发。
        """
        pcm = fallback_tone_pcm()  # 固定 16 kHz 源，与 TTS 同样先重采样再编码
        tts_ms = self._elapsed(tts_start)
        frames: list[bytes] = []
        try:
            out_rate = int(params["sample_rate"])
            if out_rate != audio.SAMPLE_RATE:
                pcm = audio.resample(pcm, from_rate=audio.SAMPLE_RATE, to_rate=out_rate)
            frames = list(self._encoder(params).frames(pcm))
        except Exception as exc:
            self._log(
                f"[tts_fallback_failed] #{number} 兜底音频编码失败，只发 tts start/stop："
                f"{type(exc).__name__}: {exc}"
            )

        audio_ms = int(
            len(pcm) / 2 / int(params["channels"]) / int(params["sample_rate"]) * 1000
        )
        first_frame_ms = await self._push_audio(
            frames, FALLBACK_TONE_TEXT, params, sender, session_id, t0
        )
        return self._finish(
            number, t0, kind, asr_ms, turn_ms, tts_ms, first_frame_ms, len(frames), audio_ms
        )

    async def _push_audio(
        self,
        frames: list[bytes],
        text: str,
        params: dict,
        sender: Sender,
        session_id: str,
        t0: float,
    ) -> int | None:
        """tts start → 字幕 → 按节奏逐帧推 → tts stop；返回首帧时刻（毫秒）。

        被打断（任务被 cancel）时：剩余未推的帧就地丢弃（不许补读念了一半的
        句子），帧数记进 ``dropped_frames`` 供 server 写 `[barge]` 日志，然后
        原样上抛 CancelledError。
        """
        self.dropped_frames = None
        await sender(ws.tts_start(session_id))
        await sender(ws.tts_sentence(text[:SENTENCE_LIMIT], session_id))
        first_frame_ms: int | None = None
        pace = int(params["frame_duration"]) * PACE_FACTOR / 1000
        total = len(frames)
        sent = 0
        try:
            for index, frame in enumerate(frames):
                await sender(frame)
                sent += 1
                if first_frame_ms is None:
                    first_frame_ms = self._elapsed(t0)
                if index < total - 1:
                    await asyncio.sleep(pace)
        except asyncio.CancelledError:
            self.dropped_frames = total - sent
            raise
        await sender(ws.tts_stop(session_id))
        return first_frame_ms

    def _finish(
        self,
        number: int,
        t0: float,
        kind: str,
        asr_ms: int,
        turn_ms: int,
        tts_ms: int,
        first_frame_ms: int | None = None,
        frames: int = 0,
        audio_ms: int = 0,
    ) -> RoundStats:
        """每轮都以这一行收尾：没有它就算「跑完了」也没法判断慢在哪一段。"""
        stats = RoundStats(
            number=number,
            asr_ms=asr_ms,
            turn_ms=turn_ms,
            tts_ms=tts_ms,
            first_frame_ms=first_frame_ms,
            total_ms=self._elapsed(t0),
            frames=frames,
            audio_ms=audio_ms,
            kind=kind,
        )
        self._log(
            f"[turn] #{number} 完成 kind={kind} asr_ms={stats.asr_ms} turn_ms={stats.turn_ms} "
            f"tts_ms={stats.tts_ms} first_frame_ms={stats.first_frame_ms} "
            f"frames={stats.frames} audio_ms={stats.audio_ms} total_ms={stats.total_ms}"
        )
        return stats

    # --- 内部 --------------------------------------------------------------

    def _run_turn(self, text: str):
        if self._session_factory is None:
            raise RuntimeError("语音链路没有配数据库（session_factory 为空）")
        with self._session_factory() as session:
            if self._respond is run_turn:
                # 默认回合函数才认 log 形参（短指令模糊命中日志从这里出来）；
                # 注入的替身签名不定，不硬塞。
                return self._respond(session, self._learner_id, text, log=self._log)
            return self._respond(session, self._learner_id, text)

    def _encoder(self, params: dict):
        key = (
            int(params["sample_rate"]),
            int(params["channels"]),
            int(params["frame_duration"]),
        )
        codec = self._codecs.get(key)
        if codec is None:
            codec = self._codec_factory(*key)
            self._codecs[key] = codec
        return codec

    def _elapsed(self, start: float) -> int:
        return int((self._clock() - start) * 1000)
