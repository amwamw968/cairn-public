"""0.2 卡11 — 采集基座：惰性导入、假后端、能力口径、工厂回退。

全部用假后端：本卡不许测试依赖真实摄像头（插没插、有没有权限都不该影响 CI）。
"""

from __future__ import annotations

import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cairn import capture
from cairn.capture import (
    BackendUnavailable,
    Capabilities,
    CaptureError,
    CaptureFormat,
    DeviceInfo,
    DeviceNotFound,
    Frame,
)
from cairn.capture.fake import FakeCameraBackend, FakeDevice

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------- AC1 惰性导入


def test_lazy_import_survives_missing_pyobjc():
    """把 AVFoundation 打成「导入即失败」，整个包仍能 import、只报告不可用。"""
    code = "\n".join(
        [
            "import sys",
            "sys.modules['AVFoundation'] = None",
            "sys.modules['CoreMedia'] = None",
            "sys.modules['Foundation'] = None",
            "import cairn.capture as c",
            "print('package', c.__name__, callable(c.get_backend))",
            "print('available', *c.avfoundation.is_available())",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "package cairn.capture True" in result.stdout
    assert "available False" in result.stdout
    assert "pyobjc" in result.stdout


def test_is_available_never_raises():
    ok, reason = capture.avfoundation.is_available()
    assert isinstance(ok, bool)
    assert isinstance(reason, str) and reason


# --------------------------------------------------------------- AC2 设备枚举


def test_fake_backend_enumerates_stable_ids():
    backend = FakeCameraBackend()
    devices = backend.enumerate_devices()
    assert len(devices) >= 2
    ids = [device.stable_id for device in devices]
    assert len(set(ids)) == len(ids)
    assert all(device_id.startswith("fake:") for device_id in ids)
    assert {device.transport for device in devices} >= {"usb", "builtin"}
    # 不是下标：按 stable_id 打开拿回的就是同一台
    assert backend.open(devices[1].stable_id).stable_id == devices[1].stable_id
    backend.close()


def test_open_unknown_device_raises_device_not_found():
    with pytest.raises(DeviceNotFound):
        FakeCameraBackend().open("fake:not-there")


def test_capabilities_and_grab_require_open():
    backend = FakeCameraBackend()
    with pytest.raises(CaptureError):
        backend.capabilities()
    with pytest.raises(CaptureError):
        backend.grab()
    backend.open("fake:builtin")
    backend.close()
    # close 之后同样不能抓
    with pytest.raises(CaptureError):
        backend.grab()


# --------------------------------------------------------------- AC3 能力降级


def test_capabilities_shape():
    backend = FakeCameraBackend()
    backend.open("fake:uvc-pro")
    caps = backend.capabilities()
    backend.close()

    assert caps.formats and caps.best_format is not None
    assert caps.best_format.width == 320
    assert caps.focus in capture.FOCUS_MODES
    assert caps.exposure in capture.EXPOSURE_MODES
    assert caps.white_balance in capture.WHITE_BALANCE_MODES
    assert caps.can_stream is True
    assert caps.can_snapshot is True
    assert caps.notes


def test_capabilities_can_express_unsupported():
    """契约允许「不支持」：none / unknown 是正常值，不是异常，也不假装 auto。"""
    bare = FakeDevice(
        device=DeviceInfo(stable_id="fake:bare", name="只能抓单帧的假设备"),
        capabilities=Capabilities(
            formats=(CaptureFormat(80, 60, None, "png"),),
            focus="none",
            exposure="unknown",
            white_balance="unknown",
            can_stream=False,
            can_snapshot=True,
            notes=("只支持抓单帧",),
        ),
    )
    backend = FakeCameraBackend([bare])
    backend.open("fake:bare")
    caps = backend.capabilities()
    backend.close()

    assert caps.focus == "none"
    assert caps.exposure == "unknown"
    assert caps.white_balance == "unknown"
    assert caps.can_stream is False


# ------------------------------------------------------------------ AC4 抓帧


def test_grab_returns_png_with_negotiated_size():
    backend = FakeCameraBackend()
    info = backend.open("fake:uvc-pro")
    frame = backend.grab()
    backend.close()

    assert frame.device.stable_id == info.stable_id
    assert frame.image_format == "png"
    assert frame.suffix == ".png"
    assert frame.media_type == "image/png"
    assert frame.data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", frame.data[16:24])
    assert (width, height) == (frame.width, frame.height) == (320, 240)
    assert frame.note and "假帧" in frame.note


def test_consecutive_grabs_differ():
    """进度条随帧号变化：同一台设备连拍两张不是同一张图。"""
    backend = FakeCameraBackend()
    backend.open("fake:uvc-pro")
    first = backend.grab()
    second = backend.grab()
    backend.close()
    assert first.data != second.data
    assert len(first.data) > 100


def test_frame_rejects_empty_bytes():
    with pytest.raises(CaptureError):
        Frame(
            data=b"",
            width=1,
            height=1,
            image_format="png",
            device=DeviceInfo(stable_id="x", name="x"),
            captured_at=datetime.now(timezone.utc),
        )


# ------------------------------------------------------------- AC1 工厂回退


def test_factory_falls_back_to_fake_with_reason(monkeypatch):
    monkeypatch.setattr(
        capture.avfoundation,
        "is_available",
        lambda: (False, "pyobjc 未安装：No module named 'AVFoundation'"),
    )
    backend = capture.get_backend("auto")
    assert backend.name == "fake"
    assert backend.is_fake is True
    assert backend.fallback_reason is not None
    assert "pyobjc" in backend.fallback_reason


def test_factory_explicit_avfoundation_never_swaps_to_fake(monkeypatch):
    monkeypatch.setattr(capture.avfoundation, "is_available", lambda: (False, "非 macOS（linux）"))
    with pytest.raises(BackendUnavailable):
        capture.get_backend("avfoundation")


def test_factory_prefers_avfoundation_when_available(monkeypatch):
    monkeypatch.setattr(capture.avfoundation, "is_available", lambda: (True, "ok"))
    backend = capture.get_backend("auto")
    assert backend.name == "avfoundation"
    assert backend.is_fake is False
    assert backend.fallback_reason is None
    backend.close()


def test_factory_rejects_unknown_choice():
    with pytest.raises(ValueError):
        capture.get_backend("opencv")


# ------------------------------------------- AC15 AVFoundation 会话配置顺序
#
# 真机回归：`startRunning` 夹在 beginConfiguration / commitConfiguration 之间会抛
# NSGenericException（2026-10-31 在 2K USB Camera 上实测）。这里用假对象把
# 「先 commit 再 start」和「异常也要成对提交」钉成测试，不依赖摄像头。

class _FakeSession:
    def __init__(self, log: list[str], *, can_add_input: bool, can_add_output: bool) -> None:
        self._log = log
        self._can_add_input = can_add_input
        self._can_add_output = can_add_output

    def beginConfiguration(self) -> None:
        self._log.append("beginConfiguration")

    def commitConfiguration(self) -> None:
        self._log.append("commitConfiguration")

    def canAddInput_(self, _device_input: object) -> bool:
        self._log.append("canAddInput")
        return self._can_add_input

    def addInput_(self, _device_input: object) -> None:
        self._log.append("addInput")

    def canAddOutput_(self, _output: object) -> bool:
        self._log.append("canAddOutput")
        return self._can_add_output

    def addOutput_(self, _output: object) -> None:
        self._log.append("addOutput")

    def startRunning(self) -> None:
        self._log.append("startRunning")

    def stopRunning(self) -> None:
        self._log.append("stopRunning")


class _FakeOutput:
    def __init__(self, log: list[str], deliver: bool) -> None:
        self._log = log
        self._deliver = deliver

    def setOutputSettings_(self, _settings: object) -> None:
        self._log.append("setOutputSettings")

    def connectionWithMediaType_(self, _media_type: object) -> object:
        self._log.append("connection")
        return object()

    def captureStillImageAsynchronouslyFromConnection_completionHandler_(
        self, _connection: object, handler
    ) -> None:
        self._log.append("captureStill")
        if self._deliver:
            handler(object(), None)


class _FakeAllocator:
    """pyobjc 的 `cls.alloc().init()` 形状：alloc 返回自身，init 返回真正对象。"""

    def __init__(self, factory) -> None:  # type: ignore[no-untyped-def]
        self._factory = factory

    def alloc(self):
        return self

    def init(self):
        return self._factory()


class _FakeDeviceObject:
    def __init__(self, unique_id: str, name: str) -> None:
        self._unique_id = unique_id
        self._name = name

    def uniqueID(self) -> str:
        return self._unique_id

    def localizedName(self) -> str:
        return self._name

    def modelID(self) -> str:
        return "FAKE-UVC-01"

    def transportType(self) -> int:
        return 2


class _FakeCM:
    def CMVideoFormatDescriptionGetDimensions(self, _desc: object) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(width=1920, height=1080)

    def CMSampleBufferGetFormatDescription(self, _sample: object) -> object:
        return object()


def _fake_modules(log: list[str], *, can_add_output: bool = True, deliver: bool = True):
    """搭一个只有 AVFoundation 形状的假模块组，注入 _load() 的缓存。"""
    from types import SimpleNamespace

    from cairn.capture import avfoundation

    fake_device = _FakeDeviceObject("0x31400002bdf02a5", "假 AVFoundation 设备")

    def _make_output() -> _FakeOutput:
        return _FakeOutput(log, deliver)

    class _OutputFactory:
        @staticmethod
        def alloc():
            return _FakeAllocator(_make_output)

        @staticmethod
        def jpegStillImageNSDataRepresentation_(_sample: object) -> bytes:
            return b"\xff\xd8FAKEJPEG\xff\xd9"

    avf = SimpleNamespace(
        AVCaptureSession=SimpleNamespace(
            alloc=lambda: _FakeAllocator(
                lambda: _FakeSession(log, can_add_input=True, can_add_output=can_add_output)
            )
        ),
        AVCaptureDeviceInput=SimpleNamespace(
            deviceInputWithDevice_error_=staticmethod(lambda device, error: (object(), None))
        ),
        AVCaptureStillImageOutput=_OutputFactory,
        AVCaptureDevice=SimpleNamespace(
            deviceWithUniqueID_=staticmethod(lambda unique_id: fake_device),
            authorizationStatusForMediaType_=staticmethod(lambda media_type: 3),
        ),
        AVMediaTypeVideo="vide",
        AVVideoCodecKey="codec",
        AVVideoCodecJPEG="jpeg",
    )
    return avfoundation._Modules(avf=avf, cm=_FakeCM(), foundation=None)


def _install_fake_modules(monkeypatch, modules) -> None:  # type: ignore[no-untyped-def]
    from cairn.capture import avfoundation

    monkeypatch.setattr(avfoundation, "_LOADED", modules)
    monkeypatch.setattr(avfoundation, "_LOAD_ERROR", None)


def test_transport_falls_back_to_fourcc():
    """pyobjc 13 不导出 transport 常量，USB 设备回的是 OSType（"uvc "）。"""
    from types import SimpleNamespace

    from cairn.capture.avfoundation import _transport

    assert _transport(SimpleNamespace(), 0x75766320) == "usb"  # "uvc "
    assert _transport(SimpleNamespace(), 0) == "unknown"


def test_avfoundation_commits_configuration_before_start_running(monkeypatch):
    from cairn.capture import avfoundation

    log: list[str] = []
    _install_fake_modules(monkeypatch, _fake_modules(log))

    backend = avfoundation.AVFoundationBackend()
    backend.open("0x31400002bdf02a5")
    frame = backend.grab()

    assert log.count("beginConfiguration") == 1
    assert log.count("commitConfiguration") == 1
    assert log.index("commitConfiguration") < log.index("startRunning")
    assert log.index("startRunning") < log.index("stopRunning")
    assert log.index("addOutput") < log.index("commitConfiguration")
    assert log.index("commitConfiguration") < log.index("connection")
    assert (frame.width, frame.height) == (1920, 1080)
    assert frame.image_format == "jpeg"
    assert frame.data.startswith(b"\xff\xd8")


def test_avfoundation_still_commits_when_configuration_fails(monkeypatch):
    from cairn.capture import avfoundation

    log: list[str] = []
    _install_fake_modules(monkeypatch, _fake_modules(log, can_add_output=False))

    backend = avfoundation.AVFoundationBackend()
    backend.open("0x31400002bdf02a5")
    with pytest.raises(CaptureError):
        backend.grab()

    # 异常路径也要 begin/commit 成对，且绝不能在这个状态下 startRunning
    assert log.count("beginConfiguration") == 1
    assert log.count("commitConfiguration") == 1
    assert "startRunning" not in log


def test_avfoundation_grab_times_out_without_frame(monkeypatch):
    from cairn.capture import avfoundation

    log: list[str] = []
    _install_fake_modules(monkeypatch, _fake_modules(log, deliver=False))
    monkeypatch.setattr(avfoundation, "_GRAB_TIMEOUT", 0.05)

    backend = avfoundation.AVFoundationBackend()
    backend.open("0x31400002bdf02a5")
    with pytest.raises(CaptureError, match="没抓到画面"):
        backend.grab()

    assert "stopRunning" in log  # 超时也要把会话停掉，不留后台取流
