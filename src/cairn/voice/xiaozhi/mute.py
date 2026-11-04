"""``GET /debug/mute``：板子每轮都问「我现在是不是被静音」。

这是 esp32-voice-entry 那颗固件补丁（B1）加的端点，不是小智官方协议的一部分：
板子拿不到它就在日志里刷 ``Mute: status read failed``，每轮一次。

**字段不是猜的**，逐字照参考实现（只读，未改动一行）：

- ``src/vision.py:490-499`` —— 路由本体：先放 ``{"ok": True}``，再合并
  ``state.status()``，最后补一个 ``catchup``；实现里落的是
  ``app.router.add_get("/debug/mute", debug_mute_get)``（``src/vision.py:602``）。
- ``src/mute.py:257-275`` —— ``MuteState.status()`` 的键：
  ``muted / since / changed_by / updated_at / mode / mode_note /
  counters{frames_in,frames_dropped,frames_accepted,turns} / last_frame_at /
  previous / persisted_path``。
- ``src/vision.py:104-110`` —— ``catchup`` 的键：
  ``pending / missed / spoken / last_at / running / last_result``。
- ``src/mute.py:29-32`` —— ``mode`` 是 ``"A"``（服务端丢帧），``mode_note`` 是那句说明。

M3a **不接静音状态机**：没有开关、不落盘，固定回答「未静音」。
所以键必须齐全（板子和运维脚本都按名字取值），值取「刚启动、什么都没发生」：
``muted=False``、四个计数器为 0、时间戳与 ``previous`` 为 ``null``。
``persisted_path`` 也是 ``null``——没有持久化就没有路径，比编一个假路径诚实。

写静音（``POST /debug/mute``）不在 M3a 范围内：那要真有一份进程级状态机。
"""

from __future__ import annotations

# 板子实测发的是不带尾斜杠的 ``/debug/mute``；带斜杠也收，免得换个配置就 404。
MUTE_PATHS = ("/debug/mute", "/debug/mute/")

# 参考实现的口径：mode A = 服务端丢帧，设备仍在采集上传（B2 才是设备停采）。
MODE = "A"
MODE_NOTE = "服务端丢帧；设备仍在采集并上传，B2 后才是设备停采"


def build_status() -> dict:
    """``GET /debug/mute`` 的响应体。M3a 固定「未静音、没有历史」。"""
    return {
        "ok": True,
        "muted": False,
        "since": None,
        "changed_by": None,
        "updated_at": None,
        "mode": MODE,
        "mode_note": MODE_NOTE,
        "counters": {
            "frames_in": 0,
            "frames_dropped": 0,
            "frames_accepted": 0,
            "turns": 0,
        },
        "last_frame_at": None,
        "previous": None,
        "persisted_path": None,
        "catchup": {
            "pending": 0,
            "missed": 0,
            "spoken": 0,
            "last_at": None,
            "running": False,
            "last_result": None,
        },
    }
