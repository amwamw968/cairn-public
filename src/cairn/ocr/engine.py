"""整页 OCR 引擎：RapidOCR 的惰性封装。

设计约束：

- **惰性初始化**：import 本模块绝不加载模型，首次 `recognize` 才构建
  RapidOCR 实例（模型加载约 10s 级），之后进程内复用。
- **可强制禁用**：环境变量 `CAIRN_OCR_DISABLE=1` 时一律抛 `OcrError`
  （测试与无模型部署用假引擎顶替）。
- **失败归一**：文件不存在 / 解码失败 / 引擎异常都抛 `OcrError`，
  消息说人话，web 层可以直接展示。
- 识别结果**不落库**（本卡只做展示，落库等切题卡）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

ENV_DISABLE = "CAIRN_OCR_DISABLE"


class OcrError(Exception):
    """OCR 不可用或失败（禁用 / 文件缺失 / 解码失败 / 引擎异常）。"""


@dataclass(frozen=True)
class OcrLine:
    """一行识别结果：文字 + 四点多边形框（原图像素坐标）+ 置信度。"""

    text: str
    box: tuple[tuple[int, int], ...]
    confidence: float
    y_center: int


# RapidOCR 实例缓存；None = 尚未初始化（import 时不加载模型）。
_engine = None


def _get_engine():
    """首次调用才 import + 构建模型；包未安装时给可执行的提示。"""
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise OcrError(
                "OCR 引擎未安装：请先安装 rapidocr-onnxruntime"
            ) from exc
        _engine = RapidOCR()
    return _engine


def _disabled() -> bool:
    return os.environ.get(ENV_DISABLE) == "1"


def recognize(path: str) -> list[OcrLine]:
    """整页识别：按 y 从上到下排序，同一视觉行内按 x 从左到右。"""
    if _disabled():
        raise OcrError(f"OCR 已被环境变量 {ENV_DISABLE}=1 禁用")
    target = Path(path)
    if not target.is_file():
        raise OcrError(f"图片不存在：{path}")
    engine = _get_engine()
    try:
        result, _elapse = engine(str(target))
    except Exception as exc:
        raise OcrError(f"图片无法识别（不存在/解码失败/引擎异常）：{exc}") from exc
    lines: list[OcrLine] = []
    for box, text, confidence in result or []:
        points = tuple(
            (int(round(float(x))), int(round(float(y)))) for x, y in box
        )
        ys = [y for _, y in points]
        lines.append(
            OcrLine(
                text=str(text),
                box=points,
                confidence=float(confidence),
                y_center=int(round(sum(ys) / len(ys))),
            )
        )
    lines.sort(key=lambda line: (line.y_center, min(x for x, _ in line.box)))
    return lines


def recognize_timed(path: str) -> tuple[list[OcrLine], float]:
    """带耗时的识别（CLI 汇报用）；返回 (文本行, 耗时秒)。"""
    started = time.perf_counter()
    lines = recognize(path)
    return lines, time.perf_counter() - started
