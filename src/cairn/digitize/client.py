"""复核用的模型适配层：一张题块图 → 题干 / 答案 / 解析。

两个后端走同一套 OpenAI 兼容 chat 协议，只换 host / model / key 文件：

    cloud（默认）  https://api.siliconflow.cn  Qwen/Qwen3-VL-32B-Instruct  ~/.hermes/keys/siliconflow.key
    local          http://127.0.0.1:9981   Qwen3-VL-4B-Instruct-4bit  ~/.hermes/keys/omlx.key

默认云端；本机 oMLX 作为可选后端，``provider="local"`` 或 ``CAIRN_VLM_PROVIDER=local`` 切回去。

骨架照抄 ``tutor/llm.py`` 与 ``voice/asr.py``：只用标准库 ``urllib``，网络出口只有
``_post`` 一个函数（测试整体替换它，不碰真网络），密钥只从文件读、绝不进代码、
不进日志、不进异常消息，所有异常继承 ``DigitizeError``，消息是中文给人看的。

对外只有 :func:`describe`。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROVIDERS = ("local", "cloud")
DEFAULT_PROVIDER = "cloud"

ENDPOINT_PATH = "/v1/chat/completions"
MAX_TOKENS = 600
BODY_SNIPPET = 200

ENV_PROVIDER = "CAIRN_VLM_PROVIDER"

# provider → (默认 base url, 默认 model, 默认 key 文件, 超时秒, 三个环境变量名)
_BACKENDS: dict[str, tuple[str, str, str, float, str, str, str]] = {
    "local": (
        "http://127.0.0.1:9981",
        "Qwen3-VL-4B-Instruct-4bit",
        "~/.hermes/keys/omlx.key",
        60.0,
        "CAIRN_VLM_LOCAL_URL",
        "CAIRN_VLM_LOCAL_MODEL",
        "CAIRN_VLM_LOCAL_KEY_FILE",
    ),
    "cloud": (
        "https://api.siliconflow.cn",
        "Qwen/Qwen3-VL-32B-Instruct",
        "~/.hermes/keys/siliconflow.key",
        90.0,
        "CAIRN_VLM_CLOUD_URL",
        "CAIRN_VLM_CLOUD_MODEL",
        "CAIRN_VLM_CLOUD_KEY_FILE",
    ),
}

PROMPT = (
    "你是小学作业批改助手。看这张图里的一道题，只输出一个 JSON 对象，不要输出别的：\n"
    '{"stem": "题干原文", "answer": "这道题的答案", "analysis": "一句话解析"}\n'
    "看不清或没把握的字段就给空字符串，不要编。"
)


class DigitizeError(RuntimeError):
    """识别/调用失败的总类；消息一定是给人看的中文。"""


class DigitizeKeyError(DigitizeError):
    """密钥文件读不到或是空的。"""


class DigitizeTimeoutError(DigitizeError):
    """在超时时间内没拿到结果。"""


class DigitizeHTTPError(DigitizeError):
    """服务返回了非 200。"""


class DigitizeResponseError(DigitizeError):
    """返回体不是预期的结构，或里面抠不出 JSON。"""


@dataclass(frozen=True)
class Digitized:
    """一张题块图的三字段，外加走了哪个后端、模型名和往返毫秒。"""

    stem: str
    answer: str
    analysis: str
    provider: str
    model: str
    ms: int


@dataclass(frozen=True)
class _Backend:
    name: str
    url: str
    model: str
    key_file: Path
    timeout: float


def _scrub(text: str, secret: str) -> str:
    """把密钥从要展示的文本里抹掉；secret 为空（还没读到）时原样返回。"""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def _snippet(raw: object, secret: str) -> str:
    """响应体的前若干字，已擦掉密钥。"""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = str(raw)
    return _scrub(text[:BODY_SNIPPET], secret)


def _read_key(path: Path) -> str:
    """密钥文件 → 去空白的 key。文件不在 / 没权限 / 空文件都给 ``DigitizeKeyError``。"""
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise DigitizeKeyError(
            f"读不到视觉模型密钥文件 {path}（{reason}）。请确认文件存在且可读。"
        ) from None
    if not key:
        raise DigitizeKeyError(f"视觉模型密钥文件 {path} 是空的。")
    return key


def _post(
    url: str, *, data: bytes, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    """唯一的网络出口。非 2xx 会抛 ``urllib.error.HTTPError``，由调用方翻译。"""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.read()


def _resolve_provider(provider: str | None) -> str:
    """显式参数 > 环境变量 > 默认值；只认 local / cloud，别的值直接抛错。"""
    raw = provider or os.environ.get(ENV_PROVIDER) or DEFAULT_PROVIDER
    name = raw.strip()
    if name not in _BACKENDS:
        raise DigitizeError(
            f"不认识的 provider「{name}」，只能填 {' 或 '.join(PROVIDERS)}。"
        )
    return name


def _backend(name: str) -> _Backend:
    """环境变量 > 默认值（显式参数只有 provider 一个，已在 ``_resolve_provider`` 处理）。"""
    url, model, key_file, timeout, env_url, env_model, env_key_file = _BACKENDS[name]
    base = (os.environ.get(env_url) or url).rstrip("/")
    model_name = os.environ.get(env_model) or model
    key_hint = os.environ.get(env_key_file) or key_file
    return _Backend(
        name=name,
        url=base,
        model=model_name,
        key_file=Path(os.path.expanduser(key_hint)),
        timeout=timeout,
    )


def _media_type(image_path: str) -> str:
    suffix = Path(image_path).suffix.lower()
    return "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"


def _data_url(image_path: str) -> str:
    """图片文件 → ``data:image/png;base64,...``。读不到 / 空文件都抛中文错。"""
    try:
        raw = Path(image_path).read_bytes()
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise DigitizeError(f"读不到题块图 {image_path}（{reason}）。") from None
    if not raw:
        raise DigitizeError(f"题块图 {image_path} 是空的，没有可识别的内容。")
    blob = base64.b64encode(raw).decode("ascii")
    return f"data:{_media_type(image_path)};base64,{blob}"


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个带三字段的 JSON 对象；抠不出返回 None。

    容忍 ```json 围栏与前后的废话：逐个尝试成对的 ``{...}`` 片段。
    """
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    candidates: list[str] = []
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start : index + 1])
                start = None

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and any(
            field in payload for field in ("stem", "answer", "analysis")
        ):
            return payload
    return None


