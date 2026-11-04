"""小智固件的协议接入（M3a）：只负责「连得上」，音频处理留给 M3b。

分层：

    wire.py     HTTP/1.1 单请求 + RFC 6455 帧，标准库实现（已知债务，见模块注释）
    ota.py      /xiaozhi/ota/ → {"websocket": {"url": …}}
    ws.py       hello / listen / abort 的消息形状 + M3a 应答状态机
    server.py   同端口路由 + 单连接闸 + 连接生命周期
    __main__.py python -m cairn.voice.xiaozhi --port 4412

包级导入零副作用：不建 socket、不读环境变量、不打印。
"""

from __future__ import annotations

__all__ = ["ota", "server", "wire", "ws"]
