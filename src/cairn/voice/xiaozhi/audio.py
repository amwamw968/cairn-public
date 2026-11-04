"""Opus ⇄ PCM：把固件推上来的二进制帧解成 16k 单声道 PCM，把要念的话编回同样的帧。

一层的边界：

    Codec.decode(packet) -> pcm bytes      60 ms 一包，坏包返回 b""（不抛）
    Codec.encode(pcm)    -> opus bytes     长度必须是 frame_bytes
    Codec.frames(pcm)    -> 逐帧编码       尾帧用静音补齐
    wav_to_pcm(data)     -> pcm bytes      只收 16-bit 单声道、采样率必须与 Codec 一致

**为什么是 opuslib**：它是 libopus 的薄 ctypes 封装，纯 Python、无编译、不拖 ffmpeg
（``av`` 自带 40 MB 二进制）。代价是要系统里有 libopus：macOS ``brew install opus``，
Debian ``apt install libopus0``。没有就 ``AudioUnavailable``，消息里带安装命令，不静默降级。

**一个已知的平台坑**：opuslib 3.0.1 的 ``bitrate`` 属性在 macOS arm64 上设/读都返回
``OPUS_BAD_ARG``，而同一个 ``opus_encoder_ctl`` 请求用 C 编译调用是正常的——这是
ctypes 变参适配的问题，不是 libopus 的问题。所以这里**不设码率**，用 libopus 默认值
（16k 单声道实测约 21 kbps，60 ms 一包约 160 字节）。想改码率得先修 opuslib。

**导入期不加载 libopus**：``import opuslib`` 放在 ``_opuslib()`` 里。这样没有 libopus
的机器仍能 import 本模块、跑协议层测试，只有在真要编解码时才报错。
"""

from __future__ import annotations

import array
import ctypes.util
import math
import os
import sys
import wave
from collections.abc import Iterator
from io import BytesIO

from cairn.voice.mic import pcm_to_wav as _mic_pcm_to_wav

SAMPLE_RATE = 16000
# 板子喇叭是 24 kHz（真机实测 2026-10-31：hello 回复声明 16k 时串口报
# "Server sample rate 16000 does not match device output sample rate 24000,
# resampling may cause distortion"）。下行按它编，上行（麦克风）仍是 16k。
OUTPUT_SAMPLE_RATE = 24000
CHANNELS = 1
FRAME_MS = 60

# macOS 上 Homebrew 装的库只在这两个前缀里（arm64 / Intel）。uv 或 python.org 直装的
# Python 不像 Homebrew 的 python 那样自带 DYLD 回退路径，``find_library("opus")``
# 会找不到明明装好的 libopus（本仓 .venv-voice 实测），所以要在进程内补上。
DYLD_PREFIXES = ("/opt/homebrew/lib", "/usr/local/lib")

# libopus 原生支持的采样率；别的值 libopus 自己会拒。
SUPPORTED_RATES = (8000, 12000, 16000, 24000, 48000)
# Opus 一包最长 120 ms，解码缓冲按它给上限，见 _Codec.decode。
MAX_FRAME_MS = 120

INSTALL_HINT = "macOS 装 libopus：brew install opus；Debian/Ubuntu：apt install libopus0"


class AudioError(RuntimeError):
    """音频参数不对 / 数据不是预期的 WAV。消息是给人看的中文。"""


class AudioUnavailable(AudioError):
    """这台机器上没有 opuslib 或 libopus。"""


def _ensure_dyld_fallback() -> None:
    """macOS：让 ``find_library`` 能看见 Homebrew 的 libopus。

    ``ctypes.util.find_library`` 在 darwin 上按 DYLD 回退路径搜库。Homebrew 的
    python 自带 ``/opt/homebrew/lib``；uv / python.org 直装的不带，于是 opuslib
    在 import 时就报「Could not find Opus library」——尽管 ``brew install opus``
    装得好好的。这里在**进程内**补环境变量（比要求启动命令带一串 env 可靠），
    已能找到时什么都不动。
    """
    if sys.platform != "darwin":
        return
    if ctypes.util.find_library("opus") is not None:
        return
    existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    found = [
        prefix
        for prefix in DYLD_PREFIXES
        if os.path.exists(os.path.join(prefix, "libopus.dylib"))
    ]
    if not found:
        return  # 没有 Homebrew 的 libopus：保持原样，让 _opuslib 给标准的安装提示
    merged: list[str] = []
    for path in (*found, *existing.split(os.pathsep)):
        if path and path not in merged:
            merged.append(path)
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(merged)


