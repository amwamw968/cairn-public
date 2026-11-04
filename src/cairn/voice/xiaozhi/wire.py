"""最小 HTTP/1.1 + WebSocket（RFC 6455）线协议，只用标准库。

**为什么手写**：Cairn 的依赖里没有 WS 后端（``websockets`` / ``wsproto`` 都没装），
uvicorn 收到 ``Upgrade`` 只会把连接关掉，板子根本连不上；而 M3a 的硬约束是
不引入新依赖。于是这里只实现小智固件真会用到的子集：

- HTTP：一条连接一个请求，``Content-Length`` 定长体，响应后即关（``Connection: close``）；
  **请求读坏了也要回一句话**：头错 → ``WireError`` 带状态码，体错 → ``Request.note``
  记原因但照常应答。一个字节都不回，板子只会记「Connection closed before response
  headers were received」，现场无法分诊。
- WS：握手（含子协议回显）、文本帧、二进制帧、分片续帧、ping/pong、close，
  并严格校验「客户端帧必须掩码」。

**这是已知债务**：M3b 要处理音频流（长连接、大帧、背压）时，优先换 ``websockets``
依赖，而不是继续加码这个手写实现。见 ``docs/PLAN.md`` 的「语音 M3a · 关键设计」。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

# 固件是嵌在 4MB 内存里的客户端：帧都很小，给个上限是为了坏包别把服务打爆。
MAX_HEADER_BYTES = 16 * 1024
# 存给业务的上限。OTA 只读 ``application`` 几个字段，256K 已经是实际报文的 200 倍；
# 超过的部分不存，但**仍然把连接上的字节读掉**（见 MAX_BODY_READ），
# 否则带着未读数据 close 会发 RST，把已经写好的响应一起丢掉。
MAX_BODY_BYTES = 256 * 1024
# 从连接上最多读走这么多。再大的 body 直接不读完就应答：固件不可能发这么大，
# 而「读到一半等对端」比「先回一句话」更糟。
MAX_BODY_READ = 4 * 1024 * 1024
MAX_FRAME_BYTES = 256 * 1024

_CHUNK = 64 * 1024
# 请求行之前允许的空行数。RFC 7230 §3.5 要求至少容忍一个；容忍若干个，
# 但要有上限，免得对端一直发 CRLF 把连接吊住。
_MAX_BLANK_LINES = 8

# RFC 6455 固定的魔法串：Sec-WebSocket-Accept = base64(sha1(key + GUID))。
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

REASONS = {
    101: "Switching Protocols",
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class WireError(Exception):
    """线协议不合法。

    带上 ``status``：调用方要**先回一句**再关连接。板子的日志里
    ``Connection closed before response headers were received`` 就是这么来的——
    一个字节都不回，固件只能报「连上又断」，现场无法判断是头坏了还是体坏了。
    """

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


class Request:
    """一条 HTTP 请求。头名统一小写，``path`` 已去掉查询串。

    ``note`` 是「体没读全」的原因（超时 / 不完整 / 过大），只用于日志，不参与路由；
    ``leftover`` 表示连接上还有没读走的字节，关连接前要先把它们读掉。
    """

    __slots__ = ("method", "path", "headers", "body", "note", "leftover")

    def __init__(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        note: str = "",
        leftover: bool = False,
    ):
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body
        self.note = note
        self.leftover = leftover

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def __repr__(self) -> str:  # pragma: no cover - 只为日志可读
        return f"Request({self.method} {self.path})"


def _left(deadline: float | None) -> float | None:
    """距离 deadline 还剩几秒；None 表示不限时。"""
    if deadline is None:
        return None
    return max(deadline - asyncio.get_running_loop().time(), 0.0)


async def _read_head(reader: asyncio.StreamReader, deadline: float | None) -> bytes:
    """读到 ``\\r\\n\\r\\n``；请求行之前的空行要跳过，不能当成「请求行不合法」。"""
    for _attempt in range(_MAX_BLANK_LINES + 1):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _left(deadline))
        except asyncio.LimitOverrunError as exc:
            raise WireError("请求头过长") from exc
        except asyncio.IncompleteReadError as exc:
            raise WireError("请求头不完整") from exc
        except asyncio.TimeoutError as exc:
            raise WireError("请求头读取超时", status=408) from exc
        if len(raw) > MAX_HEADER_BYTES:
            raise WireError(f"请求头过长：{len(raw)} 字节")
        while raw.startswith(b"\r\n"):
            raw = raw[2:]
        if raw:
            return raw
    raise WireError("请求头全是空行")


async def _read_body(
    reader: asyncio.StreamReader, length: int, deadline: float | None
) -> tuple[bytes, str, bool]:
    """按 Content-Length 读体。**读不全也返回已经拿到的部分，不抛异常。**

    返回 ``(body, note, leftover)``：``body`` 至多 ``MAX_BODY_BYTES`` 字节；
    ``note`` 是没读全的原因；``leftover`` 表示连接上还有没读走的字节。
    调用方（OTA）只关心几个字段，体坏了不该让板子拿不到 ``websocket.url``。
    """
    kept = bytearray()
    received = 0
    limit = min(length, MAX_BODY_READ)
    note = ""

    while received < limit:
        want = min(limit - received, _CHUNK)
        try:
            piece = await asyncio.wait_for(reader.readexactly(want), _left(deadline))
        except asyncio.IncompleteReadError as exc:
            piece = exc.partial
            received += len(piece)
            if len(kept) < MAX_BODY_BYTES:
                kept += piece[: MAX_BODY_BYTES - len(kept)]
            note = f"请求体不完整（声明 {length}，收到 {received}）"
            return bytes(kept), note, received < length
        except asyncio.TimeoutError:
            note = f"请求体读取超时（声明 {length}，收到 {received}）"
            return bytes(kept), note, True
        received += len(piece)
        if len(kept) < MAX_BODY_BYTES:
            kept += piece[: MAX_BODY_BYTES - len(kept)]

    if length > limit:
        note = f"请求体 {length} 字节，只读走前 {limit}"
    elif length > MAX_BODY_BYTES:
        note = f"请求体 {length} 字节，只保留前 {MAX_BODY_BYTES}"
    return bytes(kept), note, length > limit


async def read_request(
    reader: asyncio.StreamReader, *, timeout: float | None = None
) -> Request:
    """读一条完整的 HTTP 请求。

    头的错误（请求行不合法、头过长、超时）抛 ``WireError``，调用方回对应状态码；
    体的错误（不完整、超时、过大）**不抛**，只记在 ``Request.note`` 里——
    固件真正要的是响应，不是「体必须完美」。
    """
    deadline = None if not timeout else asyncio.get_running_loop().time() + float(timeout)
    raw = await _read_head(reader, deadline)

    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise WireError(f"请求行不合法：{lines[0][:80]}")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise WireError(f"请求头不合法：{line[:80]}")
        headers[name.strip().lower()] = value.strip()

    body = b""
    note = ""
    leftover = False
    declared = headers.get("content-length", "").strip()
    if declared:
        try:
            length = int(declared)
        except ValueError as exc:
            raise WireError(f"Content-Length 不是数字：{declared[:20]}") from exc
        if length < 0:
            raise WireError(f"Content-Length 是负数：{length}")
        if length:
            body, note, leftover = await _read_body(reader, length, deadline)

    return Request(
        method=parts[0].upper(),
        path=parts[1].split("?", 1)[0],
        headers=headers,
        body=body,
        note=note,
        leftover=leftover,
    )


async def write_response(
    writer: asyncio.StreamWriter,
    status: int,
    payload: dict,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """回一个 JSON 响应并声明关连接（固件每条请求后自己重连）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    lines = [
        f"HTTP/1.1 {status} {REASONS.get(status, 'Unknown')}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "Cache-Control: no-store",
    ]
    for name, value in (extra_headers or {}).items():
        lines.append(f"{name}: {value}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()


async def drain_quietly(reader: asyncio.StreamReader, timeout: float = 0.3) -> None:
    """把连接上剩下的字节读掉再关。

    对端还在发、我们却直接 close，内核会回 RST，已经写进缓冲区的响应可能一起丢——
    板子看到的就是「响应头都没收到」。只在 ``Request.leftover`` 为真时调用，
    最多耽误 ``timeout`` 秒。
    """
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(_CHUNK), timeout)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return
        if not chunk:
            return


