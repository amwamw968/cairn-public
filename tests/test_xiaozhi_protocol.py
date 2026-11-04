"""M3a：用假设备客户端跑 OTA → hello → listen，全程不碰真板子。

「假设备」自己写握手与掩码帧（不依赖 ``websockets`` 之类库），所以这份测试同时也是
「这套线协议能被第三方客户端用」的最小证据：只要 RFC 6455 的客户端能连，板子就能连。

不碰 4411 / 4412 真实端口：服务起在 127.0.0.1 的随机端口上，跑完即关。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest

from cairn.voice.xiaozhi import wire
from cairn.voice.xiaozhi import server as server_module
from cairn.voice.xiaozhi.server import WS_PATH, XiaozhiServer
from cairn.voice.xiaozhi.ws import DOWNLINK_PARAMS, Conversation

WS_URL = "ws://192.168.1.27:4412/xiaozhi/ws"
TIMEOUT = 2.0


# --- 测试脚手架：假设备与裸 HTTP ---------------------------------------------


def run_with_server(scenario, *, ws_url: str = WS_URL, log=None, request_timeout=None):
    """起一台只在环回上听的服务，跑完 scenario 就关。"""

    async def main():
        extra = {} if request_timeout is None else {"request_timeout": request_timeout}
        server = XiaozhiServer(
            host="127.0.0.1",
            port=0,
            ws_url=ws_url,
            log=log or (lambda *_: None),
            **extra,
        )
        await server.start()
        try:
            return await scenario(server)
        finally:
            await server.close()

    return asyncio.run(main())


def masked_frame(opcode: int, payload: bytes = b"") -> bytes:
    """客户端帧（必须掩码）。"""
    mask = os.urandom(4)
    body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    head = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        head.append(0x80 | length)
    elif length < 0x10000:
        head.append(0x80 | 126)
        head += length.to_bytes(2, "big")
    else:
        head.append(0x80 | 127)
        head += length.to_bytes(8, "big")
    return bytes(head) + mask + body


async def read_server_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """读服务端帧（不掩码）。"""
    first, second = await reader.readexactly(2)
    assert not second & 0x80, "服务端帧不应掩码"
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    return opcode, await reader.readexactly(length)


class FakeDevice:
    """假设备：握手 + 收发 JSON，行为贴近小智固件（带 Device-Id 等头）。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(cls, port: int) -> "FakeDevice":
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {WS_PATH} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Device-Id: 80b54ecd0f64\r\n"
            "Client-Id: fake-device\r\n"
            "User-Agent: xiaozhi-fake/1.0\r\n"
            "\r\n"
        )
        writer.write(request.encode("latin-1"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        status = head.split(b"\r\n")[0]
        assert b"101" in status, f"握手失败：{status!r}"
        return cls(reader, writer)

    async def send_json(self, payload: dict) -> None:
        await self.send_frame(wire.OP_TEXT, json.dumps(payload).encode("utf-8"))

    async def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        self._writer.write(masked_frame(opcode, payload))
        await self._writer.drain()

    async def recv_json(self) -> dict:
        opcode, payload = await asyncio.wait_for(read_server_frame(self._reader), TIMEOUT)
        assert opcode == wire.OP_TEXT, f"期望文本帧，收到操作码 {opcode:#x}"
        return json.loads(payload)

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except ConnectionError:
            pass


async def http_request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict]:
    """裸 HTTP：服务端回完就关连接，所以读到 EOF 就是完整响应。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", f"Content-Length: {len(body)}"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), TIMEOUT)
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionError:
        pass

    head, _, payload = raw.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split(" ")[1])
    parsed_headers = {}
    for line in head_lines[1:]:
        name, _, value = line.partition(":")
        if name:
            parsed_headers[name.strip().lower()] = value.strip()
    return status, parsed_headers, json.loads(payload) if payload else {}


async def send_raw(
    port: int, data: bytes, *, half_close: bool = False
) -> tuple[int, dict[str, str], bytes]:
    """逐字发一段字节（板子抓包的原文），返回状态码 / 头 / 响应体。

    收不到任何字节就直接断言失败——「静默断开」必须是一眼能看到的红，而不是空 dict。
    ``half_close`` 模拟「客户端发完就关写端」：服务端会读到 EOF，而不是干等超时。
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    if half_close:
        writer.write_eof()
    raw = await asyncio.wait_for(reader.read(), TIMEOUT)
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionError:
        pass

    assert raw, "服务端一个字节都没回（静默断开）"
    head, _, payload = raw.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split(" ")[1])
    parsed_headers = {}
    for line in head_lines[1:]:
        name, _, value = line.partition(":")
        if name:
            parsed_headers[name.strip().lower()] = value.strip()
    return status, parsed_headers, payload