def _opuslib():
    """延迟加载 opuslib；缺库时给一句能照做的中文提示。"""
    _ensure_dyld_fallback()
    try:
        import opuslib  # noqa: PLC0415 - 故意延迟，见模块 docstring
    except ImportError as exc:  # 没装 python 包
        raise AudioUnavailable(
            f"没有 opuslib（{exc}）。装法：pip install -e \".[voice]\"；{INSTALL_HINT}。"
        ) from None
    except OSError as exc:  # 装了包但系统里没有 libopus
        raise AudioUnavailable(f"opuslib 加载不了 libopus（{exc}）。{INSTALL_HINT}。") from None
    return opuslib


def available() -> tuple[bool, str]:
    """``(能不能编解码, 不能的原因)``。给启动预检用，不抛。"""
    try:
        codec = Codec()
    except AudioError as exc:
        return False, str(exc)
    codec.close()
    return True, ""


def frame_samples(sample_rate: int = SAMPLE_RATE, frame_ms: int = FRAME_MS) -> int:
    """一帧的样本数（每声道）。16k×60ms = 960。"""
    if sample_rate <= 0 or frame_ms <= 0:
        raise AudioError(f"帧参数不合法：sample_rate={sample_rate} frame_ms={frame_ms}")
    samples = sample_rate * frame_ms // 1000
    if samples * 1000 != sample_rate * frame_ms:
        raise AudioError(
            f"{sample_rate} Hz 下 {frame_ms} ms 不是整数个样本；换一个帧长。"
        )
    return samples


def wav_to_pcm(data: bytes, *, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> bytes:
    """WAV → 裸 PCM。**采样率/声道/位宽对不上就拒绝**，不把变速音频推给板子。"""
    if not data:
        raise AudioError("WAV 是空的。")
    try:
        with wave.open(BytesIO(data)) as handle:
            rate = handle.getframerate()
            width = handle.getsampwidth()
            chans = handle.getnchannels()
            if width != 2:
                raise AudioError(f"只收 16-bit PCM，这段是 {width * 8}-bit。")
            if chans != channels:
                raise AudioError(f"只收 {channels} 声道，这段是 {chans} 声道。")
            if rate != sample_rate:
                raise AudioError(
                    f"采样率 {rate} Hz 与设备 {sample_rate} Hz 不一致，"
                    "推下去会变速；请让 TTS 直接输出 16 kHz。"
                )
            return handle.readframes(handle.getnframes())
    except wave.Error as exc:
        raise AudioError(f"不是合法的 WAV：{exc}") from None


def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE, *, channels: int = CHANNELS) -> bytes:
    """裸 PCM → 带头的 WAV（ASR 要文件，不要裸流）。"""
    return _mic_pcm_to_wav(pcm, sample_rate, channels=channels)


def frame_rms(pcm: bytes) -> float:
    """一帧 16-bit 单声道 PCM 的归一化 RMS（0~1）；空帧 / 半样本返回 0。

    端点检测用它对照真机实测的量级：数字静音 ~0.0005，最轻人声 ~0.0053，
    所以阈值 0.0025 落在两者中间（阈值归 ``ws.VadSettings`` 管，这里只算数）。
    """
    trimmed = pcm[: len(pcm) - len(pcm) % 2]
    if not trimmed:
        return 0.0
    samples = array.array("h")
    samples.frombytes(trimmed)
    acc = 0
    for value in samples:
        acc += value * value
    return math.sqrt(acc / len(samples)) / 32768.0


