"""假设备后端（0.2 卡11）：测试与「没摄像头」时的链路验证。

- 纯标准库生成 PNG，不碰 numpy/PIL（本机 venv 两者都没有，也不引）。
- 图是色条 + 底部进度条：同一台设备连续抓帧，进度条会变长，人眼能看出「这是第几张」。
  没做文字渲染：没有 PIL 就得内嵌点阵字库，属于给假设备加戏，不做。
- 设备能力可定制：测试用一台 `focus="none"` 的设备验证「不支持就降级」的显示口径。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Sequence

from cairn.capture.base import (
    CameraBackend,
    Capabilities,
    CaptureError,
    CaptureFormat,
    DeviceInfo,
    DeviceNotFound,
    Frame,
)

# 纯色块的调色板：六条竖向色带，和前一台设备的底色错开。
_BARS = (
    (235, 87, 87),
    (242, 153, 74),
    (242, 201, 76),
    (111, 207, 151),
    (86, 148, 230),
    (155, 120, 220),
)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int]]) -> bytes:
    """把逐像素取样函数写成 8 位真彩 PNG（filter 0，无交错）。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # 每行的 filter type
        for x in range(width):
            raw.extend(pixel(x, y))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


@dataclass(frozen=True)
class FakeDevice:
    """一台假设备：身份 + 能力 + 成像颜色。/ 尺寸从 capabilities 的第一条格式来。"""

    device: DeviceInfo
    capabilities: Capabilities = field(default_factory=Capabilities)
    color: tuple[int, int, int] = (210, 214, 220)

    @property
    def size(self) -> tuple[int, int]:
        fmt = self.capabilities.formats[0] if self.capabilities.formats else None
        return (fmt.width, fmt.height) if fmt else (160, 120)


def default_devices() -> list[FakeDevice]:
    """两台假设备：一台「UVC 高拍仪」，一台「内置摄像头」，能力故意不一样。"""
    uvc = FakeDevice(
        device=DeviceInfo(
            stable_id="fake:uvc-pro",
            name="假高拍仪（UVC）",
            model="FAKE-UVC-01",
            vid=0x2BDF,
            pid=0x02A5,
            transport="usb",
        ),
        capabilities=Capabilities(
            formats=(
                CaptureFormat(320, 240, 30.0, "png"),
                CaptureFormat(160, 120, 30.0, "png"),
            ),
            focus="auto",
            exposure="auto",
            white_balance="auto",
            can_stream=True,
            can_snapshot=True,
            notes=("假设备：只用来看链路是否通，不接触真实摄像头",),
        ),
        color=(214, 232, 246),
    )
    builtin = FakeDevice(
        device=DeviceInfo(
            stable_id="fake:builtin",
            name="假内置摄像头",
            model="FAKE-BUILTIN",
            transport="builtin",
        ),
        capabilities=Capabilities(
            formats=(
                CaptureFormat(160, 120, 30.0, "png"),
                CaptureFormat(80, 60, 30.0, "png"),
            ),
            focus="fixed",
            exposure="locked",
            white_balance="locked",
            can_stream=True,
            can_snapshot=True,
        ),
        color=(236, 228, 214),
    )
    return [uvc, builtin]


class FakeCameraBackend(CameraBackend):
    name = "fake"
    is_fake = True

    def __init__(
        self,
        devices: Sequence[FakeDevice] | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._devices = list(default_devices() if devices is None else devices)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._opened: FakeDevice | None = None
        self._frames = 0

    def enumerate_devices(self) -> list[DeviceInfo]:
        return [item.device for item in self._devices]

    def open(self, device_id: str) -> DeviceInfo:
        for item in self._devices:
            if item.device.stable_id == device_id:
                self._opened = item
                return item.device
        raise DeviceNotFound(f"假设备里没有 {device_id!r}")

    def capabilities(self) -> Capabilities:
        item = self._opened_item()
        return item.capabilities

    def grab(self) -> Frame:
        item = self._opened_item()
        width, height = item.size
        self._frames += 1
        index = self._frames
        progress = (index % 8 + 1) / 8

        bar_height = max(3, height // 8)
        footer = max(4, height // 12)
        footer_top = height - footer
        bars = len(_BARS)
        base = item.color

        def pixel(x: int, y: int) -> tuple[int, int, int]:
            if x < 2 or x >= width - 2 or y < 2 or y >= height - 2:
                return (40, 44, 52)  # 边框：一眼看出是生成图
            if y < bar_height:
                return _BARS[min(bars - 1, x * bars // width)]
            if y >= footer_top:
                return (250, 250, 250) if x < int(width * progress) else (60, 60, 60)
            if 0 <= y - bar_height < 2:
                return (255, 255, 255)
            return base

        return Frame(
            data=encode_png(width, height, pixel),
            width=width,
            height=height,
            image_format="png",
            device=item.device,
            captured_at=self._now(),
            note=f"假帧 #{index}（色条 + 进度条，非真实拍摄）",
        )

    def close(self) -> None:
        self._opened = None

    def _opened_item(self) -> FakeDevice:
        if self._opened is None:
            raise CaptureError("假设备还没 open()，按 enumerate → open → capabilities → grab 顺序来")
        return self._opened
