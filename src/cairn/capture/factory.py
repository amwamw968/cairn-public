"""后端工厂：能上真设备就上，上不了回退假设备并明确告警。

`auto` 的判定只看「后端能不能导入、是不是 macOS」，不看「有没有插摄像头」——
没插设备时 `enumerate_devices()` 返回空列表，页面提示「没找到摄像头」，
这比悄悄换成假设备更诚实。

`CAIRN_CAMERA_BACKEND` 可取 `auto`（默认）/ `avfoundation` / `fake`。
显式要 `avfoundation` 而它不可用时直接抛 `BackendUnavailable`，不偷换。
"""

from __future__ import annotations

import sys

from cairn.capture import avfoundation
from cairn.capture.base import BackendUnavailable, CameraBackend
from cairn.capture.fake import FakeCameraBackend

CHOICES = ("auto", "avfoundation", "fake")

# 同一个原因只吵一次：这个工厂可能被每个请求调一次。
_WARNED: set[str] = set()


def get_backend(prefer: str = "auto") -> CameraBackend:
    choice = (prefer or "auto").strip().lower()
    if choice not in CHOICES:
        raise ValueError(f"未知后端 {prefer!r}，可选：{'、'.join(CHOICES)}")

    if choice == "fake":
        return FakeCameraBackend()

    available, reason = avfoundation.is_available()
    if available:
        return avfoundation.AVFoundationBackend()
    if choice == "avfoundation":
        raise BackendUnavailable(reason)

    fallback = FakeCameraBackend()
    fallback.fallback_reason = f"AVFoundation 不可用（{reason}），已回退到假设备"
    _warn_once(fallback.fallback_reason)
    return fallback


def _warn_once(message: str) -> None:
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"[cairn.capture] 警告：{message}", file=sys.stderr)
