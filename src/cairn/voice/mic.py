"""麦克风采集 + 能量断句：录一句人话，吐一个 16-bit PCM WAV。

**alpha 版不追求好听，只求能切句**：没有降噪、没有 VAD 模型，只用能量（RMS）
判断「开始说话」和「说完了」。参数是从上一版真机标定里抄的思路，值先取保守值，
真机嫌迟钝就改常量（或者调用时传参）。

分两层，为的是**测试不碰真麦克风**：

    segment(blocks, ...)  纯函数：吃 int16 数据块，吐 ``Utterance``（含 WAV bytes）
    listen(...)           只有它开流；流的工厂可注入（``stream_factory``）

采样格式用 ``sd.RawInputStream(dtype="int16")`` 是故意的：venv 里没有 numpy，
``InputStream`` 的 float 数组用不了；int16 原始块拿到的就是 WAV 数据段要的字节，
少一次转换，RMS 也能用整数算。

设备不可用 / 没有麦克风权限：``listen`` 吞掉异常，返回 ``reason="device_error"``
的 ``Utterance``，消息里告诉人去哪儿开权限——不抛栈、不崩。
"""

from __future__ import annotations

import array
import math
import sys
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from io import BytesIO
from typing import Any

SAMPLE_RATE = 16000
CHANNELS = 1
# 1024 点 = 64ms，断句精度够用，块也不至于太小把 CPU 吃满。
BLOCK_SIZE = 1024

# 响度阈值：RMS 占满量程的比例。0.008 约等于 int16 的 262，安静房间的底噪在
# 0.001~0.004 之间，这个值能压住底噪又不至于听不见正常说话。
RMS_THRESHOLD = 0.008
# 短于这个时长算「没听清」，不让 60ms 的咳嗽触发一次 ASR。
MIN_SPEECH_SECONDS = 0.25
# 语音后面静这么久就认为这句说完了。
TAIL_SILENCE_SECONDS = 0.8
# 单句上限：防止一直说话把内存和 ASR 拖住。
MAX_SECONDS = 15.0
# 一直没人说话就返回「没听到」，CLI 可以马上再听下一轮。
MAX_WAIT_SECONDS = 10.0

OK = "ok"
TOO_SHORT = "too_short"
NO_SPEECH = "no_speech"
DEVICE_ERROR = "device_error"

PERMISSION_HINT = "检查「系统设置 → 隐私与安全性 → 麦克风」是否允许当前终端访问。"


@dataclass(frozen=True)
class Utterance:
    """一次采集的结果。

    ``wav`` 为 ``None`` 表示这次没有可送去识别的音频，原因看 ``reason`` /
    ``detail``（都是给人看的中文）。``truncated`` 表示撞到 ``MAX_SECONDS`` 被截断，
    这时 ``wav`` 仍然可用——说了 15 秒也别把整句扔掉。
    """

    wav: bytes | None
    reason: str
    detail: str = ""
    speech_seconds: float = 0.0
    truncated: bool = False


def pcm_to_wav(
    pcm: bytes, sample_rate: int = SAMPLE_RATE, *, channels: int = CHANNELS
) -> bytes:
    """裸 int16 PCM → 标准 wave 头 + 数据（16-bit、单声道）。"""
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def block_rms(block: bytes) -> float:
    """一个 int16 块的 RMS，归一化到 0~1（满量程 32768）。"""
    samples = array.array("h")
    samples.frombytes(block[: len(block) - (len(block) % 2)])
    if not samples:
        return 0.0
    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples)) / 32768.0


def _too_short_detail(seconds: float) -> str:
    return f"只听到 {seconds:.2f} 秒，太短了，请再说一遍。"


