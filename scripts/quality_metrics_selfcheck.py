"""前端质量指标算法的 Python 复算自证（上传通道卡，验收 3）。

与 `src/cairn/templates/upload.html` 的 `computeMetrics()` 逐行同构：
短边缩 512（这里直接按目标尺寸生成图）、BT.601 灰度、3×3 拉普拉斯方差、
Sobel 幅值 > 100 记边缘。生成三张图对比 blur_var 量级，证明阈值 60 的量级
 separation 不是拍脑袋：

    图 1  清晰文字图（白底黑字块，模拟拍清楚的作业页）
    图 2  同图大半径盒模糊（模拟拍糊了）
    图 3  全灰图（均匀 128，糊到没有任何结构）

纯标准库（venv 无 PIL），运行：.venv/bin/python scripts/quality_metrics_selfcheck.py
"""

from __future__ import annotations

import math
import random

SHORT_SIDE = 512
GRAY_DARK, GRAY_BRIGHT = 60, 245  # 与 upload.html 常量一致
EDGE_GRAD = 100


def make_text_page(width: int, height: int) -> list[list[int]]:
    """白底 + 随机宽高的黑色文字块横条，模拟一张拍清楚的作业页。"""
    rnd = random.Random(42)
    px = [[255] * width for _ in range(height)]
    y = 8
    while y < height - 8:
        x = 8
        line_h = rnd.randint(6, 10)
        while x < width - 8:
            if rnd.random() < 0.75:  # 词间空隙留白
                bw = rnd.randint(4, 28)
                for yy in range(y, min(y + line_h, height)):
                    for xx in range(x, min(x + bw, width)):
                        px[yy][xx] = 0
            x += bw + rnd.randint(3, 10)
        y += line_h + rnd.randint(6, 14)
    return px


def box_blur(px: list[list[int]], radius: int) -> list[list[int]]:
    """简易盒模糊，模拟手抖拍糊。"""
    h, w = len(px), len(px[0])
    out = [[0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            acc = n = 0
            for yy in range(max(0, y - radius), min(h, y + radius + 1)):
                row = px[yy]
                for xx in range(max(0, x - radius), min(w, x + radius + 1)):
                    acc += row[xx]
                    n += 1
            out[y][x] = acc // n
    return out


def metrics(px: list[list[int]]) -> dict[str, float]:
    """与 upload.html computeMetrics 同构（单通道灰度即 R=G=B 的灰度图口径）。"""
    h, w = len(px), len(px[0])
    gray = [p for row in px for p in row]
    n = len(gray)
    brightness = sum(gray) / n
    dark = sum(1 for g in gray if g < GRAY_DARK) / n
    bright = sum(1 for g in gray if g > GRAY_BRIGHT) / n

    lv_sum = lv_sq = cnt = edges = 0
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            i = y * w + x
            v = 4 * gray[i] - gray[i - 1] - gray[i + 1] - gray[i - w] - gray[i + w]
            lv_sum += v
            lv_sq += v * v
            cnt += 1
            gx = (-gray[i - w - 1] - 2 * gray[i - 1] - gray[i + w - 1]
                  + gray[i - w + 1] + 2 * gray[i + 1] + gray[i + w + 1])
            gy = (-gray[i - w - 1] - 2 * gray[i - w] - gray[i - w + 1]
                  + gray[i + w - 1] + 2 * gray[i + w] + gray[i + w + 1])
            if math.sqrt(gx * gx + gy * gy) > EDGE_GRAD:
                edges += 1
    lv_mean = lv_sum / cnt
    return {
        "brightness": round(brightness, 2),
        "dark_ratio": round(dark, 4),
        "overexposure_ratio": round(bright, 4),
        "blur_var": round(lv_sq / cnt - lv_mean * lv_mean, 2),
        "edge_density": round(edges / cnt, 4),
    }


if __name__ == "__main__":
    w, h = 680, SHORT_SIDE
    text_page = make_text_page(w, h)
    cases = [
        ("图1 清晰文字图", text_page),
        ("图2 文字图盒模糊 r=8（拍糊）", box_blur(text_page, 8)),
        ("图3 全灰图（均匀 128，全糊）", [[128] * w for _ in range(h)]),
    ]
    for name, img in cases:
        print(name, metrics(img))
