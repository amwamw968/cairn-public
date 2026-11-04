"""复核用的模型适配层（P5-a）：一张题块图 → 题干 / 答案 / 解析。

对外只有 :func:`describe`；两个后端（本地 4B / 云端 32B）由 ``provider`` 切换，
默认本地。细节见 ``cairn.digitize.client``。
"""

from __future__ import annotations

from cairn.digitize.client import Digitized, DigitizeError, describe

__all__ = ["Digitized", "DigitizeError", "describe"]
