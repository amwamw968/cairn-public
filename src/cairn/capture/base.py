"""设备无关的采集契约（0.2 卡11）。

按 V4L2 的「先问再设」模型分层：`enumerate → open → capabilities → grab → close`。
后端可以是 AVFoundation（真实 UVC 摄像头/高拍仪）、假设备（测试）或将来别的东西，
上层（`scan_service` / 路由）只认这里的类型。

三条硬约束（`docs/DEVICE-BASE-RESEARCH.md` 第三节）：

1. **能力先探测、不支持就降级**：`Capabilities` 里表达「不支持」用 ``none`` / ``unknown``，
   不假设任何设备能锁曝光、能手动对焦。
2. **设备 ID 用稳定标识，不用索引**：`DeviceInfo.stable_id` 是 AVFoundation 的 `uniqueID`
   （或 USB 序列号），插拔、重启后仍然指向同一台设备；列表下标会漂移。
3. **交给上层的是编码后的字节**：本仓库不装 PIL/numpy，后端协商好格式后直接把
   JPEG/PNG 字节放进 `Frame`，落盘就是原样写文件。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime

# 能力取值口径：unknown = 没问出来（后端不支持探测），none = 明确不支持。
FOCUS_MODES = ("none", "fixed", "auto", "manual", "unknown")
EXPOSURE_MODES = ("auto", "locked", "custom", "unknown")
WHITE_BALANCE_MODES = ("auto", "locked", "custom", "unknown")

# 页面用的中文口径；后端只给英文枚举值，翻译放在模板上下文里。
MODE_LABELS = {
    "none": "不支持",
    "fixed": "固定",
    "auto": "自动",
    "manual": "手动",
    "locked": "可锁定",
    "custom": "可自定义",
    "unknown": "未知",
}

TRANSPORTS = ("usb", "builtin", "continuity", "virtual", "unknown")

_SUFFIXES = {"jpeg": ".jpg", "png": ".png", "unknown": ".bin"}
_MEDIA_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "unknown": "application/octet-stream",
}


class CaptureError(RuntimeError):
    """采集层失败（抓帧超时、设备被占用、帧为空……）。"""


class BackendUnavailable(CaptureError):
    """后端本身不可用：非 macOS、没装 pyobjc、没有摄像头权限。"""


class DeviceNotFound(CaptureError):
    """`open()` 的 stable_id 不在当前设备列表里。"""


@dataclass(frozen=True)
class DeviceInfo:
    """一台设备的最小身份。`stable_id` 是唯一可信的定位键。"""

    stable_id: str
    name: str
    model: str | None = None
    vid: int | None = None
    pid: int | None = None
    transport: str = "unknown"

    @property
    def label(self) -> str:
        return f"{self.name}（{self.model}）" if self.model else self.name

    @property
    def usb_id(self) -> str | None:
        if self.vid is None or self.pid is None:
            return None
        return f"{self.vid:04x}:{self.pid:04x}"


@dataclass(frozen=True)
class CaptureFormat:
    """一个可用的采集格式。`pixel_format` 是后端交出的编码（jpeg/png/…）。"""

    width: int
    height: int
    fps: float | None = None
    pixel_format: str = "unknown"

    @property
    def label(self) -> str:
        size = f"{self.width}×{self.height}"
        return f"{size}@{self.fps:g}fps" if self.fps else size

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class Capabilities:
    """设备能力快照。字段按调研报告第三节的顺序排；range 等要写参数时再加。"""

    formats: tuple[CaptureFormat, ...] = ()
    focus: str = "unknown"
    exposure: str = "unknown"
    white_balance: str = "unknown"
    can_stream: bool = False
    can_snapshot: bool = False
    notes: tuple[str, ...] = ()

    @property
    def best_format(self) -> CaptureFormat | None:
        """最大分辨率的那一条（页面「额定分辨率」用）。"""
        return max(self.formats, key=lambda f: f.pixels, default=None)


@dataclass(frozen=True)
class Frame:
    """一帧已编码的图。`data` 就是落盘字节，不做二次转码。"""

    data: bytes
    width: int
    height: int
    image_format: str
    device: DeviceInfo
    captured_at: datetime
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.data:
            raise CaptureError("抓到空帧：后端没有返回图像字节")

    @property
    def suffix(self) -> str:
        return _SUFFIXES.get(self.image_format, ".bin")

    @property
    def media_type(self) -> str:
        return _MEDIA_TYPES.get(self.image_format, "application/octet-stream")


class CameraBackend(abc.ABC):
    """后端基类。同一时刻只开一台设备：open 覆盖上一台。"""

    name: str = "base"
    is_fake: bool = False
    # 工厂回退到假设备时写在这里；页面据此常驻显示降级提示。
    fallback_reason: str | None = None

    @abc.abstractmethod
    def enumerate_devices(self) -> list[DeviceInfo]:
        """当前可见设备。没有设备返回空列表，不是错误。"""

    @abc.abstractmethod
    def open(self, device_id: str) -> DeviceInfo:
        """打开一台设备并返回它的身份；id 不认识就抛 `DeviceNotFound`。"""

    @abc.abstractmethod
    def capabilities(self) -> Capabilities:
        """读取**已打开**设备的能力。不支持的能力返回 none/unknown。"""

    @abc.abstractmethod
    def grab(self) -> Frame:
        """抓一帧。失败抛 `CaptureError`，不返回空帧。"""

    @abc.abstractmethod
    def close(self) -> None:
        """释放设备；重复调用必须无害。"""

    # --- 子类共用的小工具 ------------------------------------------------

    @property
    def opened_device(self) -> DeviceInfo | None:
        return getattr(self, "_opened", None)

    def _require_open(self) -> DeviceInfo:
        device = self.opened_device
        if device is None:
            raise CaptureError("设备还没 open()，按 enumerate → open → capabilities → grab 顺序来")
        return device

    def __enter__(self) -> "CameraBackend":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