def resample(pcm: bytes, *, from_rate: int, to_rate: int, channels: int = CHANNELS) -> bytes:
    """裸 PCM 线性插值重采样（只支持单声道，率相同原样返回）。

    TTS 源固定 16 kHz（``say --data-format=LEI16@16000``），而板子喇叭要 24 kHz；
    与其把 16k 数据按 24k 播（变速 = 变调，正是串口警告的来源），不如在这里重采样。
    语音场景线性插值足够；不做多相滤波那种重炮。
    """
    if from_rate == to_rate:
        return pcm
    if from_rate <= 0 or to_rate <= 0:
        raise AudioError(f"采样率不合法：{from_rate} Hz -> {to_rate} Hz")
    if channels != 1:
        raise AudioError(f"重采样只支持单声道，收到 {channels} 声道。")
    samples = array.array("h")
    samples.frombytes(pcm)
    total = len(samples)
    if total == 0:
        return b""
    out = array.array("h", bytes(2 * round(total * to_rate / from_rate)))
    step = from_rate / to_rate
    for index in range(len(out)):
        pos = index * step
        left = int(pos)
        frac = pos - left
        a = samples[left]
        b = samples[left + 1] if left + 1 < total else a
        out[index] = int(a + (b - a) * frac)
    return out.tobytes()


class Codec:
    """一台设备的一条音频通道：解码器 + 编码器 + 帧长。

    *sample_rate* / *channels* / *frame_ms* 应当来自固件 hello 的 ``audio_params``；
    对不上时调用方（``ws.Conversation``）负责回落并记日志，这里只按参数干活。
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_ms: int = FRAME_MS,
    ):
        if sample_rate not in SUPPORTED_RATES:
            raise AudioError(
                f"Opus 不支持 {sample_rate} Hz，只支持 {SUPPORTED_RATES}。"
            )
        if channels not in (1, 2):
            raise AudioError(f"Opus 只支持 1 或 2 声道，收到 {channels}。")
        if not 0 < frame_ms <= MAX_FRAME_MS:
            raise AudioError(f"帧长必须在 1~{MAX_FRAME_MS} ms 之间，收到 {frame_ms}。")

        opuslib = _opuslib()
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.frame_samples = frame_samples(sample_rate, frame_ms)
        self.frame_bytes = self.frame_samples * channels * 2
        # 解码输出上限按 120 ms 给：frame_size 只是缓冲长度，实际返回以 libopus 为准。
        self._max_samples = frame_samples(sample_rate, MAX_FRAME_MS)
        self._opuslib = opuslib
        self._decoder = opuslib.Decoder(sample_rate, channels)
        # APPLICATION_VOIP 针对语音调过，比 AUDIO 更适合念书。
        self._encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_VOIP)

    def decode(self, packet: bytes) -> bytes:
        """一包 Opus → PCM。坏包返回 ``b""``：一条坏帧不该打断整轮。"""
        if not packet:
            return b""
        try:
            return self._decoder.decode(packet, self._max_samples)
        except self._opuslib.OpusError:
            return b""

    def encode(self, pcm: bytes) -> bytes:
        """恰好一帧 PCM → 一包 Opus。长度不对是调用方的 bug，直接报错。"""
        if len(pcm) != self.frame_bytes:
            raise AudioError(
                f"一帧要 {self.frame_bytes} 字节 PCM，收到 {len(pcm)} 字节。"
            )
        return self._encoder.encode(pcm, self.frame_samples)

    def frames(self, pcm: bytes) -> Iterator[bytes]:
        """整段 PCM → 逐个 Opus 包；尾帧补静音，不让最后一两个字被吃掉。"""
        for offset in range(0, len(pcm), self.frame_bytes):
            chunk = pcm[offset : offset + self.frame_bytes]
            if len(chunk) < self.frame_bytes:
                chunk = chunk + b"\x00" * (self.frame_bytes - len(chunk))
            yield self.encode(chunk)

    def close(self) -> None:
        """opuslib 的状态由 GC 回收；这里只把引用放掉，方便启动预检后立刻释放。"""
        self._decoder = None
        self._encoder = None
