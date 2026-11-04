"""语音转文字：调本机那个 OpenAI 兼容的 ASR 服务（默认 ``127.0.0.1:8040``）。

只用标准库（``urllib`` + ``json``），不引第三方 HTTP 客户端：这一层只有一个请求，
多一个依赖就多一份装不上的可能。

**密钥只从文件读，绝不进代码、不进日志、不进异常消息。** 唯一读取点是
``_read_key``；所有对外消息在拼之前都过一遍 ``_scrub``，把 key 值擦成 ``***``。
``docs/PLAN.md`` 的 M2 卡里 AC6 钉住了这一条。

异常分级（全部继承 ``ASRError``，消息是中文，调用方只需认 ``ASRError``）：

    ASRKeyError         读不到 / 空的密钥文件
    ASRTimeoutError     超时（连接或读响应）
    ASRHTTPError        非 200
    ASRResponseError    返回不是 JSON / 没有 text 字段
    ASREmptyResultError 识别结果为空（音频里没人说话）

网络出口只有 ``_post`` 一个函数，测试整体替换它，不碰真网络。
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8040"
DEFAULT_KEY_FILE = "~/.hermes/keys/qwen_asr.key"
DEFAULT_MODEL = "Qwen/Qwen3-ASR-1.7B"

ENV_URL = "CAIRN_ASR_URL"
ENV_KEY_FILE = "CAIRN_ASR_KEY_FILE"
ENV_MODEL = "CAIRN_ASR_MODEL"
ENV_PROMPT = "CAIRN_ASR_PROMPT"

# 领域提示词：真机实测（2026-10-31）短指令常被 ASR 听成近音字（「停一下」→「听一下」），
# 把孩子最常说的短指令作为 prompt 随请求带给服务端，帮它往这些词上靠。
# 服务端不认识这个字段时请求会失败，``transcribe`` 会自动退化成不带 prompt 再试一次。
DEFAULT_ASR_PROMPT = "下一题 跳过 停一下 给我提示 我会了 继续"

ENDPOINT_PATH = "/v1/audio/transcriptions"
# 本机实测一句 2 秒的话约 1.5 秒返回；留足模型冷启动的余量。
DEFAULT_TIMEOUT = 120.0
# 报错时只带一小段响应体，够定位问题，也不至于把日志刷满。
BODY_SNIPPET = 200


class ASRError(RuntimeError):
    """ASR 调用失败的总类；消息一定是给人看的中文。"""


class ASRKeyError(ASRError):
    """密钥文件读不到或是空的。"""


class ASRTimeoutError(ASRError):
    """在超时时间内没拿到结果。"""


class ASRHTTPError(ASRError):
    """服务返回了非 200。"""


class ASRResponseError(ASRError):
    """返回体不是预期的 JSON 结构。"""


class ASREmptyResultError(ASRError):
    """识别结果为空：这段音频里没有听出文字。"""


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
    """密钥文件 → 去空白的 key。文件不在 / 没权限 / 空文件都给 ``ASRKeyError``。"""
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise ASRKeyError(f"读不到 ASR 密钥文件 {path}（{reason}）。请确认文件存在且可读。") from None
    if not key:
        raise ASRKeyError(f"ASR 密钥文件 {path} 是空的。")
    return key


def _multipart(
    wav_bytes: bytes, model: str, boundary: str, prompt: str = ""
) -> bytes:
    """按 OpenAI 兼容接口拼 multipart：``file`` + ``model``（+ 可选 ``prompt``）。"""
    head: list[bytes] = []
    head.append(f"--{boundary}\r\n".encode())
    head.append(b'Content-Disposition: form-data; name="file"; filename="speech.wav"\r\n')
    head.append(b"Content-Type: audio/wav\r\n\r\n")
    head.append(wav_bytes)
    head.append(b"\r\n")
    head.append(f"--{boundary}\r\n".encode())
    head.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
    head.append(model.encode())
    head.append(b"\r\n")
    if prompt:
        head.append(f"--{boundary}\r\n".encode())
        head.append(b'Content-Disposition: form-data; name="prompt"\r\n\r\n')
        head.append(prompt.encode())
        head.append(b"\r\n")
    head.append(f"--{boundary}--\r\n".encode())
    return b"".join(head)


def _post(url: str, *, data: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
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


def prompt_setting(prompt: str | None = None) -> str:
    """领域提示词的取值：显式参数 > 环境变量 > 默认词表；空白按「没给」处理。

    服务端启动日志（``[asr] prompt=on/off``）也用它判断 on/off。
    """
    raw = prompt if prompt is not None else os.environ.get(ENV_PROMPT)
    if raw is None or not raw.strip():
        return DEFAULT_ASR_PROMPT
    return raw.strip()


def _transcribe_once(
    wav_bytes: bytes,
    *,
    base: str,
    key: str,
    model_name: str,
    timeout: float,
    prompt: str,
) -> str:
    """发一次请求并解析结果；失败抛 ``ASRError`` 子类。prompt 为空则不带该字段。"""
    boundary = f"cairn-{uuid.uuid4().hex}"
    body = _multipart(wav_bytes, model_name, boundary, prompt)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    }
    endpoint = f"{base}{ENDPOINT_PATH}"

    try:
        status, raw = _post(endpoint, data=body, headers=headers, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            detail = _snippet(exc.read(), key)
        except Exception:  # 响应体读不出来也不影响报错本身
            detail = ""
        tail = f"：{detail}" if detail else ""
        raise ASRHTTPError(
            f"ASR 服务返回 HTTP {exc.code}（{exc.reason}）{tail}"
        ) from None
    except (socket.timeout, TimeoutError):
        raise ASRTimeoutError(
            f"ASR 服务 {timeout:g} 秒内没有响应：{endpoint}。检查服务是否卡住。"
        ) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise ASRTimeoutError(
                f"连接 ASR 服务超时：{endpoint}。检查服务是否在跑。"
            ) from None
        raise ASRError(
            f"连不上 ASR 服务 {endpoint}（{exc.reason}）。检查服务是否在跑。"
        ) from None
    except OSError as exc:
        raise ASRError(f"调用 ASR 服务失败（{exc}）。") from None

    if status != 200:
        raise ASRHTTPError(f"ASR 服务返回 HTTP {status}：{endpoint}")

    text_body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        payload = json.loads(text_body)
    except json.JSONDecodeError:
        raise ASRResponseError(
            f"ASR 返回不是合法 JSON（前 {BODY_SNIPPET} 字）：{_snippet(text_body, key)}"
        ) from None

    value = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(value, str):
        raise ASRResponseError(
            f"ASR 返回里没有 text 字段：{_snippet(text_body, key)}"
        )

    spoken = value.strip()
    if not spoken:
        raise ASREmptyResultError("ASR 没听出文字：这段音频里没有人说话，请再说一遍。")
    return spoken


def transcribe(
    wav_bytes: bytes,
    *,
    url: str | None = None,
    key_file: str | None = None,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    prompt: str | None = None,
) -> str:
    """16-bit PCM WAV bytes → 识别出的文字。

    默认带领域提示词（``prompt_setting``）发起请求；服务端若不认识 ``prompt``
    字段而失败，自动退化成**不带 prompt 再试一次**，两次都失败才算失败。
    ``ASRKeyError`` 与 prompt 无关（密钥在读请求前就拿不到），不重试。

    失败一律抛 ``ASRError`` 的子类，消息是中文；**任何消息里都不会出现 key**。
    """
    if not wav_bytes:
        raise ASRError("没有可识别的音频：这段录音是空的。")

    base, key_path, model_name = _settings(url, key_file, model)
    key = _read_key(key_path)
    domain_prompt = prompt_setting(prompt)

    try:
        return _transcribe_once(
            wav_bytes,
            base=base,
            key=key,
            model_name=model_name,
            timeout=timeout,
            prompt=domain_prompt,
        )
    except ASRKeyError:
        raise
    except ASRError:
        if not domain_prompt:
            raise
        # 服务端可能不认识 prompt 字段：退化成不带 prompt 再试一次。
        return _transcribe_once(
            wav_bytes,
            base=base,
            key=key,
            model_name=model_name,
            timeout=timeout,
            prompt="",
        )
