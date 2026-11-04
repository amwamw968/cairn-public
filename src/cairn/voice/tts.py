"""文字转语音：alpha 阶段直接用 macOS 的 ``say``。

不追求音色，只求「这一轮的话真能出声」。三个设计 + 一个 M3b 新增的出口：

1. ``speak`` 本机出声（M2 的 CLI 用）。**不抛异常**：无论 ``say`` 在不在、
   退出码多少、有没有超时——回合已经跑完了，不能因为播报失败把结果也吞掉。
   缺失就降级成打印：话进 stdout、原因进 stderr。
2. ``synthesize`` 出一段 16 kHz / 单声道 / 16-bit 的 WAV bytes（M3b 的小智板子用：
   音频要被 Opus 编码推给设备，本机不能出声）。失败**抛** ``TTSError``——
   推不出去必须让调用方知道，否则板子那边就是一片安静。
3. **声音可换**。``voice`` 参数 > ``CAIRN_TTS_VOICE`` 环境变量 > 系统默认音色，
   两条路共用，想换童声/语速只改环境变量，不动代码。

``synthesize`` 走 ``say --data-format=LEI16@16000 -o <临时文件>``：采样率写死 16 kHz，
与板子的上行参数一致。``say`` 若忽略该格式，下游 ``wav_to_pcm`` 会按采样率拒绝，
不会把变速音频推给孩子。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from cairn.voice import speech_cache
from cairn.voice.speech_cache import SpeechAudioCache, canonicalize, is_valid_audio

logger = logging.getLogger(__name__)

ENV_VOICE = "CAIRN_TTS_VOICE"
SAY_BINARY = "say"
# ``say`` 是同步的：一句十来字的话不到 3 秒，60 秒是防它被系统挂死的兜底。
SAY_TIMEOUT_SECONDS = 60.0
FALLBACK_HINT = "[播报降级] "
# 小智板子按 16k 单声道收音频；合成结果必须就是这个格式。
SYNTH_RATE = 16000
SYNTH_FORMAT = f"LEI16@{SYNTH_RATE}"


class TTSError(RuntimeError):
    """合成失败（没有 say / 非零退出 / 超时 / 没产出文件）。消息是中文。"""



def _snippet(raw: object) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    elif raw is None:
        return ""
    else:
        text = str(raw)
    return text.strip()[:120]


def _fallback(text: str, reason: str) -> None:
    """播不出去时的统一口径：话进 stdout，原因进 stderr。"""
    print(f"{FALLBACK_HINT}{reason}这句只打印：{text}", file=sys.stderr)
    print(text)


def speak(
    text: str,
    *,
    voice: str | None = None,
    say_path: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = SAY_TIMEOUT_SECONDS,
) -> None:
    """念一句。永远是「尽力」，失败只降级，不打断回合。

    *runner* 与 *say_path* 是给测试的接缝：假 runner 记录 argv 就能验证命令拼得对，
    不必真出声。
    """
    spoken = (text or "").strip()
    if not spoken:
        return

    name = voice if voice is not None else (os.environ.get(ENV_VOICE) or None)
    binary = say_path or shutil.which(SAY_BINARY)
    if not binary:
        _fallback(spoken, "这台机器上没有 say 命令，")
        return

    argv: list[str] = [binary]
    if name:
        argv += ["-v", name]
    # ``--`` 之后一律当文本：句子万一以「-」开头也不会被当成参数。
    argv += ["--", spoken]

    try:
        result = runner(argv, check=False, timeout=timeout, capture_output=True)
    except subprocess.TimeoutExpired:
        _fallback(spoken, f"say 超过 {timeout:g} 秒没播完，")
        return
    except (OSError, subprocess.SubprocessError) as exc:
        _fallback(spoken, f"调用 say 失败（{exc}），")
        return

    if getattr(result, "returncode", 0) != 0:
        detail = _snippet(getattr(result, "stderr", b"")) or "未知原因"
        _fallback(spoken, f"say 退出码 {result.returncode}（{detail}），")


#: ``cache`` 参数的哨兵：不传 = 用进程默认缓存；显式传 ``None`` = 关缓存（旧行为）。
_UNSET: Any = object()


def _resolve_voice(voice: str | None) -> str:
    """显式 voice > 环境变量 > 空串（用系统默认音色）。"""
    if voice is not None:
        return voice
    return os.environ.get(ENV_VOICE) or ""


def _say_to_bytes(
    spoken: str,
    name: str,
    binary: str,
    rate: int | None,
    runner: Callable[..., Any],
    timeout: float,
) -> bytes:
    """跑 ``say`` 产出 WAV bytes；任何失败抛 ``TTSError``。"""
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    path = Path(handle.name)
    handle.close()

    argv: list[str] = [binary]
    if name:
        argv += ["-v", name]
    if rate is not None:
        argv += ["-r", str(rate)]
    argv += ["--data-format", SYNTH_FORMAT, "-o", str(path)]
    # ``--`` 之后一律当文本：句子万一以「-」开头也不会被当成参数。
    argv += ["--", spoken]

    try:
        try:
            result = runner(argv, check=False, timeout=timeout, capture_output=True)
        except subprocess.TimeoutExpired:
            raise TTSError(f"say 超过 {timeout:g} 秒没合成完。") from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise TTSError(f"调用 say 失败（{exc}）。") from None

        if getattr(result, "returncode", 0) != 0:
            detail = _snippet(getattr(result, "stderr", b"")) or "未知原因"
            raise TTSError(f"say 退出码 {result.returncode}（{detail}）。")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise TTSError(f"say 没有产出音频文件（{exc.strerror or exc}）。") from None
    finally:
        path.unlink(missing_ok=True)

    if not data:
        raise TTSError("say 产出的音频是空的。")
    return data


def synthesize(
    text: str,
    *,
    voice: str | None = None,
    say_path: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = SAY_TIMEOUT_SECONDS,
    rate: int | None = None,
    cache: SpeechAudioCache | None | Any = _UNSET,
) -> bytes:
    """一句 → 16 kHz 单声道 16-bit 的 WAV bytes（板子要的格式，本机不出声）。

    和 ``speak`` 的取舍相反：**失败抛 ``TTSError``**。播报失败还能只打日志，
    推给设备的音频失败必须让调用方换一句/记一笔，否则孩子那边只有安静。

    缓存（BUKA LESSONS §9）：文本先 ``canonicalize`` 再算 key → 单飞槽内查
    磁盘缓存 → 命中直接返回；未命中则合成 → 完整性校验 → 落盘。缓存文件
    校验不过会被隔离成 ``.corrupt`` 并视为未命中，自然走重合成（同一次调用
    内重试一次）；重合成仍失败照旧抛 ``TTSError``，由调用方走兜底提示音。
    ``cache=None`` 显式关缓存（旧行为）；默认用进程级默认缓存
    （``CAIRN_TTS_CACHE_DIR`` 等环境变量控制，见 ``speech_cache``）。

    *runner* / *say_path* 是测试接缝：假 runner 往 ``-o`` 指定的路径写一段 WAV 即可。
    """
    audio, _ = synthesize_ex(
        text,
        voice=voice,
        say_path=say_path,
        runner=runner,
        timeout=timeout,
        rate=rate,
        cache=cache,
    )
    return audio


def synthesize_ex(
    text: str,
    *,
    voice: str | None = None,
    say_path: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = SAY_TIMEOUT_SECONDS,
    rate: int | None = None,
    cache: SpeechAudioCache | None | Any = _UNSET,
) -> tuple[bytes, bool]:
    """同 ``synthesize``，但多返回一个「是否缓存命中」（HTTP 服务标 X-Cache 用）。"""
    if cache is _UNSET:
        cache = speech_cache.default_cache()

    if cache is None:
        # 旧行为：不缓存、不单飞。保留给显式关闭与既有测试。
        spoken = (text or "").strip()
        if not spoken:
            raise TTSError("没有要合成的话（空字符串）。")
        binary = say_path or shutil.which(SAY_BINARY)
        if not binary:
            raise TTSError(
                "这台机器上没有 say 命令，合成不了语音；"
                "非 macOS 上请换一个 TTS 后端（M3b 只支持 say）。"
            )
        return (
            _say_to_bytes(spoken, _resolve_voice(voice), binary, None, runner, timeout),
            False,
        )

    canonical = canonicalize(text or "")
    if canonical is None:
        raise TTSError("没有要合成的话（空文本或含控制字符/私用区字符）。")

    name = _resolve_voice(voice)
    key = speech_cache.cache_key(canonical, name, rate if rate is not None else "")

    # 单飞：同一 key 并发只合成一次（threading.Lock——synthesize 是同步函数，
    # 在小智链路里经 asyncio.to_thread 跑在线程池，asyncio.Lock 不可用）。
    with cache.coalescer.slot(key):
        hit = cache.get(key)
        if hit is not None:
            return hit, True

        binary = say_path or shutil.which(SAY_BINARY)
        if not binary:
            raise TTSError(
                "这台机器上没有 say 命令，合成不了语音；"
                "非 macOS 上请换一个 TTS 后端（M3b 只支持 say）。"
            )
        data = _say_to_bytes(canonical, name, binary, rate, runner, timeout)

        if not is_valid_audio(data):
            logger.warning("[tts] 合成结果校验不过 key=%s 已丢弃", key)
            raise TTSError("合成结果完整性校验不过（长度或音频头异常）。")
        cache.put(key, data)
        return data, False


__all__: Sequence[str] = ("speak", "synthesize", "synthesize_ex", "TTSError")