# --- OTA：GET / POST 都回 websocket.url --------------------------------------


DEVICE_HEADERS = {
    "Device-Id": "80b54ecd0f64",
    "Client-Id": "fake-device",
    "User-Agent": "xiaozhi-fake/1.0",
    "Content-Type": "application/json",
}


def test_ota_post_returns_websocket_url():
    body = json.dumps(
        {
            "application": {"name": "xiaozhi", "version": "1.0.0"},
            "board": {"type": "chd-esp32s3-box"},
            "chip_model_name": "esp32s3",
        }
    ).encode("utf-8")

    async def scenario(server):
        return await http_request(
            server.port, "POST", "/xiaozhi/ota/", body=body, headers=DEVICE_HEADERS
        )

    status, _headers, payload = run_with_server(scenario)

    assert status == 200
    assert payload["websocket"]["url"] == WS_URL
    assert payload["websocket"]["token"] == ""
    # 不伪造版本号：没有镜像就不写 firmware 段，固件不会去升级。
    assert "firmware" not in payload
    assert "server_time" in payload


@pytest.mark.parametrize("path", ["/xiaozhi/ota/", "/xiaozhi/ota"])
def test_ota_get_answers_both_paths(path):
    async def scenario(server):
        return await http_request(server.port, "GET", path)

    status, _headers, payload = run_with_server(scenario)

    assert status == 200
    assert payload["websocket"]["url"] == WS_URL


def test_ota_tolerates_unparsable_body():
    async def scenario(server):
        return await http_request(
            server.port, "POST", "/xiaozhi/ota/", body=b"not json at all"
        )

    status, _headers, payload = run_with_server(scenario)

    assert status == 200
    assert payload["websocket"]["url"] == WS_URL


def test_ota_unknown_path_is_404():
    async def scenario(server):
        return await http_request(server.port, "GET", "/nope")

    status, _headers, payload = run_with_server(scenario)

    assert status == 404
    assert payload["error"] == "not found"


def test_ota_url_override_is_what_the_device_gets():
    """--ws-url 覆盖：返回给固件的地址就是传进来的那一个。"""
    custom = "ws://10.0.0.9:9000/xiaozhi/ws"

    async def scenario(server):
        return await http_request(server.port, "GET", "/xiaozhi/ota/")

    status, _headers, payload = run_with_server(scenario, ws_url=custom)

    assert status == 200
    assert payload["websocket"]["url"] == custom


def test_default_ws_url_uses_lan_ip(monkeypatch):
    """不传 --ws-url 时用「本机地址 + 端口 + /xiaozhi/ws」，不是 127.0.0.1。"""
    monkeypatch.setattr(server_module, "local_ip", lambda: "192.168.1.27")

    assert XiaozhiServer(port=4412).ws_url == "ws://192.168.1.27:4412/xiaozhi/ws"


# --- 真板子抓包的两条请求（逐字照抄，不再只有我们自造的「理想请求」） ----------


def board_ota_body() -> bytes:
    """真板子那次 POST 的 body：JSON 且恰好 1153 字节（抓包里的 Content-Length）。"""
    body = {
        "version": 2,
        "flash_size": 16777216,
        "minimum_free_heap_size": 123456,
        "mac_address": "80:b5:4e:cd:0f:64",
        "chip_model_name": "esp32s3",
        "application": {"name": "chd-esp32s3-box-lcd35", "version": "2.5.3"},
        "board": {"type": "chd-esp32s3-box-lcd35", "name": "chd-esp32s3-box-lcd35"},
        "pad": "",
    }
    body["pad"] = "x" * (1153 - len(json.dumps(body).encode("utf-8")))
    raw = json.dumps(body).encode("utf-8")
    assert len(raw) == 1153
    return raw


