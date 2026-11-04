"""采集层（0.2 卡11）：只负责「拍到一张清晰的图并存下来」，不做识别。

    from cairn.capture import get_backend

    backend = get_backend()          # auto：avfoundation 优先，不可用回退 fake
    devices = backend.enumerate_devices()
    backend.open(devices[0].stable_id)
    caps = backend.capabilities()
    frame = backend.grab()           # Frame.data 是可直接落盘的 JPEG/PNG 字节
    backend.close()

`avfoundation` 子模块本身是惰性的（模块顶层不 import pyobjc），所以在这里导入它
不会让整个包在没装 pyobjc 的机器上炸掉。
"""

from cairn.capture import avfoundation
from cairn.capture.base import (
    EXPOSURE_MODES,
    FOCUS_MODES,
    MODE_LABELS,
    TRANSPORTS,
    WHITE_BALANCE_MODES,
    BackendUnavailable,
    CameraBackend,
    Capabilities,
    CaptureError,
    CaptureFormat,
    DeviceInfo,
    DeviceNotFound,
    Frame,
)
from cairn.capture.factory import CHOICES, get_backend

__all__ = [
    "avfoundation",
    "get_backend",
    "CHOICES",
    "CameraBackend",
    "Capabilities",
    "CaptureError",
    "CaptureFormat",
    "BackendUnavailable",
    "DeviceInfo",
    "DeviceNotFound",
    "Frame",
    "FOCUS_MODES",
    "EXPOSURE_MODES",
    "WHITE_BALANCE_MODES",
    "TRANSPORTS",
    "MODE_LABELS",
]
