"""ASR 客户端：请求拼得对、异常分得清、密钥一个字都不许漏。

全部用例替换 ``asr._post``（唯一网络出口），不碰真网络、不读真密钥。
"""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest

from cairn.voice import asr

KEY = "sk-example-not-a-real-key"
WAV = b"RIFF....WAVEfmt "


@pytest.fixture()
def key_file(tmp_path):
    path = tmp_path / "qwen_asr.key"
    path.write_text(f"{KEY}\n", encoding="utf-8")
    return path


@pytest.fixture()
def capture(monkeypatch):
    """把 ``_post`` 换成记录器；测试自己决定返回什么 / 抛什么。"""
    seen: dict[str, object] = {}

    def fake_post(url, *, data, headers, timeout):
        seen.update(url=url, data=data, headers=headers, timeout=timeout)
        return 200, json.dumps({"text": " 给我出一道英语题。 "}).encode()

    monkeypatch.setattr(asr, "_post", fake_post)
    return seen


def test_transcribe_posts_multipart_and_strips_text(capture, key_file) -> None:
    """URL / multipart 字段 / Authorization 都对，返回值去掉首尾空白。"""
    text = asr.transcribe(WAV, key_file=str(key_file))

    assert text == "给我出一道英语题。"
    assert capture["url"] == "http://127.0.0.1:8040/v1/audio/transcriptions"
    assert capture["headers"]["Authorization"] == f"Bearer {KEY}"
    body = capture["data"]
    assert isinstance(body, bytes)
    assert b'name="file"; filename="speech.wav"' in body
    assert WAV in body
    assert b'name="model"' in body
    assert asr.DEFAULT_MODEL.encode() in body
    assert capture["timeout"] == asr.DEFAULT_TIMEOUT


def test_env_vars_override_defaults(monkeypatch, capture, key_file) -> None:
    """三个环境变量都能改端点和模型。"""
    monkeypatch.setenv(asr.ENV_URL, "http://10.0.0.9:9000/")
    monkeypatch.setenv(asr.ENV_MODEL, "demo/asr-small")
    monkeypatch.setenv(asr.ENV_KEY_FILE, str(key_file))

    asr.transcribe(WAV)

    assert capture["url"] == "http://10.0.0.9:9000/v1/audio/transcriptions"
    assert b"demo/asr-small" in capture["data"]


def test_missing_key_file_raises_key_error_without_network(monkeypatch, tmp_path) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("没有 key 就不该发请求")

    monkeypatch.setattr(asr, "_post", boom)

    with pytest.raises(asr.ASRKeyError) as info:
        asr.transcribe(WAV, key_file=str(tmp_path / "nope.key"))

    assert "读不到 ASR 密钥文件" in str(info.value)
    assert KEY not in str(info.value)


def test_empty_key_file_raises(key_file) -> None:
    key_file.write_text("   \n", encoding="utf-8")
    with pytest.raises(asr.ASRKeyError, match="是空的"):
        asr.transcribe(WAV, key_file=str(key_file))


def test_empty_wav_raises_before_network(monkeypatch, key_file) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("空音频不该发请求")

    monkeypatch.setattr(asr, "_post", boom)
    with pytest.raises(asr.ASRError, match="没有可识别的音频"):
        asr.transcribe(b"", key_file=str(key_file))


def test_http_error_is_translated_and_key_is_scrubbed(monkeypatch, key_file) -> None:
    """非 200 → ``ASRHTTPError``；响应体里就算带了 key，消息里也不许出现。"""
    headers = Message()
    payload = json.dumps({"error": f"bad key {KEY}"}).encode()

    def fake_post(url, *, data, headers, timeout):
        raise urllib.error.HTTPError(
            url, 401, "Unauthorized", headers, io.BytesIO(payload)
        )

    monkeypatch.setattr(asr, "_post", fake_post)

    with pytest.raises(asr.ASRHTTPError) as info:
        asr.transcribe(WAV, key_file=str(key_file))

    message = str(info.value)
    assert "HTTP 401" in message
    assert KEY not in message
    assert "***" in message


