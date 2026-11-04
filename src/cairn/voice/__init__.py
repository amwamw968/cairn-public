"""语音入口（M1 文本回合 + M2 麦克风）。

分三层，换语音实现时不改业务：

    cli.py     交互式命令行：``python -m cairn.voice`` / ``--mic``
    router.py  一句话 → Intent（闲聊 / 要题 / 判分 / 进度 / 题库）
    turn.py    一轮：路由 → 调 MCP 工具 → 念的与约束分开
    mic.py     麦克风采集 + 能量断句 → 16-bit PCM WAV（M2）
    asr.py     WAV → 文字，调本机 OpenAI 兼容 ASR（M2）
    tts.py     文字 → 声音，用 macOS ``say``（M2）

铁律：业务判断一律转调 ``cairn.mcp_server`` 里已有的函数，voice 层不复制取题、
判分、进度逻辑。关键词只在 ``router.py`` 一处，``turn.py`` 只认枚举。依据见
``docs/VOICE-SKELETON.md``。

``mic`` / ``asr`` / ``tts`` 故意不在本文件 re-export：``mic`` 只在真要开麦克风时
才 import ``sounddevice``，包级导入保持零副作用。
"""

from __future__ import annotations

from cairn.voice.router import Intent, pick_subject, route
from cairn.voice.turn import Turn, run_turn

__all__ = ["Intent", "Turn", "pick_subject", "route", "run_turn"]
