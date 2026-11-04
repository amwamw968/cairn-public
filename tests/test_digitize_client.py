"""视觉模型适配层：请求打到哪、解析扛不扛得住脏输出、密钥一个字都不许漏。

全部用例替换 ``client._post``（唯一网络出口），不碰真网络、不读真密钥。
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from email.message import Message

import pytest

from cairn.digitize import Digitized, DigitizeError, describe
from cairn.digitize import client

KEY = "sk-example-not-a-real-key"
PNG = b"\x89PNG\r\n\x1a\n-fake-png"
GOOD = '{"stem": "1+1=?", "answer": "2", "analysis": "整数加法。"}'


def _reply(content: str) -> bytes:
    """OpenAI 兼容 chat 返回体：只填 choices[0].message.content。"""
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


@pytest.fixture()
def image(tmp_path):
    path = tmp_path / "q1.png"
    path.write_bytes(PNG)
    return path


@pytest.fixture()
def key_file(tmp_path):
    path = tmp_path / "omlx.key"
    path.write_text(f"{KEY}\n", encoding="utf-8")
    return path


@pytest.fixture()
def capture(monkeypatch, key_file):
    """把 ``_post`` 换成记录器；两个后端的 key 环境变量都指到临时文件。"""
    seen: dict[str, object] = {}

    def fake_post(url, *, data, headers, timeout):
        seen.update(url=url, data=data, headers=headers, timeout=timeout)
        return 200, _reply(GOOD)

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setenv("CAIRN_VLM_LOCAL_KEY_FILE", str(key_file))
    monkeypatch.setenv("CAIRN_VLM_CLOUD_KEY_FILE", str(key_file))
    return seen


def _body(capture) -> dict:
    data = capture["data"]
    assert isinstance(data, bytes)
    return json.loads(data)


def test_default_provider_is_cloud_host_and_model(capture, image) -> None:
    """AC2：不给 provider → 云端端点 + 32B 模型 + Bearer 头 + temperature 0。"""
    result = describe(str(image))

    assert isinstance(result, Digitized)
    assert "api.siliconflow.cn" in capture["url"]
    assert capture["headers"]["Authorization"] == f"Bearer {KEY}"
    body = _body(capture)
    assert body["model"] == "Qwen/Qwen3-VL-32B-Instruct"
    assert body["temperature"] == 0
    assert body["max_tokens"] == client.MAX_TOKENS
    assert capture["timeout"] == 90.0
    assert result.provider == "cloud"
    assert result.model == "Qwen/Qwen3-VL-32B-Instruct"
    assert result.ms >= 0


def test_local_provider_still_works_when_asked(capture, image) -> None:
    """AC3：显式 provider="local" → 本机 oMLX 端点 + 4B 模型（本地后端没被删掉）。"""
    result = describe(str(image), provider="local")

    assert capture["url"] == "http://127.0.0.1:9981/v1/chat/completions"
    assert capture["headers"]["Authorization"] == f"Bearer {KEY}"
    body = _body(capture)
    assert body["model"] == "Qwen3-VL-4B-Instruct-4bit"
    assert capture["timeout"] == 60.0
    assert result.provider == "local"
    assert result.model == "Qwen3-VL-4B-Instruct-4bit"


def test_cloud_provider_targets_siliconflow(capture, image) -> None:
    """AC3：provider="cloud" → api.siliconflow.cn + 32B 模型 + 90 秒超时。"""
    result = describe(str(image), provider="cloud")

    assert capture["url"] == "https://api.siliconflow.cn/v1/chat/completions"
    body = _body(capture)
    assert body["model"] == "Qwen/Qwen3-VL-32B-Instruct"
    assert capture["headers"]["Authorization"] == f"Bearer {KEY}"
    assert capture["timeout"] == 90.0
    assert result.provider == "cloud"
    assert result.model == "Qwen/Qwen3-VL-32B-Instruct"


def test_unknown_provider_raises_without_request(monkeypatch, image, key_file) -> None:
    """AC4：非法 provider → DigitizeError，消息含可用值，一次请求都不发。"""
    calls: list[str] = []

    def boom(*args, **kwargs):
        calls.append("called")
        raise AssertionError("非法 provider 不该发请求")

    monkeypatch.setattr(client, "_post", boom)
    monkeypatch.setenv("CAIRN_VLM_LOCAL_KEY_FILE", str(key_file))

    with pytest.raises(DigitizeError) as info:
        describe(str(image), provider="gpt")

    message = str(info.value)
    assert "local" in message and "cloud" in message
    assert calls == []


def test_fenced_json_with_chatter_still_parses(monkeypatch, capture, image) -> None:
    """AC5 上半：```json 围栏 + 前后废话 → 三字段照抠。"""
    chatty = f"好的，结果如下：\n```json\n{GOOD}\n```\n希望有帮助。"

    def fake_post(url, *, data, headers, timeout):
        return 200, _reply(chatty)

    monkeypatch.setattr(client, "_post", fake_post)

    result = describe(str(image))

    assert result.stem == "1+1=?"
    assert result.answer == "2"
    assert result.analysis == "整数加法。"


def test_text_without_json_raises(monkeypatch, capture, image) -> None:
    """AC5 下半：返回体里完全没有 JSON → DigitizeError，不许返回空对象。"""
    monkeypatch.setattr(
        client, "_post", lambda url, *, data, headers, timeout: (200, _reply("看不清。"))
    )

    with pytest.raises(DigitizeError, match="没有给出可解析的 JSON"):
        describe(str(image))


def test_json_without_known_fields_raises(monkeypatch, capture, image) -> None:
    """JSON 合法但一个字段都没有，也不算解析成功。"""
    monkeypatch.setattr(
        client, "_post", lambda url, *, data, headers, timeout: (200, _reply('{"ok":true}'))
    )

    with pytest.raises(client.DigitizeResponseError):
        describe(str(image))


def test_missing_key_file_raises_without_key_leak(monkeypatch, tmp_path, image) -> None:
    """AC6：key 文件不存在 → DigitizeError，消息里不含任何 key 片段，也不发请求。"""

    def boom(*args, **kwargs):
        raise AssertionError("没有 key 就不该发请求")

    monkeypatch.setattr(client, "_post", boom)
    monkeypatch.setenv("CAIRN_VLM_LOCAL_KEY_FILE", str(tmp_path / "nope.key"))

    with pytest.raises(DigitizeError) as info:
        describe(str(image), provider="local")

    message = str(info.value)
    assert "读不到视觉模型密钥文件" in message
    assert KEY not in message
    assert "sk-vlm" not in message


def test_http_error_is_translated_and_key_scrubbed(monkeypatch, image, key_file) -> None:
    """非 200 → DigitizeHTTPError；响应体里就算带了 key，消息里也不许出现。"""
    payload = json.dumps({"error": f"bad key {KEY}"}).encode()

    def fake_post(url, *, data, headers, timeout):
        raise urllib.error.HTTPError(
            url, 401, "Unauthorized", Message(), io.BytesIO(payload)
        )

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setenv("CAIRN_VLM_LOCAL_KEY_FILE", str(key_file))

    with pytest.raises(client.DigitizeHTTPError) as info:
        describe(str(image), provider="local")

    message = str(info.value)
    assert "HTTP 401" in message
    assert KEY not in message
    assert "***" in message


def test_timeout_is_translated(monkeypatch, image, key_file) -> None:
    def fake_post(url, *, data, headers, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setenv("CAIRN_VLM_LOCAL_KEY_FILE", str(key_file))

    with pytest.raises(client.DigitizeTimeoutError, match="秒内没有响应"):
        describe(str(image), provider="local")


def test_connection_error_is_translated(monkeypatch, image, key_file) -> None:
    def fake_post(url, *, data, headers, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setenv("CAIRN_VLM_LOCAL_KEY_FILE", str(key_file))

    with pytest.raises(DigitizeError) as info:
        describe(str(image), provider="local")

    assert "连不上视觉模型服务" in str(info.value)
    assert KEY not in str(info.value)


def test_env_provider_and_endpoint_override(monkeypatch, capture, image) -> None:
    """``CAIRN_VLM_PROVIDER`` / URL / MODEL 都能改选后端。"""
    monkeypatch.setenv(client.ENV_PROVIDER, "cloud")
    monkeypatch.setenv("CAIRN_VLM_CLOUD_URL", "http://10.0.0.9:9000/")
    monkeypatch.setenv("CAIRN_VLM_CLOUD_MODEL", "demo/vlm-small")

    result = describe(str(image))

    assert capture["url"] == "http://10.0.0.9:9000/v1/chat/completions"
    assert _body(capture)["model"] == "demo/vlm-small"
    assert result.provider == "cloud"


def test_image_is_sent_as_data_url_png(capture, image) -> None:
    """图片读成 bytes → base64 → data:image/png。"""
    describe(str(image))

    parts = _body(capture)["messages"][0]["content"]
    image_part = next(p for p in parts if p["type"] == "image_url")
    url = image_part["image_url"]["url"]
    expected = base64.b64encode(PNG).decode("ascii")
    assert url == f"data:image/png;base64,{expected}"
    text_part = next(p for p in parts if p["type"] == "text")
    assert "只输出" in text_part["text"]
    assert "不要编" in text_part["text"]


def test_jpeg_extension_gets_jpeg_mime(tmp_path, capture) -> None:
    path = tmp_path / "q1.JPG"
    path.write_bytes(PNG)

    describe(str(path))

    parts = _body(capture)["messages"][0]["content"]
    image_part = next(p for p in parts if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_missing_image_raises_before_network(monkeypatch, tmp_path, key_file) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("图都没有就不该发请求")

    monkeypatch.setattr(client, "_post", boom)
    monkeypatch.setenv("CAIRN_VLM_LOCAL_KEY_FILE", str(key_file))

    with pytest.raises(DigitizeError, match="读不到题块图"):
        describe(str(tmp_path / "nope.png"), provider="local")


def test_missing_fields_become_empty_strings(monkeypatch, capture, image) -> None:
    """模型没把握时给空串：缺的字段补空串，不是崩。"""
    monkeypatch.setattr(
        client,
        "_post",
        lambda url, *, data, headers, timeout: (
            200,
            _reply('{"stem": "1+1=?", "answer": "", "analysis": ""}'),
        ),
    )

    result = describe(str(image))

    assert (result.stem, result.answer, result.analysis) == ("1+1=?", "", "")


def test_all_errors_share_one_base_class() -> None:
    """调用方只认 ``DigitizeError`` 就能兜住全部失败。"""
    for exc_type in (
        client.DigitizeKeyError,
        client.DigitizeTimeoutError,
        client.DigitizeHTTPError,
        client.DigitizeResponseError,
    ):
        assert issubclass(exc_type, DigitizeError)
