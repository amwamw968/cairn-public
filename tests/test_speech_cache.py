"""TTS 缓存层：规范化 / 缓存命中 / 损坏隔离 / LRU 淘汰 / 单飞 / 完整性校验。"""

from __future__ import annotations

import os
import struct
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from cairn.voice import speech_cache, tts
from cairn.voice.speech_cache import SpeechAudioCache, canonicalize, cache_key, is_valid_audio

TEXT = "第一题，英语。"


# -- 工具 -------------------------------------------------------------------


def make_wav(data_len: int = 320) -> bytes:
    """手工拼一段合法 WAV（PCM 参数任意，只要 RIFF 块自洽且总长 ≥256 B）。"""
    data = b"\x00\x01" * (data_len // 2)
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 16000 * 2, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.fixture()
def wav_runner():
    """假合成器：往 -o 指定的路径写一段合法 WAV，并记录每次调用。"""
    calls: list[list[str]] = []
    audio = make_wav()

    def runner(argv, **kwargs):
        calls.append(list(argv))
        out = Path(argv[argv.index("-o") + 1])
        out.write_bytes(audio)
        return SimpleNamespace(returncode=0, stderr=b"")

    return SimpleNamespace(calls=calls, runner=runner, audio=audio)


def cache_file(tmp_path: Path, key: str) -> Path:
    return tmp_path / f"{key}.wav"


# -- 规范化 -----------------------------------------------------------------


def test_canonicalize_fullwidth_space() -> None:
    """全角空格（U+3000）也是空白：首尾去掉、中间合并。"""
    assert canonicalize("　你好　") == "你好"
    assert canonicalize("你好　　世界") == "你好 世界"


def test_canonicalize_collapses_consecutive_whitespace() -> None:
    assert canonicalize("  a  b \n\t c  ") == "a b c"
    assert canonicalize("a\t\tb\nc") == "a b c"


def test_canonicalize_rejects_control_chars_and_empty() -> None:
    assert canonicalize("a\x00b") is None  # Cc
    assert canonicalize("a\u200bb") is None  # ZWSP，Cf
    assert canonicalize("") is None
    assert canonicalize("   ") is None


def test_cache_key_varies_by_voice_and_rate() -> None:
    text = canonicalize(TEXT)
    assert text is not None
    base = cache_key(text)
    assert base == cache_key(text, "", "")
    assert base != cache_key(text, "Tingting", "")
    assert base != cache_key(text, "", 160)
    assert base != cache_key("另一句", "", "")


# -- 合成路径：缓存命中 / 损坏重合成 -----------------------------------------


def test_second_synthesis_hits_cache(tmp_path, monkeypatch, wav_runner) -> None:
    monkeypatch.delenv(tts.ENV_VOICE, raising=False)
    cache = SpeechAudioCache(tmp_path)

    first, hit1 = tts.synthesize_ex(TEXT, say_path="/usr/bin/say", runner=wav_runner.runner, cache=cache)
    second, hit2 = tts.synthesize_ex(TEXT, say_path="/usr/bin/say", runner=wav_runner.runner, cache=cache)

    assert hit1 is False
    assert hit2 is True
    assert second == first == wav_runner.audio
    assert len(wav_runner.calls) == 1  # 第二次没再合成


def test_corrupt_cache_is_isolated_and_resynthesized(tmp_path, monkeypatch, wav_runner, caplog) -> None:
    monkeypatch.delenv(tts.ENV_VOICE, raising=False)
    cache = SpeechAudioCache(tmp_path)
    key = cache_key(canonicalize(TEXT) or "", "", "")
    cache_file(tmp_path, key).write_bytes(wav_runner.audio[:100])  # 截断 → 校验不过

    with caplog.at_level("WARNING", logger="cairn.voice.speech_cache"):
        data, hit = tts.synthesize_ex(TEXT, say_path="/usr/bin/say", runner=wav_runner.runner, cache=cache)

    assert hit is False
    assert data == wav_runner.audio
    assert len(wav_runner.calls) == 1  # 损坏视为未命中 → 重合成一次
    assert "[tts] 缓存损坏" in caplog.text and key in caplog.text
    corrupt = Path(str(cache_file(tmp_path, key)) + ".corrupt")
    assert corrupt.exists()  # 坏文件被改名隔离
    assert corrupt.read_bytes() == wav_runner.audio[:100]
    # 重合成的结果已重新落盘，此后再调用应命中
    assert cache.get(key) == wav_runner.audio
    assert len(wav_runner.calls) == 1  # 全程只重合成了一次


# -- 磁盘缓存：双限 LRU 淘汰 --------------------------------------------------


def test_evicts_least_recently_used_over_entry_limit(tmp_path) -> None:
    cache = SpeechAudioCache(tmp_path, max_entries=2)
    audio = make_wav()
    for i, t in enumerate((1000, 2000, 3000)):
        cache.put(f"k{i}", audio)
        os.utime(tmp_path / f"k{i}.wav", (t, t))  # 手动排访问时间：k0 < k1 < k2

    cache.get("k1")  # 触摸 k1 → 最久未用变成 k0
    cache.put("k3", audio)

    names = sorted(p.name for p in tmp_path.iterdir())
    assert "k1.wav" in names and "k3.wav" in names
    assert "k0.wav" not in names  # 超过 2 条上限，淘汰最久未用的 k0


def test_put_rejects_invalid_audio(tmp_path) -> None:
    cache = SpeechAudioCache(tmp_path)
    cache.put("bad", b"\x00" * 10)  # 长度过小，校验不过
    assert cache.count() == 0


# -- 单飞 --------------------------------------------------------------------


def test_single_flight_same_key_synth_once(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(tts.ENV_VOICE, raising=False)
    cache = SpeechAudioCache(tmp_path)
    calls: list[list[str]] = []
    audio = make_wav()
    release = threading.Event()

    def slow_runner(argv, **kwargs):
        calls.append(list(argv))
        Path(argv[argv.index("-o") + 1]).write_bytes(audio)
        release.wait(timeout=5)  # 拖住第一个合成者，制造真并发
        return SimpleNamespace(returncode=0, stderr=b"")

    results: list[tuple[bytes, bool]] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            results.append(
                tts.synthesize_ex(TEXT, say_path="/usr/bin/say", runner=slow_runner, cache=cache)
            )
        except Exception as exc:  # pragma: no cover - 失败时让断言报出来
            errors.append(exc)

    first = threading.Thread(target=worker)
    first.start()
    while not calls:
        assert first.is_alive()
    second = threading.Thread(target=worker)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not errors
    assert len(calls) == 1  # 两个并发请求只合成一次
    assert results[0][0] == results[1][0]  # 双方拿到同一段音频
    assert results[0][1] is False and results[1][1] is True  # 排队者拿到的是缓存命中


# -- 完整性校验 ---------------------------------------------------------------


def test_is_valid_audio_rejects_too_small() -> None:
    assert is_valid_audio(make_wav()[:200]) is False  # < 256 B
    assert is_valid_audio(b"") is False


def test_is_valid_audio_rejects_broken_wav_header() -> None:
    audio = make_wav()
    assert is_valid_audio(b"RIFX" + audio[4:]) is False  # 不是 RIFF
    broken = bytearray(audio)
    broken[-1] ^= 0xFF  # data 块声明长度与实际内容不再自洽的变体：改 RIFF 总长
    broken[4:8] = struct.pack("<I", len(audio) * 2)
    assert is_valid_audio(bytes(broken)) is False


def test_is_valid_audio_accepts_real_wav() -> None:
    assert is_valid_audio(make_wav()) is True
    assert is_valid_audio(make_wav(data_len=2048)) is True


def test_is_valid_audio_mp3_rough_check() -> None:
    assert is_valid_audio(b"ID3" + b"\x00" * 300) is True  # ID3 头
    assert is_valid_audio(b"\xff\xfb\x90\x00" + b"\x00" * 300) is True  # 帧同步字
    assert is_valid_audio(b"\x00" * 300) is False  # 什么头都没有
