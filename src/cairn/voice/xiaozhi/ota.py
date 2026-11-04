"""小智固件要的 OTA 端点：只回答「把 WebSocket 连到哪儿」。

固件开机 → 连 Wi-Fi → 拿编译进去的 OTA 地址发一次请求 → 从返回 JSON 的
``websocket.url`` 读出服务端地址。这个端点不回任何版本信息，固件就保持当前固件，
不会被一个假版本号拉去升级。

响应形状（固件 ``main/ota.cc`` 的读法，抄自 esp32-voice-entry 的 ``src/ota.py``）：::

    {"websocket": {"url": "ws://…/xiaozhi/ws", "token": "", "version": 1},
     "server_time": {"timestamp": 1750000000000, "timezone_offset": 480}}

``server_time`` 只用来对表（固件用它校正 RTC），不带版本语义。
"""

from __future__ import annotations

import json
import time

# 固件两种写法都出现过：编译进去的常量带尾斜杠，手填的不带。两条都收。
OTA_PATHS = ("/xiaozhi/ota/", "/xiaozhi/ota")

# 固件按这个值决定 WS 用哪套协议；1 = 现在的 JSON + Opus 那一套。
PROTOCOL_VERSION = 1

# 东八区。固件只拿它对表，不影响任何业务判断。
DEFAULT_TZ_OFFSET_MINUTES = 480

# 固件带的身份字段。M3a 不认证，只记一行日志，认不出的头一律忽略。
IDENTITY_HEADERS = ("device-id", "client-id", "user-agent")


def build_payload(
    ws_url: str,
    *,
    now: float | None = None,
    tz_offset_minutes: int = DEFAULT_TZ_OFFSET_MINUTES,
) -> dict:
    """OTA 响应体。没有镜像就不写 ``firmware`` 段。"""
    stamp = int((time.time() if now is None else now) * 1000)
    return {
        "websocket": {"url": ws_url, "token": "", "version": PROTOCOL_VERSION},
        "server_time": {"timestamp": stamp, "timezone_offset": tz_offset_minutes},
    }


def parse_body(body: bytes) -> dict:
    """固件的请求体：正常是 JSON，但不是 JSON 也必须能应答。"""
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def describe(headers: dict[str, str], body: bytes) -> str:
    """给启动日志一行「谁来了」：认不出的字段就当没有，绝不因为缺字段报错。"""
    device = headers.get("device-id") or "-"
    client = headers.get("client-id") or "-"
    application = parse_body(body).get("application")
    if not isinstance(application, dict):
        application = {}
    name = application.get("name") or "-"
    version = application.get("version") or "-"
    return f"device={device} client={client} app={name} v{version}"
