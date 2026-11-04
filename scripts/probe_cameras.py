#!/usr/bin/env python3
"""列出本机摄像头（含 UVC 高拍仪）与各自能力，人工验证用（0.2 卡11）。

    .venv/bin/python scripts/probe_cameras.py
    .venv/bin/python scripts/probe_cameras.py --backend avfoundation

输出三段：

1. **运行环境**：平台、Python、pyobjc/AVFoundation 是否可用；
2. **系统层设备清单**：`system_profiler SPCameraDataType`（纯 stdlib）。即使没装 pyobjc，
   也能在这里看到插着的 UVC 设备——两条路互为核对，免得「探针说没设备」和「系统说有」
   互相打脸；
3. **采集后端**：枚举到的设备 + 先问再设拿到的能力（分辨率、对焦、曝光、白平衡）。

探针只读能力，不抓帧、不写盘、不改任何采集参数。
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cairn.capture import (  # noqa: E402  (先补 sys.path 再导入)
    BackendUnavailable,
    CaptureError,
    avfoundation,
    get_backend,
)
from cairn.capture.base import MODE_LABELS  # noqa: E402


def _section(title: str) -> None:
    print(f"\n== {title} ==")


def system_cameras() -> list[dict[str, str]]:
    """系统层摄像头清单；调不通就返回空列表（探针不该因为这条挂掉）。"""
    try:
        result = subprocess.run(
            ["system_profiler", "SPCameraDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    rows = []
    for item in payload.get("SPCameraDataType", []) or []:
        rows.append(
            {
                "name": str(item.get("_name") or "未命名"),
                "model": str(item.get("spcamera_model-id") or ""),
                "unique": str(item.get("spcamera_unique-id") or ""),
            }
        )
    return rows


def mode(value: str) -> str:
    return f"{value}（{MODE_LABELS.get(value, value)}）"


def main() -> int:
    parser = argparse.ArgumentParser(description="列出本机摄像头与能力")
    parser.add_argument(
        "--backend",
        default="auto",
        choices=("auto", "avfoundation", "fake"),
        help="auto=优先 AVFoundation，不可用回退假设备（默认）；fake=只看假设备",
    )
    args = parser.parse_args()

    _section("运行环境")
    print(f"平台        : {platform.platform()}")
    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    available, reason = avfoundation.is_available()
    print(f"AVFoundation: {'可用' if available else '不可用'} — {reason}")
    if not available:
        print("              → 真后端取不到流；抓帧会走假设备。系统层清单在下一段。")

    _section("系统层摄像头（system_profiler SPCameraDataType）")
    cameras = system_cameras()
    if cameras:
        for index, camera in enumerate(cameras, start=1):
            print(f"[{index}] {camera['name']}")
            if camera["model"]:
                print(f"    model  : {camera['model']}")
            if camera["unique"]:
                print(f"    unique : {camera['unique']}")
    else:
        print("（没有读到；可能没插设备，或这台不是 macOS）")

    _section(f"采集后端：--backend {args.backend}")
    try:
        backend = get_backend(args.backend)
    except (BackendUnavailable, ValueError) as exc:
        print(f"后端不可用：{exc}")
        return 1

    print(f"选中后端    : {backend.name}")
    if backend.fallback_reason:
        print(f"降级原因    : {backend.fallback_reason}")
    if backend.is_fake and not backend.fallback_reason:
        print("说明        : 假设备，只验证链路，不接触真实摄像头")

    try:
        devices = backend.enumerate_devices()
    except CaptureError as exc:
        print(f"枚举失败：{exc}")
        return 1

    if not devices:
        print("没有枚举到设备。")
        return 0

    for index, device in enumerate(devices, start=1):
        print(f"\n[{index}] {device.name}")
        print(f"    stable_id : {device.stable_id}")
        print(f"    model     : {device.model or '—'}")
        print(f"    transport : {device.transport}")
        if device.usb_id:
            print(f"    usb       : {device.usb_id}")
        try:
            backend.open(device.stable_id)
            caps = backend.capabilities()
        except CaptureError as exc:
            print(f"    能力读取失败：{exc}")
            continue
        finally:
            backend.close()

        best = caps.best_format
        print(f"    额定分辨率: {best.label if best else '未知'}")
        print(f"    格式数    : {len(caps.formats)}")
        for fmt in caps.formats[:6]:
            print(f"      - {fmt.label} · {fmt.pixel_format}")
        if len(caps.formats) > 6:
            print(f"      … 另有 {len(caps.formats) - 6} 种")
        print(f"    对焦      : {mode(caps.focus)}")
        print(f"    曝光      : {mode(caps.exposure)}")
        print(f"    白平衡    : {mode(caps.white_balance)}")
        print(f"    连续取流  : {'支持' if caps.can_stream else '不支持'}")
        print(f"    抓单帧    : {'支持' if caps.can_snapshot else '不支持'}")
        for note in caps.notes:
            print(f"    备注      : {note}")

    print(f"\n共 {len(devices)} 台设备（后端 {backend.name}）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
