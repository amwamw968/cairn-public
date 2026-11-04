"""红笔痕迹检测：题框里**有没有**红笔，不判是勾还是叉。

老师判卷用红笔，家长却要在标错页逐题回忆"哪几道错了"。这里把"题框里有红笔"
的题先勾上，家长只需确认或取消。**只做有无**：勾/叉/分数一视同仁——分辨符号
要等真实批改样本标定，本模块不猜。

## 红色判据

    R > 110  且  R − G > 45  且  R − B > 45

三个条件缺一不可：

- `R > 110`：压掉深褐/暗红阴影（纸上阴影、笔杆投影），它们 R 也偏高但整体很暗；
- `R − G > 45`、`R − B > 45`：要求红色通道**明显压过**另外两个通道。红笔（正红
  200,30,40 / 圆珠笔暗红 150,60,60）都远高于这条线；而
  - 黑笔/印刷字（R≈G≈B，差值 0）、
  - 蓝笔（B 最大，R−B 为负）、
  - 浅灰阴影（205,205,200，差值 5）、
  - 暖光下的白纸（252,240,220，R−G=12、R−B=32）都过不了。

判据只在 RGB 上做差值，不看绝对亮度；`convert("RGB")` 之后灰度图三通道相等，
天然全部判 False（不会因为"灰度扫描件"炸出红笔）。

## 阈值

`RED_RATIO_THRESHOLD = 0.10%`（红像素数 / 该框面积）。标定依据（合成图，
460×220 题框，JPEG quality 85 前后一致，见 docs/EVIDENCE.md）：

| 样本 | 红像素占比 |
| --- | --- |
| 红勾（线宽 6 / 4） | 1.10% / 0.79% |
| 红叉（线宽 6 / 4） | 2.05% / 2.05% |
| 红分数「-5」56px | 0.52% |
| 红分数「100」28px | 0.33% |
| 红分数「-5」28px（最弱的真痕迹） | 0.135% |
| 蓝笔勾 / 黑笔勾 / 浅灰阴影 / 印刷+灰线 / 空白纸 | 0.000% |

取 0.10%：对最弱的真痕迹（0.135%）留 1.35× 余量，而所有非红样本实测 0.000%，
中间是一段空档。**宁可多勾**：多勾家长点一下取消，漏勾就是漏一道错题——所以
阈值贴着真痕迹一侧，不往噪声一侧抬。代价是卷面上**印刷的红字/红框**也会命中，
这在本卡范围内（只判有无），真机上表现为多勾一道、家长取消即可。

## 依赖

用到 numpy——它**不是本卡新引的依赖**：`rapidocr-onnxruntime`（`pyproject.toml`
里已有）声明了 `numpy<3.0.0,>=1.19.5`，环境里 2.5.4 随它装进来。用它是因为
2560×1098 的整页要扫 280 万像素，纯 PIL/Python 逐像素会超出 AC6 的 0.5 秒预算
（实测见 EVIDENCE）；numpy 只做一次矢量化掩码，再按框切片求和。

## 开关

`CAIRN_RED_MARK=0` 整层关闭（`is_enabled`）。默认开启；web 与 CLI 都先问它，
关掉时**不读图、不算像素**，行为回到加这个模块之前。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PIL import Image

# 整层开关：只有显式写 0 才关（缺省 = 开），与 CAIRN_OCR_DISABLE 的写法一致
ENV_DISABLE = "CAIRN_RED_MARK"

# 红色判据（见模块注释）：够亮 + 红通道明显压过绿/蓝
RED_MIN = 110
RED_DOMINANCE = 45

# 红像素占框面积的比例门槛（见模块注释的标定表）
RED_RATIO_THRESHOLD = 0.001

# 一个题框：(x0, y0, x1, y1)，与 `QuestionBlock.bbox` / 库里 bbox_json 同坐标系
Box = tuple[int, int, int, int]


def is_enabled() -> bool:
    """`CAIRN_RED_MARK=0` → 关闭；其余（含未设置）→ 开启。"""
    return os.environ.get(ENV_DISABLE, "1") != "0"


def _open(image: Image.Image | str | Path) -> Image.Image:
    """统一成已解码的 RGB 图（路径进来就地读盘，调用方不必关心）。"""
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    with Image.open(image) as handle:
        handle.load()
        return handle.convert("RGB")


def _red_mask(image: Image.Image) -> np.ndarray:
    """整页的布尔掩码：True = 该像素是红笔色。整页只算一次，按框切片复用。"""
    pixels = np.asarray(image.convert("RGB"), dtype=np.uint8)
    red = pixels[:, :, 0]
    green = pixels[:, :, 1]
    blue = pixels[:, :, 2]
    # uint8 直接相减会回绕（30 - 200 = 86），先升到 int16 再减
    return (
        (red > RED_MIN)
        & (np.subtract(red, green, dtype=np.int16) > RED_DOMINANCE)
        & (np.subtract(red, blue, dtype=np.int16) > RED_DOMINANCE)
    )


def _clip(box: Box, width: int, height: int) -> tuple[int, int, int, int] | None:
    """把框裁到图内；裁完没面积（越界/反向/退化框）返回 None。"""
    x0, y0, x1, y1 = box
    x0, x1 = max(0, min(int(x0), width)), max(0, min(int(x1), width))
    y0, y1 = max(0, min(int(y0), height)), max(0, min(int(y1), height))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def red_ratios(
    image: Image.Image | str | Path, boxes: list[Box] | tuple[Box, ...]
) -> list[float]:
    """逐框给出"红像素占该框面积的比例"（0~1）；坏框给 0.0，一个框都不漏。"""
    pixels = _open(image)
    if not boxes:
        return []
    mask = _red_mask(pixels)
    height, width = mask.shape
    ratios: list[float] = []
    for box in boxes:
        area = _clip(box, width, height)
        if area is None:
            ratios.append(0.0)
            continue
        x0, y0, x1, y1 = area
        window = mask[y0:y1, x0:x1]
        ratios.append(float(window.sum()) / float(window.size))
    return ratios


def detect_red_marks(
    image: Image.Image | str | Path,
    boxes: list[Box] | tuple[Box, ...],
    *,
    threshold: float = RED_RATIO_THRESHOLD,
) -> list[bool]:
    """逐框判"有没有红笔痕迹"：比例超阈值 → True。

    `image` 可以是 PIL 图，也可以是图片路径；`boxes` 用的是**这张图**的坐标。
    传路径时只解码一次，不会为每个框重开文件。

    只判有无：红色勾、红叉、红色分数都算 True（见模块注释）。
    """
    return [ratio > threshold for ratio in red_ratios(image, boxes)]


def detect_in_page(
    path: str | Path,
    boxes: list[Box] | tuple[Box, ...],
    angle: int = 0,
) -> list[bool]:
    """按"摆正后"坐标判红笔：web 用（库里存的 bbox 是摆正后的那套坐标）。

    `angle` 是 `read_page` 判出的"还需逆时针转多少度"；与 `orient._rotate`
    同一套操作（90° 倍数走 transpose，无插值），转出来的画布和切题时那张一致。
    读不了图（文件不在/损坏）就全按"没红笔"——标错页不能因为红笔检测 500。
    """
    if not boxes or not is_enabled():
        return [False] * len(boxes)
    try:
        image = _open(path)
    except OSError:
        return [False] * len(boxes)
    if angle:
        image = image.rotate(angle, expand=True, fillcolor="white")
    return detect_red_marks(image, boxes)
