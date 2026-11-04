"""小角度倾斜校正：用 OCR 框基线估计整页倾斜角并摆正（只用 PIL，不引 cv2）。

拍歪的卷子先校正再切题，切题结果才和摆正时一致。
角度估计：每个识别框的**底边（基线）**与水平线的夹角，全部框取
中位数——单个框受识别噪声影响会抖，中位数对个别歪框稳健。

**和 `orient` 的顺序：先 orient（整页 90° 的倍数），再 deskew（±5° 小角度）。**
理由（实测）：整页躺倒时文字基线是竖的，行框成了竖条，而"底边"仍是水平的，
`estimate_angle` 对四个角度的真图一律返回 0.00——它看不见 90°，对躺倒页只能
什么都不做。反过来先摆正，基线回到水平，±5° 的估计才落在它擅长的量程里。
先 deskew 后 orient 也不行：deskew 会把竖直基线算成 0°，白跑一次全页 OCR，
还可能把噪声当倾斜量、把好图转歪。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

from cairn.ocr.engine import OcrLine, recognize

# 估计角绝对值小于该阈值（度）直接返回原图，避免无谓重采样
MIN_ANGLE = 0.5


def estimate_angle(lines: list[OcrLine]) -> float:
    """估计整页倾斜角（度）：各框底边角度的中位数。

    图像坐标 y 向下：正文顺时针歪（右边低）→ 底边 dy>0 → 返回正值。
    无框（空白页）返回 0.0。
    """
    angles: list[float] = []
    for line in lines:
        # 底边 = y 最大的两点，按 x 排序（框点序不依赖识别器保证）
        bottom = sorted(line.box, key=lambda p: p[1])[-2:]
        (x0, y0), (x1, y1) = sorted(bottom)
        if x1 == x0 and y1 == y0:
            continue
        angles.append(math.degrees(math.atan2(y1 - y0, x1 - x0)))
    if not angles:
        return 0.0
    angles.sort()
    return angles[len(angles) // 2]


def deskew_image(image: Image.Image, lines: list[OcrLine]) -> tuple[Image.Image, float]:
    """用**已经识别好的行**做小角度摆正：`(rotated_image, angle)`。

    给"先 orient 再 deskew"的流水线用：orient 已经把整页摆正、并做过全页 OCR，
    这里直接吃它的行，不再跑一遍识别（一页一次 OCR 的预算就是这么省的）。
    判定与 `deskew(path)` 完全一致：|angle| < 0.5° 原样返回**同一个图像对象**。
    """
    angle = estimate_angle(lines)
    if abs(angle) < MIN_ANGLE:
        return image, angle
    rotated = image.rotate(
        angle,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=(255, 255, 255),
    )
    return rotated, angle


def deskew(path: str) -> tuple[Image.Image, float]:
    """估计倾斜角并返回摆正后的图：`(rotated_image, angle)`。

    - |angle| < 0.5°：不重采样，直接返回原图（省一次有损插值）。
    - 否则 PIL `rotate(angle, expand=True)` 摆正，白底填充。
    - 角度估计依赖 OCR，`CAIRN_OCR_DISABLE=1` / 文件缺失时照常抛 `OcrError`。
    """
    lines = recognize(path)
    return deskew_image(Image.open(Path(path)), lines)
