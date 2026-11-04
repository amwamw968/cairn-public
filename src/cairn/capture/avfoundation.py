"""AVFoundation 后端（pyobjc）：macOS 上的 UVC 摄像头/高拍仪。

**惰性导入**：模块顶层不 import pyobjc。`_load()` 在第一次真正用到时才导入并缓存，
所以没装 pyobjc、或不在 macOS 上时，`import cairn.capture` 仍然成立，
`is_available()` 返回 `(False, 原因)` 而不是抛异常。

能力读取按「先问再设」：逐个问 `isFocusModeSupported_` / `isExposureModeSupported_` /
`isWhiteBalanceModeSupported_`，不支持就落到 `none` / `unknown`，不假设能锁曝光。

会话生命周期有一条硬规则：`beginConfiguration` / `commitConfiguration` 必须成对，
`startRunning` 只能在 commit 之后。2026-10-31 在 2K USB Camera 上踩过
（`NSGenericException: startRunning may not be called between calls to
beginConfiguration and commitConfiguration`），所以现在用 `configuring` 标志 +
`finally` 保证异常路径也成对；`tests/test_capture.py` 里用假对象钉住了调用顺序。

抓帧走 `AVCaptureStillImageOutput`（原始 JPEG 字节），等帧上限 5 秒，超时抛
`CaptureError("没抓到画面：…")`，不会把 HTTP 请求无限挂住。该 API 自 10.15 起废弃，
若某天系统移除它，这里会抛 `CaptureError` 并提示改用 `AVCapturePhotoOutput`。

分辨率口径：`capabilities().formats` 列的是设备支持的**全部**格式（本机最大 2560×1440），
`grab()` 用的是会话协商出的实际格式（本机实测 1920×1080）。页面上「额定分辨率」看能力，
详情页看实拍，两者不必相等；要强制最大分辨率得再协商 `activeFormat`，本卡不做。

`DeviceInfo.vid/pid` 留空：AVFoundation 不暴露 USB 描述符，`system_profiler` 的
`VendorID_11231 ProductID_677` 是另一条路径，不在这个后端里解析。
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from cairn.capture.base import (
    BackendUnavailable,
    CameraBackend,
    Capabilities,
    CaptureError,
    CaptureFormat,
    DeviceInfo,
    DeviceNotFound,
    Frame,
)

# 抓帧最多等多久（秒）。等太久会把 HTTP 请求吊死；等太短会误报设备坏。
# 3–5 秒是产品口径：等不到就明说「没抓到画面」，不无限挂住。
_GRAB_TIMEOUT = 5.0
_PERMISSION_TIMEOUT = 15.0

_AUTHORIZED = 3
_DENIED_STATUSES = (1, 2)  # restricted / denied

# 枚举哪些设备类型：外接 UVC（高拍仪走这条）、内置、连续性相机、桌面视角。
# 常量在不同 macOS 版本里陆续加入，取不到就跳过。
_DEVICE_TYPE_CONSTANTS = (
    "AVCaptureDeviceTypeExternal",
    "AVCaptureDeviceTypeBuiltInWideAngleCamera",
    "AVCaptureDeviceTypeContinuityCamera",
    "AVCaptureDeviceTypeDeskViewCamera",
)

# transportType 的数值不稳定，按符号常量比对，比对不上就 unknown。
_TRANSPORT_CONSTANTS = (
    ("AVCaptureDeviceTransportTypeUSB", "usb"),
    ("AVCaptureDeviceTransportTypeBuiltIn", "builtin"),
    ("AVCaptureDeviceTransportTypeContinuityCamera", "continuity"),
    ("AVCaptureDeviceTransportTypeVirtual", "virtual"),
    ("AVCaptureDeviceTransportTypePCI", "virtual"),
)


@dataclass(frozen=True)
class _Modules:
    avf: object
    cm: object
    foundation: object


_LOADED: _Modules | None = None
_LOAD_ERROR: str | None = None


def _load() -> _Modules:
    """导入 pyobjc 框架；失败转成 `BackendUnavailable` 并缓存原因。"""
    global _LOADED, _LOAD_ERROR
    if _LOADED is not None:
        return _LOADED
    if sys.platform != "darwin":
        raise BackendUnavailable(f"非 macOS（{sys.platform}），没有 AVFoundation")
    if _LOAD_ERROR is not None:
        raise BackendUnavailable(_LOAD_ERROR)
    try:
        import AVFoundation  # noqa: PLC0415  (惰性导入是本模块的核心约束)
        import CoreMedia  # noqa: PLC0415
        import Foundation  # noqa: PLC0415
    except ImportError as exc:
        _LOAD_ERROR = f"pyobjc 未安装或 AVFoundation 不可导入：{exc}"
        raise BackendUnavailable(_LOAD_ERROR) from None
    _LOADED = _Modules(AVFoundation, CoreMedia, Foundation)
    return _LOADED


def is_available() -> tuple[bool, str]:
    """(能否用, 原因)。任何情况下都不抛异常。"""
    try:
        _load()
    except BackendUnavailable as exc:
        return False, str(exc)
    return True, "pyobjc 的 AVFoundation 已就绪"


def _fourcc(code: int) -> str:
    raw = bytes(((code >> 24) & 0xFF, (code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF))
    text = raw.decode("ascii", "replace").strip()
    return text or "unknown"


def _transport(avf: object, value: int) -> str:
    for constant, label in _TRANSPORT_CONSTANTS:
        known = getattr(avf, constant, None)
        if known is not None and int(value) == int(known):
            return label
    # 新版 AVFoundation 对 USB 设备回的是 OSType 四字码（本机实测 0x75766320 == "uvc "），
    # 而 pyobjc 13 没有导出 AVCaptureDeviceTransportType* 常量，只能按四字码兜底。
    code = _fourcc(int(value)).strip().lower()
    return {"uvc": "usb", "usb": "usb", "bltn": "builtin", "virt": "virtual"}.get(code, "unknown")


def _supports(device: object, method: str, mode: int) -> bool:
    checker = getattr(device, method, None)
    if checker is None:
        return False
    return bool(checker(mode))


def _focus_mode(avf: object, device: object) -> str:
    if _supports(device, "isFocusModeSupported_", avf.AVCaptureFocusModeAutoFocus) or _supports(
        device, "isFocusModeSupported_", avf.AVCaptureFocusModeContinuousAutoFocus
    ):
        return "auto"
    if _supports(device, "isFocusModeSupported_", avf.AVCaptureFocusModeLocked):
        return "fixed"  # 能锁在当前位置，但不是手动调焦
    return "none"


def _exposure_mode(avf: object, device: object) -> str:
    if _supports(device, "isExposureModeSupported_", avf.AVCaptureExposureModeCustom):
        return "custom"
    if _supports(device, "isExposureModeSupported_", avf.AVCaptureExposureModeAutoExpose) or _supports(
        device, "isExposureModeSupported_", avf.AVCaptureExposureModeContinuousAutoExposure
    ):
        return "auto"
    if _supports(device, "isExposureModeSupported_", avf.AVCaptureExposureModeLocked):
        return "locked"
    return "unknown"


def _white_balance_mode(avf: object, device: object) -> str:
    if _supports(device, "isWhiteBalanceModeSupported_", avf.AVCaptureWhiteBalanceModeLocked):
        return "locked"
    if _supports(device, "isWhiteBalanceModeSupported_", avf.AVCaptureWhiteBalanceModeAutoWhiteBalance) or _supports(
        device, "isWhiteBalanceModeSupported_", avf.AVCaptureWhiteBalanceModeContinuousAutoWhiteBalance
    ):
        return "auto"
    return "unknown"


def _device_info(avf: object, device: object) -> DeviceInfo:
    model = getattr(device, "modelID", None)
    model_id = str(model()) if callable(model) else None
    return DeviceInfo(
        stable_id=str(device.uniqueID()),
        name=str(device.localizedName()),
        model=model_id or None,
        vid=None,
        pid=None,
        transport=_transport(avf, device.transportType()),
    )


class AVFoundationBackend(CameraBackend):
    name = "avfoundation"

    def __init__(self) -> None:
        self._opened: DeviceInfo | None = None
        self._device: object | None = None

    def enumerate_devices(self) -> list[DeviceInfo]:
        mods = _load()
        types = [getattr(mods.avf, name) for name in _DEVICE_TYPE_CONSTANTS if getattr(mods.avf, name, None) is not None]
        if not types:
            raise BackendUnavailable("这个系统版本没有可用的视频设备类型常量")
        session = mods.avf.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
            types,
            mods.avf.AVMediaTypeVideo,
            mods.avf.AVCaptureDevicePositionUnspecified,
        )
        if session is None:
            raise BackendUnavailable("无法创建设备发现会话")
        return [_device_info(mods.avf, device) for device in session.devices()]

    def open(self, device_id: str) -> DeviceInfo:
        mods = _load()
        device = mods.avf.AVCaptureDevice.deviceWithUniqueID_(device_id)
        if device is None:
            raise DeviceNotFound(f"没有这台设备：{device_id}")
        self._device = device
        self._opened = _device_info(mods.avf, device)
        return self._opened

    def capabilities(self) -> Capabilities:
        mods = _load()
        device = self._require_device()
        formats: list[CaptureFormat] = []
        for fmt in device.formats():
            desc = fmt.formatDescription()
            dimensions = mods.cm.CMVideoFormatDescriptionGetDimensions(desc)
            ranges = list(fmt.videoSupportedFrameRateRanges() or [])
            fps = float(ranges[0].maxFrameRate()) if ranges else None
            formats.append(
                CaptureFormat(
                    width=int(dimensions.width),
                    height=int(dimensions.height),
                    fps=fps,
                    pixel_format=_fourcc(mods.cm.CMFormatDescriptionGetMediaSubType(desc)),
                )
            )
        formats.sort(key=lambda item: item.pixels, reverse=True)

        focus = _focus_mode(mods.avf, device)
        exposure = _exposure_mode(mods.avf, device)
        white_balance = _white_balance_mode(mods.avf, device)
        notes = ["抓帧统一输出 JPEG 字节（AVCaptureStillImageOutput）", "本卡只读能力，不写曝光/对焦"]
        if exposure == "unknown":
            notes.append("曝光：这台设备不支持任何可查询的曝光模式，用驱动默认值")
        if focus == "none":
            notes.append("对焦：不支持自动/锁定对焦，完全交给设备")
        return Capabilities(
            formats=tuple(formats),
            focus=focus,
            exposure=exposure,
            white_balance=white_balance,
            can_stream=True,
            can_snapshot=True,
            notes=tuple(notes),
        )

    def grab(self) -> Frame:
        mods = _load()
        device = self._require_device()
        self._ensure_access(mods)

        output_cls = getattr(mods.avf, "AVCaptureStillImageOutput", None)
        if output_cls is None:
            raise CaptureError(
                "这个系统没有 AVCaptureStillImageOutput；需要改用 AVCapturePhotoOutput"
                "（要 delegate + runloop，本卡未实现）"
            )

        session = mods.avf.AVCaptureSession.alloc().init()
        running = False
        # AVFoundation 的硬规则：beginConfiguration 与 commitConfiguration 必须成对，
        # 且 startRunning 只能在 commit 之后（在两者之间调用会抛 NSGenericException）。
        # 所以用 configuring 标志 + finally 兜住异常路径，绝不把会话留在「配置中」。
        configuring = True
        session.beginConfiguration()
        try:
            device_input, error = mods.avf.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
            if device_input is None:
                raise CaptureError(f"打开设备输入失败：{error}")
            if not session.canAddInput_(device_input):
                raise CaptureError("采集会话拒绝了这台设备的输入")
            session.addInput_(device_input)

            output = output_cls.alloc().init()
            output.setOutputSettings_({mods.avf.AVVideoCodecKey: mods.avf.AVVideoCodecJPEG})
            if not session.canAddOutput_(output):
                raise CaptureError("采集会话拒绝了静态图输出")
            session.addOutput_(output)

            session.commitConfiguration()
            configuring = False

            # 连接要在 commit 之后取：配置未提交时 output 还没有建立连接。
            connection = output.connectionWithMediaType_(mods.avf.AVMediaTypeVideo)
            if connection is None:
                raise CaptureError("静态图输出没有视频连接")

            session.startRunning()
            running = True
            sample = self._capture_still(output, connection)

            dimensions = mods.cm.CMVideoFormatDescriptionGetDimensions(
                mods.cm.CMSampleBufferGetFormatDescription(sample)
            )
            raw = mods.avf.AVCaptureStillImageOutput.jpegStillImageNSDataRepresentation_(sample)
            if raw is None:
                raise CaptureError("JPEG 编码失败，静态图输出没有给回数据")
            return Frame(
                data=bytes(raw),
                width=int(dimensions.width),
                height=int(dimensions.height),
                image_format="jpeg",
                device=self._opened or _device_info(mods.avf, device),
                captured_at=datetime.now(timezone.utc),
            )
        finally:
            if configuring:
                # 配置途中出错也要成对收尾；这里再抛只会盖掉原始异常，所以吞掉。
                try:
                    session.commitConfiguration()
                except Exception:  # noqa: BLE001  (cleanup 阶段，不能掩盖原错误)
                    pass
            if running:
                session.stopRunning()

    def close(self) -> None:
        self._device = None
        self._opened = None

    # --- 内部 ------------------------------------------------------------

    def _require_device(self) -> object:
        if self._device is None:
            raise CaptureError("设备还没 open()，按 enumerate → open → capabilities → grab 顺序来")
        return self._device

    def _ensure_access(self, mods: _Modules) -> None:
        status = mods.avf.AVCaptureDevice.authorizationStatusForMediaType_(mods.avf.AVMediaTypeVideo)
        if status == _AUTHORIZED:
            return
        if status in _DENIED_STATUSES:
            raise CaptureError(
                "没有摄像头权限：系统设置 → 隐私与安全性 → 摄像头，打开后重试"
            )
        granted: dict[str, bool] = {}
        done = threading.Event()

        def handler(ok: bool) -> None:
            granted["ok"] = bool(ok)
            done.set()

        mods.avf.AVCaptureDevice.requestAccessForMediaType_completionHandler_(mods.avf.AVMediaTypeVideo, handler)
        if not done.wait(_PERMISSION_TIMEOUT) or not granted.get("ok"):
            raise CaptureError(f"摄像头授权未通过（{_PERMISSION_TIMEOUT:.0f}s 内没有拿到许可）")

    def _capture_still(self, output: object, connection: object) -> object:
        box: dict[str, object] = {}
        done = threading.Event()

        def handler(sample_buffer: object, error: object) -> None:
            box["sample"] = sample_buffer
            box["error"] = error
            done.set()

        output.captureStillImageAsynchronouslyFromConnection_completionHandler_(connection, handler)
        if not done.wait(_GRAB_TIMEOUT):
            raise CaptureError(
                f"没抓到画面：{_GRAB_TIMEOUT:.0f} 秒内没有拿到帧"
                "（设备可能被别的程序占用，或没有授权）"
            )
        if box.get("error") is not None:
            raise CaptureError(f"抓帧失败：{box['error']}")
        sample = box.get("sample")
        if sample is None:
            raise CaptureError("抓帧没有返回图像数据")
        return sample
