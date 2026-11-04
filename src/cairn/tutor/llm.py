"""讲解用的 LLM 客户端：本机 OpenAI 兼容的 chat 服务（默认 Qwen3-8B）。

约定与 `voice/asr.py` 完全同款（本卡要求"走现有封装、别自己发明"——
仓库里唯一的 LLM/模型服务客户端就是 asr.py 这一套，讲解卡照它的骨架
实现 chat 补全，只用标准库 urllib，网络出口同样只有 ``_post`` 一个函数，
测试整体替换它，不碰真网络）：

- 配置：显式参数 > 环境变量 > 默认值
- 密钥从环境变量或文件读取，绝不进代码、不进日志、不进异常消息
- 异常全部继承 ``LLMError``，消息是中文，调用方只需认 ``LLMError``
- 无网络/无 key/超时都抛错，由 service 层兜住降级到模板文案（不许 500）

service 层只用 :func:`chat`，签名稳定；换模型只动环境变量。
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "Qwen/Qwen3-8B"
DEFAULT_KEY_FILE = "~/.hermes/keys/qwen_tutor.key"
ENDPOINT_PATH = "/v1/chat/completions"
# 讲解一段一两百字，本机 8B 实测量级是秒级；留足冷启动余量即可。
DEFAULT_TIMEOUT = 120.0
BODY_SNIPPET = 200

ENV_URL = "CAIRN_TUTOR_LLM_URL"
ENV_KEY_FILE = "CAIRN_TUTOR_LLM_KEY_FILE"
ENV_API_KEY = "CAIRN_TUTOR_LLM_API_KEY"
ENV_THINKING = "CAIRN_TUTOR_LLM_THINKING"
ENV_MODEL = "CAIRN_TUTOR_LLM_MODEL"

MAX_TOKENS = 300


class LLMError(RuntimeError):
    """LLM 调用失败的总类；消息一定是给人看的中文。"""


class LLMKeyError(LLMError):
    """密钥文件读不到或是空的。"""


class LLMTimeoutError(LLMError):
    """在超时时间内没拿到结果。"""


class LLMHTTPError(LLMError):
    """服务返回了非 200。"""


class LLMResponseError(LLMError):
    """返回体不是预期的 JSON 结构。"""


def _scrub(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def _snippet(raw: object, secret: str) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = str(raw)
    return _scrub(text[:BODY_SNIPPET], secret)


def _read_key(path: Path) -> str:
    """密钥文件 → 去空白的 key。文件不在/没权限/空文件都给 ``LLMKeyError``。"""
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise LLMKeyError(
            f"读不到 LLM 密钥文件 {path}（{reason}）。请确认文件存在且可读。"
        ) from None
    if not key:
        raise LLMKeyError(f"LLM 密钥文件 {path} 是空的。")
    return key


def _post(
    url: str, *, data: bytes, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    """唯一的网络出口。非 2xx 会抛 ``urllib.error.HTTPError``，由调用方翻译。"""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.read()


def _settings(
    url: str | None, key_file: str | None, model: str | None
) -> tuple[str, Path, str]:
    """显式参数 > 环境变量 > 默认值。空字符串按「没给」处理。"""
    base = (url or os.environ.get(ENV_URL) or DEFAULT_URL).rstrip("/")
    key_hint = key_file or os.environ.get(ENV_KEY_FILE) or DEFAULT_KEY_FILE
    name = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
    return base, Path(os.path.expanduser(key_hint)), name


def chat(
    messages: list[dict[str, str]],
    *,
    url: str | None = None,
    key_file: str | None = None,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """OpenAI 兼容 ``/v1/chat/completions``：messages → 首个 choice 的文本。

    失败一律抛 ``LLMError`` 子类；**任何消息里都不会出现 key**。
    显式 key_file 优先，其次是环境变量，最后是配置的密钥文件。没有可用密钥
    时按本地无鉴权服务处理，不带 Authorization 头直接发。
    """
    base, key_path, model_name = _settings(url, key_file, model)

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = "" if key_file else os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        try:
            key = _read_key(key_path)
        except LLMKeyError:
            pass  # 无 key 可用：本地服务不强制鉴权，裸发。
    if "\r" in key or "\n" in key:
        raise LLMKeyError("LLM 密钥格式无效")
    if key:
        headers["Authorization"] = f"Bearer {key}"

    request_body = {"model": model_name, "messages": messages, "max_tokens": MAX_TOKENS}
    thinking = os.environ.get(ENV_THINKING, "").strip().lower()
    if thinking:
        if thinking not in {"enabled", "disabled"}:
            raise LLMError("LLM thinking 配置无效")
        request_body["thinking"] = {"type": thinking}
    body = json.dumps(request_body).encode("utf-8")
    endpoint = f"{base}{ENDPOINT_PATH}"

    try:
        status, raw = _post(endpoint, data=body, headers=headers, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            detail = _snippet(exc.read(), key)
        except Exception:  # 响应体读不出来也不影响报错本身
            detail = ""
        tail = f"：{detail}" if detail else ""
        raise LLMHTTPError(
            f"LLM 服务返回 HTTP {exc.code}（{_scrub(str(exc.reason), key)}）{tail}"
        ) from None
    except (socket.timeout, TimeoutError):
        raise LLMTimeoutError(
            f"LLM 服务 {timeout:g} 秒内没有响应：{_scrub(endpoint, key)}。检查服务是否卡住。"
        ) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise LLMTimeoutError(
                f"连接 LLM 服务超时：{_scrub(endpoint, key)}。检查服务是否在跑。"
            ) from None
        raise LLMError(
            f"连不上 LLM 服务 {_scrub(endpoint, key)}（{_scrub(str(exc.reason), key)}）。检查服务是否在跑。"
        ) from None
    except OSError as exc:
        raise LLMError(f"调用 LLM 服务失败（{_scrub(str(exc), key)}）。") from None

    if status != 200:
        raise LLMHTTPError(f"LLM 服务返回 HTTP {status}：{_scrub(endpoint, key)}")

    text_body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        payload = json.loads(text_body)
    except json.JSONDecodeError:
        raise LLMResponseError(
            f"LLM 返回不是合法 JSON（前 {BODY_SNIPPET} 字）：{_snippet(text_body, key)}"
        ) from None

    value = None
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    value = message.get("content")
    if not isinstance(value, str) or not value.strip():
        raise LLMResponseError(
            f"LLM 返回里没有 choices[0].message.content：{_snippet(text_body, key)}"
        )
    return value.strip()