def is_upgrade(request: Request) -> bool:
    """是不是一条 WS 升级请求。缺任一条件都按普通 HTTP 处理。"""
    if request.header("upgrade").lower() != "websocket":
        return False
    return "upgrade" in request.header("connection").lower()


def accept_key(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


async def accept(writer: asyncio.StreamWriter, request: Request) -> None:
    """回 101。固件若请求了子协议，回显第一个——不回显会让严格的客户端拒绝握手。"""
    key = request.header("sec-websocket-key")
    if not key:
        raise WireError("缺少 Sec-WebSocket-Key")
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_key(key)}",
    ]
    wanted = request.header("sec-websocket-protocol")
    if wanted:
        lines.append(f"Sec-WebSocket-Protocol: {wanted.split(',')[0].strip()}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()


class Frame:
    """一帧。``fin=False`` 表示还有续帧（由调用方累积）。"""

    __slots__ = ("opcode", "payload", "fin")

    def __init__(self, opcode: int, payload: bytes, fin: bool):
        self.opcode = opcode
        self.payload = payload
        self.fin = fin

    def __repr__(self) -> str:  # pragma: no cover - 只为日志可读
        return f"Frame(opcode={self.opcode:#x}, len={len(self.payload)}, fin={self.fin})"


async def read_frame(reader: asyncio.StreamReader) -> Frame:
    """读一帧。对端正常关闭时抛 ``IncompleteReadError``，交给调用方收尾。"""
    header = await reader.readexactly(2)
    fin = bool(header[0] & 0x80)
    if header[0] & 0x70:
        raise WireError("RSV 位非 0：不支持任何扩展")
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    if length > MAX_FRAME_BYTES:
        raise WireError(f"帧过大：{length} 字节（上限 {MAX_FRAME_BYTES}）")
    if not masked:
        raise WireError("客户端帧必须掩码")
    mask = await reader.readexactly(4)
    payload = _unmask(await reader.readexactly(length), mask)
    return Frame(opcode=opcode, payload=payload, fin=fin)


def _unmask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return b""
    repeated = (mask * (len(data) // 4 + 1))[: len(data)]
    return bytes(left ^ right for left, right in zip(data, repeated))


def encode_frame(opcode: int, payload: bytes = b"", *, fin: bool = True) -> bytes:
    """服务端发出的帧：不掩码（RFC 6455 §5.1）。"""
    head = bytearray([(0x80 if fin else 0x00) | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        head.append(length)
    elif length < 0x10000:
        head.append(126)
        head += length.to_bytes(2, "big")
    else:
        head.append(127)
        head += length.to_bytes(8, "big")
    return bytes(head) + payload


def encode_close(code: int = 1000, reason: str = "") -> bytes:
    return encode_frame(OP_CLOSE, code.to_bytes(2, "big") + reason.encode("utf-8")[:120])


async def send(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(data)
    await writer.drain()


async def send_json(writer: asyncio.StreamWriter, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    await send(writer, encode_frame(OP_TEXT, text.encode("utf-8")))
