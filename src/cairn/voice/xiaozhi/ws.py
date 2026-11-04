"""小智 WS 的消息形状与会话状态机：hello / listen / abort，以及一段听音窗口。

消息形状照搬 esp32-voice-entry 的 ``src/protocol.py``（MIT，见 ``NOTICE``），
字段名一个不改——固件是照着这些名字解析的。

M3b 的「收帧 → 出 WAV」加上真机实测（2026-10-31，固件 2.5.3）补的两块：

    hello                → 上行按设备声明的 16k 对齐解码；hello 回复声明下行 24k
    listen start|detect  → 开窗口、清缓冲
    （二进制帧）          → Opus 解码 → PCM 追加 → 端点检测
    listen stop          → 关窗口，把这段 PCM 做成 WAV 交给 ``take_utterance()``
    端点检测切句          → realtime 模式板子**永不发 stop**，服务端自己判断
                           「说话后持续 ≥0.5s 低能量」即这句结束，同样交
                           ``take_utterance()``；连续 20s 无语音就清缓冲重来；
                           单句累计 ≥30s（可配）也强制切，剩余音频进下一句
    abort                → 关窗口、丢缓冲，待推送的音频由 server 取消

**本模块不碰网络、不碰 asyncio**：它只产出「要回什么消息」和「这一段 WAV」，
推送给谁、什么时候推、被打断了怎么取消，都是 ``server.py`` 的事。这样音频格式
和回合逻辑都可以在纯同步的测试里钉住。

**参数对齐（上行/下行分开）**：设备 hello 里的 ``audio_params`` 是它「要发什么」
的声明（麦克风，16 kHz）。能支持就照它的（只支持 opus + 8/12/16/24/48 kHz +
单声道），支持不了就回落 16 kHz / 60 ms，并把原因交给调用方写日志。而 hello
**回复**里的 ``audio_params`` 是我们「要发什么」的声明——板子喇叭是 24 kHz，
必须声明 24k，否则板子自己重采样、孩子听到的声音变调（串口实测警告见上）。
两条路不混：``UPLINK_PARAMS`` 管解码，``DOWNLINK_PARAMS`` 管编码与回复。
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, replace

from cairn.voice.xiaozhi import audio

TRANSPORT = "websocket"

# 上行（设备麦克风 → 我们）：解码与 ASR WAV 都用它；照设备 hello 对齐，回落也是它。
UPLINK_PARAMS = {
    "format": "opus",
    "sample_rate": audio.SAMPLE_RATE,
    "channels": audio.CHANNELS,
    "frame_duration": audio.FRAME_MS,
}
# 下行（我们 → 设备喇叭）：hello 回复声明 24k，TTS 也按它编码（16k 源先重采样）。
DOWNLINK_PARAMS = {
    "format": "opus",
    "sample_rate": audio.OUTPUT_SAMPLE_RATE,
    "channels": audio.CHANNELS,
    "frame_duration": audio.FRAME_MS,
}

# Opus 允许的帧长（RFC 6716 §2.1.4）；设备只可能用这几个。
SUPPORTED_FRAME_MS = (10, 20, 40, 60, 120)

# 这两种都算「一轮开始」：手动按键是 start，唤醒词唤醒是 detect。
OPEN_STATES = ("start", "detect")

# --- 服务端端点检测（realtime 连续推流，板子不发 listen stop） -----------------
#
# 阈值来自对照项目真机实测：最轻人声 RMS 0.0053，数字静音 0.0005，
# 0.0025 落在两者中间。其余参数同样可用环境变量覆盖（见 vad_settings_from_env）。
VAD_THRESHOLD = 0.0025
VAD_FRAME_MS = 30
VAD_TAIL_MS = 500
VAD_IDLE_SECONDS = 20.0
# 单句硬上限：一口气说不换气（凑不齐尾静音）时，累计超过它就强制切句交 ASR，
# 兜住 ``_pcm`` 的无界增长。0 表示不设上限（测试对照用）。
VAD_MAX_UTTERANCE_MS = 30000

VAD_ENV_VARS = {
    "threshold": ("CAIRN_VAD_THRESHOLD", float),
    "frame_ms": ("CAIRN_VAD_FRAME_MS", int),
    "tail_ms": ("CAIRN_VAD_TAIL_MS", int),
    "idle_seconds": ("CAIRN_VAD_IDLE_SECONDS", float),
    "max_utterance_ms": ("CAIRN_VAD_MAX_UTTERANCE_MS", int),
}


@dataclass(frozen=True)
class VadSettings:
    """端点检测参数；默认值来自上面的常量，环境变量可逐项覆盖。"""

    threshold: float = VAD_THRESHOLD
    frame_ms: int = VAD_FRAME_MS
    tail_ms: int = VAD_TAIL_MS
    idle_seconds: float = VAD_IDLE_SECONDS
    max_utterance_ms: int = VAD_MAX_UTTERANCE_MS


def vad_settings_from_env(environ: dict[str, str] | None = None) -> VadSettings:
    """读环境变量生成 ``VadSettings``；配置了不合法的值就报错，不悄悄用默认。"""
    env = os.environ if environ is None else environ
    settings = VadSettings()
    for field, (name, cast) in VAD_ENV_VARS.items():
        raw = env.get(name)
        if raw is None:
            continue
        try:
            value = cast(raw)
        except ValueError:
            raise ValueError(f"{name}={raw!r} 不是合法的 {cast.__name__}。") from None
        settings = replace(settings, **{field: value})
    return settings


# --- 打断判据（barge-in）：滑窗比例 -------------------------------------------
#
# 真机实测（2026-10-31 14:27）：播报期间上行峰值 RMS 0.0086 已过阈值 0.0025，
# 但「连续 300ms 高能量」永远凑不齐——人的说话音节之间能量必然掉下来。
# 判据改成滑窗比例：最近 window_ms 的帧窗口里，超过阈值的高能帧占比 ≥ ratio
# （默认 300ms 窗口、40% ≈ 4 个 30ms 帧）即判定有人在说话。
BARGE_RATIO = 0.4
BARGE_WINDOW_MS = 300

BARGE_ENV_VARS = {
    "ratio": ("CAIRN_BARGE_RATIO", float),
    "window_ms": ("CAIRN_BARGE_MS", int),
}


@dataclass(frozen=True)
class BargeSettings:
    """打断判据参数；默认值来自上面的常量，环境变量可逐项覆盖。"""

    ratio: float = BARGE_RATIO
    window_ms: int = BARGE_WINDOW_MS


def barge_settings_from_env(environ: dict[str, str] | None = None) -> BargeSettings:
    """读环境变量生成 ``BargeSettings``；配置了不合法的值就报错，不悄悄用默认。"""
    env = os.environ if environ is None else environ
    settings = BargeSettings()
    for field, (name, cast) in BARGE_ENV_VARS.items():
        raw = env.get(name)
        if raw is None:
            continue
        try:
            value = cast(raw)
        except ValueError:
            raise ValueError(f"{name}={raw!r} 不是合法的 {cast.__name__}。") from None
        settings = replace(settings, **{field: value})
    return settings


def hello_reply(session_id: str, params: dict | None = None) -> dict:
    return {
        "type": "hello",
        "transport": TRANSPORT,
        "session_id": session_id,
        "audio_params": dict(params or DOWNLINK_PARAMS),
    }


def tts_start(session_id: str) -> dict:
    return {"session_id": session_id, "type": "tts", "state": "start"}


def tts_sentence(text: str, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "type": "tts",
        "state": "sentence_start",
        "text": text,
    }


def tts_stop(session_id: str) -> dict:
    return {"session_id": session_id, "type": "tts", "state": "stop"}


def align_params(reported: object) -> tuple[dict, str]:
    """设备上报的 ``audio_params``（上行）→ ``(要用的参数, 回落原因)``。

    **只对齐上行 16 kHz**：板子麦克风就是 16k，上行解码照它；声明别的率一律回落
    16k / 60 ms 并把原因交给调用方写日志。下行与它无关——hello 回复固定按
    ``DOWNLINK_PARAMS`` 声明 24 kHz（板子喇叭的率），两条路不混。帧长可以跟着设备
    走（10/20/40/60/120），那只影响我们分帧的节奏，不涉及重采样。

    ``reported`` 不是 dict / 缺字段都算「没声明」，用默认值、不报错。
    """
    if not isinstance(reported, dict) or not reported:
        return dict(UPLINK_PARAMS), ""

    fmt = str(reported.get("format") or "").lower()
    rate = reported.get("sample_rate")
    channels = reported.get("channels")
    frame_ms = reported.get("frame_duration")

    if fmt and fmt != "opus":
        return dict(UPLINK_PARAMS), f"设备要 {fmt}，我们只会 opus"
    if rate is not None and rate != audio.SAMPLE_RATE:
        return dict(UPLINK_PARAMS), f"设备声明上行 {rate!r} Hz，我们按 16 kHz 解（板子麦克风是 16k）"
    if channels is not None and channels != 1:
        return dict(UPLINK_PARAMS), f"设备要 {channels!r} 声道，我们只做单声道"
    if frame_ms is not None and (
        not isinstance(frame_ms, int) or frame_ms not in SUPPORTED_FRAME_MS
    ):
        return dict(UPLINK_PARAMS), f"设备帧长 {frame_ms!r} ms 不受支持"

    return {
        "format": "opus",
        "sample_rate": audio.SAMPLE_RATE,
        "channels": 1,
        "frame_duration": frame_ms if isinstance(frame_ms, int) else audio.FRAME_MS,
    }, ""


@dataclass(frozen=True)
class Utterance:
    """一段听音窗口的产物：给 ASR 的 WAV + 几个便于打日志的数。"""

    wav: bytes
    seconds: float
    packets: int
    decode_errors: int


class Conversation:
    """一条连接的协议状态。只认 hello / listen / abort，别的类型一律忽略。

    ``codec_factory`` 是测试接缝：默认 ``audio.Codec``；测试传一个假编解码器，
    就不需要 libopus。它接受 ``(sample_rate, channels, frame_ms)`` 三个位置参数。
    """

    def __init__(
        self,
        session_id: str,
        *,
        codec_factory=None,
        vad: VadSettings | None = None,
        barge: BargeSettings | None = None,
        log=None,
    ):
        self.session_id = session_id
        self._log = log or (lambda _message: None)
        self.params: dict = dict(UPLINK_PARAMS)
        self.params_reason = ""  # 非空表示「设备参数不支持，已回落」，只用于日志
        # 下行声明：hello 回复与 TTS 编码都用它；帧长跟上行的节奏保持一致。
        self.output_params: dict = dict(DOWNLINK_PARAMS)
        self.listening = False
        self.audio_bytes = 0
        self.stray_packets = 0
        self.audio_error = ""
        self._aborted = False
        self._stopped = False
        self._codec_factory = codec_factory or audio.Codec
        self._codec = None
        self._pcm = bytearray()
        self._packets = 0
        self._decode_errors = 0
        # 端点检测状态（见 _feed_vad）：都不碰网络、不碰时钟，纯按音频时长推进。
        self.vad = vad or vad_settings_from_env()
        self._vad_offset = 0  # 已做过 RMS 的字节偏移（VAD 消费 ≠ 缓冲清空）
        self._speech_seen = False
        self._silence_ms = 0  # 说话之后累计的尾静音
        self._silent_ms = 0  # 从头累计的连续无语音
        self._utterance_ms = 0  # 当前这句已累计的音频时长（含说话前/后的静音帧）
        self._endpoint = False
        self._max_cut_bytes = 0  # >0 表示端点是「单句超长」强制切的，切点在缓冲的这个字节处
        self._barge_armed = False
        # 打断判定（barge-in）：server 在播报宽限期过后调用 arm_barge()，此后
        # 每帧进滑窗——宽限期内的帧不进窗口（arm 之前一律忽略），窗口内高能帧
        # 占比 ≥ ratio 即判定有人在说话（判据说明见 BargeSettings 上方）。
        self.barge = barge or barge_settings_from_env()
        self._barge_window: deque[bool] = deque()  # 滑窗里的帧是否高能（按时间序）
        # 诊断用的帧 RMS 峰值：server 播报期间每秒取走一次写 [barge] 日志。
        self._barge_peak_rms = 0.0
        # 「有人说话」的瞬时证据：出现高于阈值的帧就置位，server 逐包取走
        # （take_speech_tick），用来重置会话空闲计时。唤醒词回声同样置位——
        # 孩子刚开口，窗口不该在他喊完的瞬间过期。
        self._speech_tick = False

    # --- 设备发来的 --------------------------------------------------------

    def on_text(self, raw: str) -> list[dict]:
        """一条文本消息 → 要立刻回的消息列表。坏 JSON / 未知类型都返回空表，绝不抛。"""
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(message, dict):
            return []

        kind = message.get("type")
        if kind == "hello":
            self._apply_hello(message.get("audio_params"))
            return [hello_reply(self.session_id, self.output_params)]
        if kind == "listen":
            return self._on_listen(str(message.get("state") or ""))
        if kind == "abort":
            self.abort()
            return []
        # mcp / iot / llm 之类本卡都不接，静默忽略。
        return []

    def on_audio(self, payload: bytes) -> None:
        """一包上行 Opus。窗口外只计数；坏包只计数，绝不打断连接。"""
        self.audio_bytes += len(payload)
        if not self.listening or not payload:
            if payload:
                self.stray_packets += 1
            return
        codec = self._ensure_codec()
        if codec is None:
            return
        pcm = codec.decode(payload)
        if not pcm:
            self._decode_errors += 1
            return
        self._packets += 1
        self._pcm += pcm
        self._feed_vad()

    def take_utterance(self) -> Utterance | None:
        """把当前缓冲做成一段 WAV 并清空。没有音频返回 ``None``。

        ``listen stop`` 之后或端点检测切句之后调用；调用方拿到就送去 ASR，
        不要留在本地。

        「单句超长」强制切的句子只交出**切点之前**的音频（上限那一段），
        切点之后陆续到达的字节留在缓冲里进入下一句，一帧都不丢。
        """
        if not self._pcm:
            return None
        cut = self._max_cut_bytes
        packets, decode_errors = self._packets, self._decode_errors
        if 0 < cut < len(self._pcm):
            pcm = bytes(self._pcm[:cut])
            del self._pcm[:cut]
            # 切点之后的音频接着当下一句听：VAD 计数归零，偏移对齐新缓冲开头，
            # 切点之后的帧还没做过 RMS（切句时 ``_vad_offset`` 恰好等于切点）。
            self._vad_offset = 0
            self._speech_seen = False
            self._silence_ms = 0
            self._silent_ms = 0
            self._utterance_ms = 0
            self._endpoint = False
            self._max_cut_bytes = 0
            self._packets = 0
            self._decode_errors = 0
        else:
            pcm = bytes(self._pcm)
            self._reset_audio()
        utterance = Utterance(
            wav=audio.pcm_to_wav(
                pcm,
                int(self.params["sample_rate"]),
                channels=int(self.params["channels"]),
            ),
            seconds=len(pcm) / (int(self.params["sample_rate"]) * int(self.params["channels"]) * 2),
            packets=packets,
            decode_errors=decode_errors,
        )
        return utterance

    def abort(self) -> None:
        """打断：关窗口 + 丢缓冲。已经推给板子的音频由 server 侧取消任务。"""
        self.listening = False
        self._reset_audio()
        self._aborted = True

    def take_abort(self) -> bool:
        """取走「收到过 abort」这一次事件。server 每处理一批文本消息检查一次。"""
        seen = self._aborted
        self._aborted = False
        return seen

    def take_stop(self) -> bool:
        """取走「收到过 listen stop」这一次事件。

        server 只在这个标志为真时才调 ``take_utterance()``：窗口中途插进来的
        未知文本（mcp / iot / 坏 JSON）不能把半截音频 premature 地送去识别。
        """
        seen = self._stopped
        self._stopped = False
        return seen

    def take_endpoint(self) -> bool:
        """取走「端点检测判定这句说完了」这一次事件（realtime 连续推流用）。

        语义与 ``take_stop`` 相同：server 只在标志为真时才调 ``take_utterance()``，
        拿到音频后缓冲与 VAD 状态一起清零，继续听下一句。
        """
        seen = self._endpoint
        self._endpoint = False
        return seen

    def arm_barge(self) -> None:
        """打断判定的宽限期已过：从这里开始的帧才进滑窗。

        由 server 在播报开始超过宽限期后调用；arm 之前（含之前的能量）不进窗口，
        这是无 AEC 设备上避开自家喇叭回声的关键。重复调用会清窗重计。
        """
        self._barge_armed = True
        self._barge_window.clear()

    @property
    def barge_armed(self) -> bool:
        return self._barge_armed

    @property
    def barge_ratio(self) -> float:
        """滑窗内高能帧占比（分母是满窗容量 window_ms / frame_ms）。

        窗口还没攒满时也按满窗算，占比偏保守——宁可晚一帧打断，不把刚开口的
        一个音节误判成说话。
        """
        if not self._barge_window:
            return 0.0
        loud = sum(self._barge_window)
        return loud * self.vad.frame_ms / self.barge.window_ms

    @property
    def barge_ms(self) -> float:
        """滑窗内高能帧的总毫秒数（日志/诊断用）。"""
        return sum(self._barge_window) * self.vad.frame_ms

    def take_barge_peak_rms(self) -> float:
        """取走自上次调用以来的帧 RMS 峰值并归零（播报诊断日志用）。"""
        peak = self._barge_peak_rms
        self._barge_peak_rms = 0.0
        return peak

    def take_speech_tick(self) -> bool:
        """自上次调用以来有没有出现过高于阈值的声音。

        server 每收一包取一次：为真就把会话空闲计时重置（最后一次有人说话）。
        """
        seen = self._speech_tick
        self._speech_tick = False
        return seen

    # --- 内部 --------------------------------------------------------------

    def _feed_vad(self) -> None:
        """对缓冲里新到的完整帧做 RMS 端点检测（纯同步，不碰时钟）。

        - 帧内 RMS ≥ 阈值 → 有人在说话，尾静音计数归零；
        - 说话之后连续累计 ≥ ``tail_ms`` 低能量 → 这句结束，置 ``_endpoint``；
        - 从头累计 ≥ ``idle_seconds`` 无语音 → 清空缓冲重来（不发声音、不报错）；
        - 这句累计 ≥ ``max_utterance_ms``（默认 30 s，一口气不换气）→ 强制置
          ``_endpoint``，并记下切点：``take_utterance`` 只交出切点前的音频，
          之后到的字节留在缓冲里当下一句的开头（见 ``take_utterance``）。

        切句后（``take_utterance`` → ``_reset_audio``）状态归零，同一条连接
        继续听下一句。判定结果只走 ``take_endpoint``，绝不进 ``spoken``。
        """
        if self._endpoint:
            return
        frame_bytes = self._vad_frame_bytes()
        if frame_bytes <= 0:
            return
        while len(self._pcm) - self._vad_offset >= frame_bytes:
            frame = bytes(self._pcm[self._vad_offset : self._vad_offset + frame_bytes])
            self._vad_offset += frame_bytes
            self._utterance_ms += self.vad.frame_ms
            rms = audio.frame_rms(frame)
            if rms > self._barge_peak_rms:
                self._barge_peak_rms = rms
            if self._barge_armed:
                # 打断判据与端点检测各算各的：高能帧进滑窗，超出窗口容量的旧帧
                # 淘汰；占比够不够由 server 读 barge_ratio 判定。
                self._barge_window.append(rms >= self.vad.threshold)
                capacity = max(1, round(self.barge.window_ms / self.vad.frame_ms))
                while len(self._barge_window) > capacity:
                    self._barge_window.popleft()
            if 0 < self.vad.max_utterance_ms <= self._utterance_ms:
                # 单句硬上限：孩子一口气说不换气时唯一的切句出口，兜住缓冲无界增长。
                self._max_cut_bytes = self._vad_offset
                self._endpoint = True
                self._log(
                    f"[vad] 单句超过 {self.vad.max_utterance_ms}ms，"
                    f"强制切句（已收 {self._utterance_ms / 1000:.2f} s）"
                )
                return
            if rms >= self.vad.threshold:
                self._speech_seen = True
                self._speech_tick = True
                self._silence_ms = 0
                continue
            if self._speech_seen:
                self._silence_ms += self.vad.frame_ms
                if self._silence_ms >= self.vad.tail_ms:
                    self._endpoint = True
                    return
            else:
                self._silent_ms += self.vad.frame_ms
                if self._silent_ms >= self.vad.idle_seconds * 1000:
                    self._reset_audio()
                    return

    def _vad_frame_bytes(self) -> int:
        rate = int(self.params["sample_rate"])
        channels = int(self.params["channels"])
        return rate * self.vad.frame_ms // 1000 * channels * 2

    def _apply_hello(self, reported: object) -> None:
        self.params, self.params_reason = align_params(reported)
        # 参数变了，旧解码器就不能再用了（帧长/采样率都绑在它身上）。
        self._codec = None
        # 下行帧长跟上行的节奏走；率与声道固定 24k/单声道，不随设备声明变。
        self.output_params = {
            **DOWNLINK_PARAMS,
            "frame_duration": self.params["frame_duration"],
        }

    def _ensure_codec(self):
        if self._codec is not None:
            return self._codec
        try:
            self._codec = self._codec_factory(
                int(self.params["sample_rate"]),
                int(self.params["channels"]),
                int(self.params["frame_duration"]),
            )
        except audio.AudioError as exc:
            if not self.audio_error:
                self.audio_error = str(exc)
            return None
        return self._codec

    def _on_listen(self, state: str) -> list[dict]:
        if state in OPEN_STATES:
            self.listening = True
            self._reset_audio()
            return []
        if state == "stop":
            self.listening = False
            self._stopped = True
            return []
        return []

    def _reset_audio(self) -> None:
        self._pcm.clear()
        self._packets = 0
        self._decode_errors = 0
        # VAD 状态一起归零：开新窗 / abort / 清缓冲 / 交出音频后都从头听。
        self._vad_offset = 0
        self._speech_seen = False
        self._silence_ms = 0
        self._silent_ms = 0
        self._utterance_ms = 0
        self._max_cut_bytes = 0
        self._endpoint = False
        self._barge_armed = False
        self._barge_window.clear()

    @property
    def pending_seconds(self) -> float:
        """当前缓冲里有几秒音频（日志用，不消耗缓冲）。"""
        frame = int(self.params["sample_rate"]) * int(self.params["channels"]) * 2
        return len(self._pcm) / frame if frame else 0.0