def test_timeout_is_translated(monkeypatch, key_file) -> None:
    def fake_post(url, *, data, headers, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(asr, "_post", fake_post)

    with pytest.raises(asr.ASRTimeoutError, match="秒内没有响应"):
        asr.transcribe(WAV, key_file=str(key_file), timeout=3)


def test_connection_error_is_translated(monkeypatch, key_file) -> None:
    def fake_post(url, *, data, headers, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(asr, "_post", fake_post)

    with pytest.raises(asr.ASRError) as info:
        asr.transcribe(WAV, key_file=str(key_file))

    assert "连不上 ASR 服务" in str(info.value)
    assert KEY not in str(info.value)


def test_empty_result_raises(monkeypatch, key_file) -> None:
    monkeypatch.setattr(
        asr, "_post", lambda url, *, data, headers, timeout: (200, b'{"text":"   "}')
    )

    with pytest.raises(asr.ASREmptyResultError, match="没听出文字"):
        asr.transcribe(WAV, key_file=str(key_file))


def test_non_json_result_raises(monkeypatch, key_file) -> None:
    monkeypatch.setattr(
        asr, "_post", lambda url, *, data, headers, timeout: (200, b"<html>502</html>")
    )

    with pytest.raises(asr.ASRResponseError, match="不是合法 JSON"):
        asr.transcribe(WAV, key_file=str(key_file))


def test_missing_text_field_raises(monkeypatch, key_file) -> None:
    monkeypatch.setattr(
        asr, "_post", lambda url, *, data, headers, timeout: (200, b'{"ok":true}')
    )

    with pytest.raises(asr.ASRResponseError, match="没有 text 字段"):
        asr.transcribe(WAV, key_file=str(key_file))


def test_all_errors_share_one_base_class() -> None:
    """调用方只认 ``ASRError`` 就能兜住全部失败。"""
    for exc_type in (
        asr.ASRKeyError,
        asr.ASRTimeoutError,
        asr.ASRHTTPError,
        asr.ASRResponseError,
        asr.ASREmptyResultError,
    ):
        assert issubclass(exc_type, asr.ASRError)


# --- 领域提示词（真机 2026-10-31：短指令常被听成近音字） --------------------


def test_prompt_field_included_by_default(monkeypatch, capture, key_file) -> None:
    """不配环境变量也带默认领域提示词：孩子最常说的短指令。"""
    monkeypatch.delenv(asr.ENV_PROMPT, raising=False)

    asr.transcribe(WAV, key_file=str(key_file))

    body = capture["data"]
    assert isinstance(body, bytes)
    assert b'name="prompt"' in body
    assert asr.DEFAULT_ASR_PROMPT.encode() in body


def test_prompt_env_override(monkeypatch, capture, key_file) -> None:
    """``CAIRN_ASR_PROMPT`` 覆盖默认词表。"""
    monkeypatch.setenv(asr.ENV_PROMPT, "自定义词表 甲 乙")

    asr.transcribe(WAV, key_file=str(key_file))

    body = capture["data"]
    assert "自定义词表 甲 乙".encode() in body
    assert asr.DEFAULT_ASR_PROMPT.encode() not in body


def test_prompt_setting_resolution(monkeypatch) -> None:
    """显式参数 > 环境变量 > 默认词表；启动日志的 on/off 也走这里。"""
    monkeypatch.delenv(asr.ENV_PROMPT, raising=False)
    assert asr.prompt_setting() == asr.DEFAULT_ASR_PROMPT

    monkeypatch.setenv(asr.ENV_PROMPT, "环境词表")
    assert asr.prompt_setting() == "环境词表"
    assert asr.prompt_setting("显式词表") == "显式词表"


def test_prompt_failure_falls_back_without_prompt(monkeypatch, key_file) -> None:
    """服务端不认识 prompt 字段（400）：自动退化成不带 prompt 再试一次并成功。"""
    bodies: list[bytes] = []

    def fake_post(url, *, data, headers, timeout):
        assert isinstance(data, bytes)
        bodies.append(data)
        if len(bodies) == 1:
            raise urllib.error.HTTPError(
                url, 400, "Bad Request", Message(), io.BytesIO(b"unknown field: prompt")
            )
        return 200, json.dumps({"text": "停一下"}).encode()

    monkeypatch.setattr(asr, "_post", fake_post)
    text = asr.transcribe(WAV, key_file=str(key_file))

    assert text == "停一下"
    assert len(bodies) == 2
    assert b'name="prompt"' in bodies[0]
    assert asr.DEFAULT_ASR_PROMPT.encode() in bodies[0]
    assert b'name="prompt"' not in bodies[1]


def test_both_prompt_and_fallback_fail_raise(monkeypatch, key_file) -> None:
    """两次都失败才算失败：最后一次的错误原样上抛。"""
    calls: list[int] = []

    def fake_post(url, *, data, headers, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(
            url, 400, "Bad Request", Message(), io.BytesIO(b"bad")
        )

    monkeypatch.setattr(asr, "_post", fake_post)

    with pytest.raises(asr.ASRHTTPError, match="HTTP 400"):
        asr.transcribe(WAV, key_file=str(key_file))

    assert len(calls) == 2