def segment(
    blocks: Iterable[bytes],
    *,
    sample_rate: int = SAMPLE_RATE,
    rms_threshold: float = RMS_THRESHOLD,
    min_speech: float = MIN_SPEECH_SECONDS,
    tail_silence: float = TAIL_SILENCE_SECONDS,
    max_seconds: float = MAX_SECONDS,
    max_wait: float = MAX_WAIT_SECONDS,
) -> Utterance:
    """按能量切一句：等到有人说话 → 等尾部静音 → 交 WAV。

    三种「没有可用音频」都返回 ``wav=None``：等太久没人出声（``no_speech``）、
    说的时间不够长（``too_short``）、以及调用方自己给的空块序列。
    """
    tail_samples = int(tail_silence * sample_rate)
    min_samples = int(min_speech * sample_rate)
    max_samples = int(max_seconds * sample_rate)
    wait_samples = int(max_wait * sample_rate)

    collected: list[bytes] = []
    total = 0
    speech_start: int | None = None
    last_voice_end = 0
    truncated = False

    for block in blocks:
        samples = len(block) // 2
        if samples <= 0:
            continue
        voiced = block_rms(block) >= rms_threshold

        if speech_start is None:
            total += samples
            if voiced:
                speech_start = total - samples
                last_voice_end = total
                collected.append(block)
            elif total >= wait_samples:
                return Utterance(
                    None,
                    NO_SPEECH,
                    f"{max_wait:g} 秒内没听到说话，请再说一遍。",
                )
            continue

        collected.append(block)
        total += samples
        if voiced:
            last_voice_end = total
        if total - last_voice_end >= tail_samples:
            break
        if total - speech_start >= max_samples:
            truncated = True
            break

    if speech_start is None:
        return Utterance(None, NO_SPEECH, f"{max_wait:g} 秒内没听到说话，请再说一遍。")

    pcm = b"".join(collected)
    if truncated:
        pcm = pcm[: max_samples * 2]

    voiced_samples = last_voice_end - speech_start
    if voiced_samples < min_samples:
        return Utterance(
            None,
            TOO_SHORT,
            _too_short_detail(voiced_samples / sample_rate),
            speech_seconds=voiced_samples / sample_rate,
        )

    detail = f"录音 {len(pcm) // 2 / sample_rate:.1f} 秒，已到上限被截断。" if truncated else ""
    return Utterance(
        pcm_to_wav(pcm, sample_rate),
        OK,
        detail,
        speech_seconds=voiced_samples / sample_rate,
        truncated=truncated,
    )


class MicUnavailable(RuntimeError):
    """麦克风打不开（没装 sounddevice / 没权限 / 设备被占用）。"""


def _default_stream_factory(**kwargs: Any) -> Any:
    """真流工厂。sounddevice 只在真要开麦克风时才 import。"""
    try:
        import sounddevice as sd
    except Exception as exc:  # 没装：给装法，别把 ImportError 甩到用户脸上
        raise MicUnavailable(
            '没有安装 sounddevice，装法：pip install -e ".[voice]"。'
        ) from exc
    try:
        return sd.RawInputStream(**kwargs)
    except Exception as exc:
        raise MicUnavailable(f"打不开麦克风（{exc}）。{PERMISSION_HINT}") from exc


def _read_blocks(stream: Any, block_size: int, max_blocks: int) -> Iterable[bytes]:
    """从流里按块取 int16 字节；取够 ``max_blocks`` 就停（等待 + 录音的上限）。"""
    for _ in range(max_blocks):
        data, _overflowed = stream.read(block_size)
        yield bytes(data)


def listen(
    *,
    stream_factory: Callable[..., Any] | None = None,
    device: Any = None,
    sample_rate: int = SAMPLE_RATE,
    block_size: int = BLOCK_SIZE,
    max_seconds: float = MAX_SECONDS,
    max_wait: float = MAX_WAIT_SECONDS,
    **segment_kwargs: Any,
) -> Utterance:
    """开麦克风录一句。任何设备层异常都翻译成 ``device_error``，不抛栈。"""
    factory = stream_factory or _default_stream_factory
    seconds_per_block = block_size / sample_rate
    max_blocks = int(math.ceil((max_wait + max_seconds) / seconds_per_block)) + 2

    try:
        stream = factory(
            samplerate=sample_rate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=block_size,
            device=device,
        )
        with stream:
            return segment(
                _read_blocks(stream, block_size, max_blocks),
                sample_rate=sample_rate,
                max_seconds=max_seconds,
                max_wait=max_wait,
                **segment_kwargs,
            )
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # 设备/权限/驱动：说人话，别崩
        message = str(exc) or exc.__class__.__name__
        print(f"麦克风不可用：{message}", file=sys.stderr)
        return Utterance(None, DEVICE_ERROR, f"麦克风不可用：{message}")