def board_ota_request(body: bytes | None = None, *, declared: int | None = None) -> bytes:
    """板子的 OTA 请求原文：头的名字与顺序都一样，带 Connection: close。"""
    payload = board_ota_body() if body is None else body
    length = len(board_ota_body()) if declared is None else declared
    head = (
        "POST /xiaozhi/ota/ HTTP/1.1\r\n"
        "Host: 192.168.1.27:4411\r\n"
        "Accept-Language: zh-CN\r\n"
        "Activation-Version: 1\r\n"
        "Client-Id: 74676794-0000-0000-0000-000000000000\r\n"
        "Content-Type: application/json\r\n"
        "Device-Id: 80:b5:4e:cd:0f:64\r\n"
        "User-Agent: chd-esp32s3-box-lcd35/2.5.3\r\n"
        f"Content-Length: {length}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return head.encode("latin-1") + payload


BOARD_MUTE_REQUEST = (
    "GET /debug/mute HTTP/1.1\r\n"
    "Host: 192.168.1.27:4411\r\n"
    "Accept: application/json\r\n"
    "Authorization: Bearer test-control-token\r\n"
    "Content-Type: application/json\r\n"
    "Connection: close\r\n"
    "\r\n"
).encode("latin-1")


def test_board_ota_request_1153_bytes_returns_websocket_url():
    """真板子那条：1153 字节体 + Connection: close + Activation-Version/Client-Id → 200。"""

    async def scenario(server):
        return await send_raw(server.port, board_ota_request())

    status, headers, payload = run_with_server(scenario)
    data = json.loads(payload)

    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert data["websocket"]["url"] == WS_URL
    assert data["websocket"]["token"] == ""
    assert data["websocket"]["version"] == 1
    assert "firmware" not in data


def test_board_mute_request_matches_reference_shape():
    """GET /debug/mute：键逐字对齐参考实现，M3a 固定「未静音」。"""

    async def scenario(server):
        return await send_raw(server.port, BOARD_MUTE_REQUEST)

    status, headers, payload = run_with_server(scenario)
    data = json.loads(payload)

    assert status == 200
    assert headers["content-type"].startswith("application/json")
    # 参考 src/vision.py:496-498 + src/mute.py:257-275 + src/vision.py:104-110
    assert set(data) == {
        "ok",
        "muted",
        "since",
        "changed_by",
        "updated_at",
        "mode",
        "mode_note",
        "counters",
        "last_frame_at",
        "previous",
        "persisted_path",
        "catchup",
    }
    assert data["ok"] is True
    assert data["muted"] is False
    assert data["mode"] == "A"
    assert data["mode_note"] == "服务端丢帧；设备仍在采集并上传，B2 后才是设备停采"
    assert data["counters"] == {
        "frames_in": 0,
        "frames_dropped": 0,
        "frames_accepted": 0,
        "turns": 0,
    }
    assert set(data["catchup"]) == {
        "pending",
        "missed",
        "spoken",
        "last_at",
        "running",
        "last_result",
    }


@pytest.mark.parametrize("path", ["/debug/mute", "/debug/mute/"])
def test_mute_answers_both_path_variants(path):
    request = BOARD_MUTE_REQUEST.replace(b"/debug/mute ", path.encode() + b" ", 1)

    async def scenario(server):
        return await send_raw(server.port, request)

    status, _headers, payload = run_with_server(scenario)

    assert status == 200
    assert json.loads(payload)["muted"] is False


def test_mute_rejects_post_with_json_405():
    request = BOARD_MUTE_REQUEST.replace(b"GET /debug/mute", b"POST /debug/mute", 1)

    async def scenario(server):
        return await send_raw(server.port, request)

    status, _headers, payload = run_with_server(scenario)

    assert status == 405
    assert json.loads(payload)["error"] == "method not allowed"


def test_unknown_path_returns_404_json_not_a_dropped_connection():
    async def scenario(server):
        return await send_raw(
            server.port, b"GET /debug/nope HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        )

    status, headers, payload = run_with_server(scenario)

    assert status == 404
    assert headers["content-type"].startswith("application/json")
    assert json.loads(payload)["error"] == "not found"


def test_handler_exception_returns_500_json_and_logs(monkeypatch):
    """OTA 处理函数内部炸了 → 500 + JSON，日志里有异常类型；不是静默断开。"""
    logs: list[str] = []

    def boom(*_args, **_kwargs):
        raise RuntimeError("payload 造不出来")

    monkeypatch.setattr(server_module.ota, "build_payload", boom)

    async def scenario(server):
        return await send_raw(server.port, board_ota_request())

    status, headers, payload = run_with_server(scenario, log=logs.append)

    assert status == 500
    assert headers["content-type"].startswith("application/json")
    assert json.loads(payload)["error"] == "internal server error"
    assert any("RuntimeError" in line for line in logs), logs


def test_ota_answers_when_body_is_shorter_than_declared():
    """体没发全（板子半路停住）：等一小会儿照样回 200，不静默断开。"""
    full = board_ota_body()

    async def scenario(server):
        return await send_raw(server.port, board_ota_request(full[: len(full) // 2]))

    status, _headers, payload = run_with_server(scenario, request_timeout=0.2)

    assert status == 200
    assert json.loads(payload)["websocket"]["url"] == WS_URL


def test_ota_answers_when_peer_half_closes_mid_body():
    """对端发一半就关写端：立刻回 200，不要等到超时。"""
    full = board_ota_body()

    async def scenario(server):
        return await send_raw(
            server.port, board_ota_request(full[: len(full) // 2]), half_close=True
        )

    status, _headers, payload = run_with_server(scenario, request_timeout=1.0)

    assert status == 200
    assert json.loads(payload)["websocket"]["url"] == WS_URL


def test_ota_answers_when_declared_body_is_huge():
    """Content-Length 远超上限：不读成无底洞，也不关连接，照样回 200。"""
    full = board_ota_body()
    padded = full[:-2] + b',' + b'"pad2": "' + b"y" * (300 * 1024) + b'"}'

    async def scenario(server):
        return await send_raw(server.port, board_ota_request(padded, declared=len(padded)))

    status, _headers, payload = run_with_server(scenario, request_timeout=1.0)

    assert status == 200
    assert json.loads(payload)["websocket"]["url"] == WS_URL


def test_leading_blank_line_before_request_line_is_tolerated():
    async def scenario(server):
        return await send_raw(server.port, b"\r\n" + board_ota_request())

    status, _headers, payload = run_with_server(scenario)

    assert status == 200
    assert json.loads(payload)["websocket"]["url"] == WS_URL


def test_broken_request_line_gets_400_json():
    """请求行都读不懂：回 400 JSON（板子能看到状态码），而不是直接关。"""

    async def scenario(server):
        return await send_raw(server.port, b"HELLO\r\n\r\n")

    status, headers, payload = run_with_server(scenario)

    assert status == 400
    assert headers["content-type"].startswith("application/json")
    assert json.loads(payload)["error"] == "bad request"


# --- WebSocket：hello / listen / abort ---------------------------------------


def test_hello_handshake_returns_audio_params():
    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "features": {"mcp": True},
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
            return await device.recv_json()
        finally:
            await device.close()

    reply = run_with_server(scenario)

    assert reply["type"] == "hello"
    assert reply["transport"] == "websocket"
    assert reply["audio_params"] == DOWNLINK_PARAMS
    assert reply["session_id"]


def test_listen_start_opens_window_without_speaking():
    """M3b：开始听的时候不说话——话要等 ``listen stop`` 之后由回合给。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json({"type": "listen", "state": "start", "mode": "manual"})
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(device.recv_json(), 0.3)
            # 连接没坏：随后一条 hello 照样有回应
            await device.send_json({"type": "hello"})
            return await device.recv_json()
        finally:
            await device.close()

    assert run_with_server(scenario)["type"] == "hello"


def test_listen_detect_also_opens_window():
    """唤醒词唤醒走 detect（不是 start），同样只是开窗口。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json({"type": "listen", "state": "detect"})
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(device.recv_json(), 0.3)
            await device.send_json({"type": "hello"})
            return await device.recv_json()
        finally:
            await device.close()

    assert run_with_server(scenario)["type"] == "hello"


def test_abort_does_not_crash_and_connection_survives():
    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_json({"type": "abort", "reason": "wake_word_detected"})
            await device.send_json({"type": "listen", "state": "start"})
            await device.send_json({"type": "abort", "reason": "none"})
            await device.send_json({"type": "hello"})
            return await device.recv_json()
        finally:
            await device.close()

    assert run_with_server(scenario)["type"] == "hello"


def test_binary_audio_outside_window_keeps_connection():
    """窗口外的二进制帧只计数（板子的 VAD 有时会在 stop 后再送尾巴）。"""

    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_frame(wire.OP_BINARY, b"\xf8\xff\xfe" * 40)
            await device.send_json({"type": "hello"})
            return await device.recv_json()
        finally:
            await device.close()

    assert run_with_server(scenario)["type"] == "hello"


def test_ping_is_answered_with_pong():
    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_frame(wire.OP_PING, b"keepalive")
            return await asyncio.wait_for(read_server_frame(device._reader), TIMEOUT)
        finally:
            await device.close()

    opcode, payload = run_with_server(scenario)

    assert opcode == wire.OP_PONG
    assert payload == b"keepalive"


def test_close_frame_is_echoed_and_connection_ends():
    async def scenario(server):
        device = await FakeDevice.connect(server.port)
        try:
            await device.send_frame(wire.OP_CLOSE, (1000).to_bytes(2, "big"))
            opcode, _payload = await asyncio.wait_for(
                read_server_frame(device._reader), TIMEOUT
            )
            tail = await asyncio.wait_for(device._reader.read(), TIMEOUT)
            return opcode, tail
        finally:
            await device.close()

    opcode, tail = run_with_server(scenario)

    assert opcode == wire.OP_CLOSE
    assert tail == b""


# --- 并发：只放一个连接 -------------------------------------------------------


def test_second_connection_rejected_while_first_is_open():
    async def scenario(server):
        first = await FakeDevice.connect(server.port)
        try:
            status, _headers, payload = await http_request(
                server.port,
                "GET",
                WS_PATH,
                headers={
                    "Upgrade": "websocket",
                    "Connection": "Upgrade",
                    "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(),
                    "Sec-WebSocket-Version": "13",
                },
            )
            # 第一个连接不受影响
            await first.send_json({"type": "hello"})
            hello = await first.recv_json()
            return status, payload, hello
        finally:
            await first.close()

    status, payload, hello = run_with_server(scenario)

    assert status == 503
    assert payload["error"] == "busy"
    assert hello["type"] == "hello"


def test_connection_slot_is_released_after_disconnect():
    async def scenario(server):
        first = await FakeDevice.connect(server.port)
        await first.close()
        await asyncio.sleep(0.05)  # 让服务端把那条连接收尾
        second = await FakeDevice.connect(server.port)
        try:
            await second.send_json({"type": "hello"})
            return await second.recv_json()
        finally:
            await second.close()

    assert run_with_server(scenario)["type"] == "hello"


# --- 状态机单测：窗口口径 ----------------------------------------------------


def test_conversation_window_opens_and_closes_without_talking():
    session = Conversation("s1")

    assert session.on_text('{"type":"listen","state":"detect"}') == []
    assert session.listening is True
    # 同一窗口里的 start 只是重复开窗，不回话
    assert session.on_text('{"type":"listen","state":"start"}') == []
    assert session.listening is True
    assert session.on_text('{"type":"listen","state":"stop"}') == []
    assert session.listening is False


def test_conversation_ignores_broken_json_and_unknown_types():
    session = Conversation("s1")

    assert session.on_text("not json") == []
    assert session.on_text('{"type":"mcp","payload":{}}') == []
    assert session.on_text("[]") == []
    assert session.on_text('{"type":"abort"}') == []
    assert session.audio_bytes == 0
    session.on_audio(b"\x00" * 12)
    assert session.audio_bytes == 12
    assert session.stray_packets == 1, "窗口外的包只计数"
