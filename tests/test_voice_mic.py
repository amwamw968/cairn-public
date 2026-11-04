"""麦克风断句：纯函数 ``segment`` 用合成块覆盖五种情形，``listen`` 用假流验证接线。

全程不 open 真麦克风、不 import sounddevice：``listen`` 的流工厂可注入。
"""

from __future__ import annotations

import array
import wave
from io import BytesIO

import pytest

from cairn.voice import mic

BLOCK = mic.BLOCK_SIZE
SPEECH = 2000  # 2000/32768 ≈ 0.061，高于默认阈值 0.008
SILENCE = 0


def make_block(amplitude: int, block_size: int = BLOCK) -> bytes:
    """一个 int16 块：正负交替的方波，RMS 恰好是 amplitude/32768。"""
    samples = array.array(
        "h", [amplitude if i % 2 == 0 else -amplitude for i in range(block_size)]
    )
    return samples.tobytes()


def silence_blocks(count: int) -> list[bytes]:
    return [make_block(SILENCE) for _ in range(count)]


def read_wav(payload: bytes) -> tuple[int, int, int, int]:
    with wave.open(BytesIO(payload)) as handle:
        return (
            handle.getnchannels(),
            handle.getsampwidth(),
            handle.getframerate(),
            handle.getnframes(),
        )


def test_normal_sentence_returns_standard_wav() -> None:
    """0.256 秒人声 + 尾部静音够 0.8 秒 → 正常出 WAV，头是 16kHz/16bit/单声道。"""
    blocks = [make_block(SPEECH) for _ in range(4)] + silence_blocks(13)

    utterance = mic.segment(blocks)

    assert utterance.reason == mic.OK
    assert utterance.truncated is False
    assert utterance.wav is not None
    assert read_wav(utterance.wav) == (1, 2, 16000, 17 * BLOCK)
    assert utterance.speech_seconds == pytest.approx(4 * BLOCK / 16000)


def test_too_short_blip_is_discarded() -> None:
    """只响了一下（64ms）→ 不送 ASR，明确说太短。"""
    blocks = [make_block(SPEECH)] + silence_blocks(13)

    utterance = mic.segment(blocks)

    assert utterance.reason == mic.TOO_SHORT
    assert utterance.wav is None
    assert "太短" in utterance.detail
    assert utterance.speech_seconds == pytest.approx(BLOCK / 16000)


def test_max_duration_truncates_to_limit() -> None:
    """一直说不停 → 截到 15 秒为止，剩下的照样能识别。"""
    blocks = [make_block(SPEECH) for _ in range(300)]

    utterance = mic.segment(blocks)

    assert utterance.reason == mic.OK
    assert utterance.truncated is True
    assert utterance.wav is not None
    channels, width, rate, frames = read_wav(utterance.wav)
    assert (channels, width, rate) == (1, 2, 16000)
    assert frames == int(mic.MAX_SECONDS * mic.SAMPLE_RATE)


def test_pure_silence_never_triggers() -> None:
    """一直是底噪 → 等够 max_wait 就返回「没听到」，不会无限等。"""
    consumed = 0

    def quiet():
        nonlocal consumed
        for _ in range(400):
            consumed += 1
            yield make_block(SILENCE)

    utterance = mic.segment(quiet())

    assert utterance.reason == mic.NO_SPEECH
    assert utterance.wav is None
    assert "没听到" in utterance.detail
    # 10 秒等待 = 157 块左右；断言「提前停了」而不是把 400 块吃完
    assert 150 <= consumed <= 160


def test_threshold_is_configurable() -> None:
    """阈值调高到人声以上 → 同一段音频按静音处理。"""
    blocks = [make_block(SPEECH) for _ in range(4)] + silence_blocks(13)

    utterance = mic.segment(blocks, rms_threshold=0.5)

    assert utterance.reason == mic.NO_SPEECH


class FakeStream:
    """假采集流：按块吐数据，取完就吐静音（模拟麦克风一直开着）。"""

    def __init__(self, blocks: list[bytes], block_size: int) -> None:
        self._blocks = iter(blocks)
        self._block_size = block_size
        self.closed = False

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.closed = True
        return False

    def read(self, block_size: int):
        try:
            return next(self._blocks), False
        except StopIteration:
            return bytes(block_size * 2), False


def test_listen_wires_stream_correctly() -> None:
    """``listen`` 按 16k / 单声道 / int16 开流，并把块序列交给 ``segment``。"""
    seen: dict[str, object] = {}
    blocks = [make_block(SPEECH) for _ in range(4)] + silence_blocks(13)

    def factory(**kwargs):
        seen.update(kwargs)
        return FakeStream(blocks, kwargs["blocksize"])

    utterance = mic.listen(stream_factory=factory)

    assert seen["samplerate"] == 16000
    assert seen["channels"] == 1
    assert seen["dtype"] == "int16"
    assert utterance.reason == mic.OK
    assert utterance.wav is not None


def test_device_error_returns_chinese_hint(capsys) -> None:
    """设备打不开（没权限 / 被占用）：返回 device_error，不抛栈。"""

    def factory(**kwargs):
        raise OSError("PortAudioError: Device unavailable")

    utterance = mic.listen(stream_factory=factory)

    assert utterance.reason == mic.DEVICE_ERROR
    assert utterance.wav is None
    assert "麦克风不可用" in utterance.detail
    assert "麦克风不可用" in capsys.readouterr().err


def test_missing_sounddevice_gives_install_hint(monkeypatch) -> None:
    """没装 sounddevice：提示装法，不把 ImportError 甩出来。"""
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("No module named 'sounddevice'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    utterance = mic.listen()

    assert utterance.reason == mic.DEVICE_ERROR
    assert "pip install" in utterance.detail


def test_pcm_to_wav_writes_canonical_header() -> None:
    pcm = make_block(SPEECH) * 2
    payload = mic.pcm_to_wav(pcm)

    assert payload[:4] == b"RIFF"
    assert payload[8:12] == b"WAVE"
    assert read_wav(payload) == (1, 2, 16000, 2 * BLOCK)
    assert len(payload) == 44 + len(pcm)


def test_block_rms_matches_amplitude() -> None:
    assert mic.block_rms(make_block(SPEECH)) == pytest.approx(SPEECH / 32768)
    assert mic.block_rms(make_block(SILENCE)) == 0.0
    assert mic.block_rms(b"") == 0.0