def _fields(payload: dict) -> tuple[str, str, str]:
    values = []
    for field in ("stem", "answer", "analysis"):
        value = payload.get(field, "")
        values.append("" if value is None else str(value).strip())
    return values[0], values[1], values[2]


def describe(image_path: str, *, provider: str | None = None) -> Digitized:
    """把一张题块图变成 题干 / 答案 / 解析。失败抛 ``DigitizeError``。

    默认走云端 32B；``provider="local"`` 切回本机 oMLX 的 4B。
    **任何消息里都不会出现 key**。
    """
    name = _resolve_provider(provider)
    backend = _backend(name)
    key = _read_key(backend.key_file)
    image = _data_url(image_path)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
    }
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        }
    ]
    body = json.dumps(
        {
            "model": backend.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
        }
    ).encode("utf-8")
    endpoint = f"{backend.url}{ENDPOINT_PATH}"

    started = time.monotonic()
    try:
        status, raw = _post(
            endpoint, data=body, headers=headers, timeout=backend.timeout
        )
    except urllib.error.HTTPError as exc:
        try:
            detail = _snippet(exc.read(), key)
        except Exception:  # 响应体读不出来也不影响报错本身
            detail = ""
        tail = f"：{detail}" if detail else ""
        raise DigitizeHTTPError(
            f"视觉模型服务返回 HTTP {exc.code}（{exc.reason}）{tail}"
        ) from None
    except (socket.timeout, TimeoutError):
        raise DigitizeTimeoutError(
            f"视觉模型服务 {backend.timeout:g} 秒内没有响应：{endpoint}。检查服务是否卡住。"
        ) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise DigitizeTimeoutError(
                f"连视觉模型服务超时：{endpoint}。检查服务是否在跑。"
            ) from None
        raise DigitizeError(
            f"连不上视觉模型服务 {endpoint}（{exc.reason}）。检查服务是否在跑。"
        ) from None
    except OSError as exc:
        raise DigitizeError(f"调用视觉模型服务失败（{exc}）。") from None
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if status != 200:
        raise DigitizeHTTPError(f"视觉模型服务返回 HTTP {status}：{endpoint}")

    text_body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        payload = json.loads(text_body)
    except json.JSONDecodeError:
        raise DigitizeResponseError(
            f"视觉模型返回不是合法 JSON（前 {BODY_SNIPPET} 字）：{_snippet(text_body, key)}"
        ) from None

    content = None
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise DigitizeResponseError(
            f"视觉模型返回里没有 choices[0].message.content：{_snippet(text_body, key)}"
        )

    fields = _extract_json(content)
    if fields is None:
        raise DigitizeResponseError(
            f"视觉模型没有给出可解析的 JSON（前 {BODY_SNIPPET} 字）："
            f"{_snippet(content, key)}"
        )
    stem, answer, analysis = _fields(fields)
    return Digitized(
        stem=stem,
        answer=answer,
        analysis=analysis,
        provider=backend.name,
        model=backend.model,
        ms=elapsed_ms,
    )
