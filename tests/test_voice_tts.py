"""TTS：命令拼得对、音色可覆盖、任何失败都只降级不抛异常。"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from cairn.voice import tts

SAY = "/usr/bin/say"
TEXT = "第一题，英语。"


@pytest.fixture()
def calls():
    """假 runner：记录 argv，按测试给的 returncode 返回。"""
    seen: list[list[str]] = []
    state = SimpleNamespace(returncode=0, stderr=b"")

    def runner(argv, **kwargs):
        seen.append(list(argv))
        assert kwargs["check"] is False
        return SimpleNamespace(returncode=state.returncode, stderr=state.stderr)

    return SimpleNamespace(seen=seen, state=state, runner=runner)


def test_speak_calls_say_with_text(calls) -> None:
    assert tts.speak(TEXT, say_path=SAY, runner=calls.runner) is None
    assert calls.seen == [[SAY, "--", TEXT]]


def test_voice_argument_becomes_dash_v(calls) -> None:
    tts.speak(TEXT, voice="Tingting", say_path=SAY, runner=calls.runner)
    assert calls.seen == [[SAY, "-v", "Tingting", "--", TEXT]]


def test_voice_env_overrides_default(monkeypatch, calls) -> None:
    monkeypatch.setenv(tts.ENV_VOICE, "Flo")
    tts.speak(TEXT, say_path=SAY, runner=calls.runner)
    assert calls.seen == [[SAY, "-v", "Flo", "--", TEXT]]


def test_explicit_voice_wins_over_env(monkeypatch, calls) -> None:
    monkeypatch.setenv(tts.ENV_VOICE, "Flo")
    tts.speak(TEXT, voice="Tingting", say_path=SAY, runner=calls.runner)
    assert calls.seen == [[SAY, "-v", "Tingting", "--", TEXT]]


def test_missing_say_falls_back_to_print(monkeypatch, capsys) -> None:
    """没有 say（非 macOS）：话打出来 + 一句提示，不抛异常。"""
    monkeypatch.setattr(tts.shutil, "which", lambda name: None)

    tts.speak(TEXT, runner=lambda *a, **k: pytest.fail("不该调用 runner"))

    captured = capsys.readouterr()
    assert TEXT in captured.out
    assert "没有 say 命令" in captured.err


def test_nonzero_exit_falls_back(calls, capsys) -> None:
    calls.state.returncode = 3
    calls.state.stderr = b"voice not found"

    tts.speak(TEXT, say_path=SAY, runner=calls.runner)

    captured = capsys.readouterr()
    assert TEXT in captured.out
    assert "退出码 3" in captured.err
    assert "voice not found" in captured.err


def test_timeout_falls_back(capsys) -> None:
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    tts.speak(TEXT, say_path=SAY, runner=runner)

    captured = capsys.readouterr()
    assert TEXT in captured.out
    assert "没播完" in captured.err


def test_oserror_falls_back(capsys) -> None:
    def runner(argv, **kwargs):
        raise OSError("exec format error")

    tts.speak(TEXT, say_path=SAY, runner=runner)

    captured = capsys.readouterr()
    assert TEXT in captured.out
    assert "调用 say 失败" in captured.err


def test_empty_text_does_nothing(calls, capsys) -> None:
    tts.speak("   ", say_path=SAY, runner=calls.runner)

    assert calls.seen == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
