"""增强这一步：把纸压白、把字压黑（摆正之后的第二道）。

为什么不并进 `preprocess`：摆正解决的是"拍歪、有阴影、背景脏"，输出仍然是
一张**照片**——纸是浅灰、铅笔字是深灰。切题与 OCR 真正吃得下的是"扫描件"：
纸接近纯白、字接近纯黑、两者之间还留着抗锯齿的过渡。这是两件事，参数也互相
牵扯（摆正按几何算，增强按本页的灰度分布算），所以单独一个模块、单独一个工具
开关（`CAIRN_TOOL_ENHANCE`）。

四步，顺序不能换：

1. **估背景场** `estimate_background`：缩到 1/8 → `medianBlur(15)`（把字、格线、
   印章这些"比纸小得多"的东西从背景里剔掉）→ 高斯 σ3（抹平中值留下的块状台阶）
   → 放大回原尺寸。得到的是一张"如果这页没有字，纸面该有多亮"的低频图。
2. **除法归一** `_normalise`：`I / bg * 244`。纸面暗的地方提得多、亮的地方提得少，
   阴影和反光一起被拉平；乘 244 而不是 255，留一点余量，纯白不刺眼、也不会把
   抗锯齿的过渡层直接截掉。**不裁边、不改几何**——宽高和输入逐像素对齐。
3. **黑白点写死 6 / 244**：`build_lut` 用一对固定阈值生成 256 项查找表。
   上一版按本页 `p1/p95` 自适应，实测在三张真卷子上各掉一道题（paper-03 只剩
   17 块、paper-04 剩 9–11 块且混进假块）——分位数会被"这一页有多少空白"牵着走，
   窄边距的卷子把白点压到 200 上下，格线和题干一起被推黑，切题就断。写死之后
   三张真图分别是 18 / 10 / 18 块。**这是实测结论，不是理论选择**，别再改回去。
4. **s 曲线 + 分档锐化**：s 曲线（0.12）把中灰往两头推，但保留 8–16 级的过渡带，
   不做二值化——OCR 靠这条过渡带认出笔画边界。锐化按档分开：给切题/OCR 的
   **gray 档默认不锐化**（unsharp 会把笔画边上拉出亮边，OCR 的行框跟着抖，
   paper-04 上 0.55 → 8 块、0 → 11 块），给家长看的 color 档保留 0.55。

彩色档用**同一个背景场、同一条 LUT** 逐通道套用：三通道除的是同一张背景、过的是
同一条曲线，笔迹的色相（红笔、蓝笔、荧光笔）就不会被拉偏，纸质偏黄也不会被
"校"成白的。

输出长边上限 2560（`LONG_SIDE`）：手机原图 4000px 级送进来只是更慢，
2560 已经够看清手写小字；小图不放大。

约定：这里只做图像变换，不写盘、不判方向、不抛"业务异常"；读写文件的两个小函数
（`save_gray`）由调用方兜异常——上传这条路不许被增强挡住。
"""

from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

# 输出长边上限：与原型一致（2560 看得清手写小字，又不会让中值滤波拖慢）
LONG_SIDE = 2560

# 背景估计：缩到 1/8 → 中值 15（剔除字与格线）→ 高斯 σ3（抹平中值的块状感）
BG_FACTOR = 8
BG_MEDIAN = 15
BG_SIGMA = 3.0

# 除法归一的目标纸白：244 而不是 255，留余量给抗锯齿过渡
PAPER_LEVEL = 244.0

# 黑白点写死：黑点 6、白点 244（实测三张真卷子唯一全中的一组，见模块注释第 3 条）
BLACK_LEVEL = 6.0
WHITE_LEVEL = 244.0

# s 曲线强度：往两头推，但保留过渡带（压成纯二值反而掉字）
S_CURVE = 0.12

# 锐化分档：gray 档（切题/OCR 用）默认 0.0，color 档（给人看）0.55；σ 共用
GRAY_SHARPEN_AMOUNT = 0.0
COLOR_SHARPEN_AMOUNT = 0.55
SHARPEN_SIGMA = 1.0

# 存灰度图用的 JPEG 质量：与 preprocess / 题块裁图同档
OUTPUT_JPEG_QUALITY = 92


def _limit_long_side(bgr: np.ndarray, long_side: int = LONG_SIDE) -> np.ndarray:
    """长边超限就等比缩下来；不超过就原样返回（小图不放大）。"""
    height, width = bgr.shape[:2]
    longest = max(height, width)
    if longest <= long_side:
        return bgr
    ratio = long_side / longest
    return cv2.resize(
        bgr,
        (max(1, int(round(width * ratio))), max(1, int(round(height * ratio)))),
        interpolation=cv2.INTER_AREA,
    )


def estimate_background(
    gray: np.ndarray,
    *,
    factor: int = BG_FACTOR,
    median: int = BG_MEDIAN,
    sigma: float = BG_SIGMA,
) -> np.ndarray:
    """估这页的纸面亮度场（与 `gray` 同尺寸、同 dtype uint8）。

    缩小再滤波是为了既快又稳：1/8 尺寸上做 15×15 中值，等于原尺寸 120px 的窗口，
    字和格线整块消失，只剩"纸在哪、哪块被阴影压暗"；放大回来是低频的，肉眼无差。
    """
    height, width = gray.shape
    small = cv2.resize(
        gray,
        (max(8, width // factor), max(8, height // factor)),
        interpolation=cv2.INTER_AREA,
    )
    small = cv2.medianBlur(small, median | 1)  # 中值核必须是奇数
    small = cv2.GaussianBlur(small, (0, 0), sigma)
    return cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)


def _normalise(channel: np.ndarray, background: np.ndarray) -> np.ndarray:
    """`channel / bg * 244`，夹在 0–255，返回 float32 的"归一化灰度场"。"""
    field = np.maximum(background.astype(np.float32), 1.0)
    return np.clip(
        channel.astype(np.float32) / field * PAPER_LEVEL, 0.0, 255.0
    )


def build_lut(
    black: float = BLACK_LEVEL,
    paper: float = WHITE_LEVEL,
    *,
    s_curve: float = S_CURVE,
) -> np.ndarray:
    """黑点以下压到 0、白点以上拉到 255，中间做一条温和的 s 曲线。

    `y = clip((x - black) / (paper - black), 0, 1)` 是主映射：默认取模块常量
    6 / 244，跟这一页的灰度分布无关（固定阈值是实测选出来的，见模块注释）。
    s 曲线 `y + k*(y-0.5)*(1-|2y-1|)` 在两端几乎不动、在中段加斜率——
    抗锯齿的过渡层因此不会被一刀切成纯黑或纯白。
    """
    x = np.arange(256, dtype=np.float32) / 255.0
    span = max(1e-6, paper - black)
    y = np.clip((x * 255.0 - black) / span, 0.0, 1.0)
    y = y + s_curve * (y - 0.5) * (1.0 - np.abs(2.0 * y - 1.0))
    return (np.clip(y, 0.0, 1.0) * 255.0).astype(np.uint8)


def _sharpen(
    image: np.ndarray,
    *,
    amount: float,
    sigma: float = SHARPEN_SIGMA,
) -> np.ndarray:
    """unsharp：`图 * (1 + amount) - 模糊 * amount`（`addWeighted` 自带饱和）。

    `amount <= 0` 原样返回——gray 档就是这样"零锐化"的，不调 `GaussianBlur`。
    """
    if amount <= 0.0:
        return image
    blurred = cv2.GaussianBlur(image, (0, 0), sigma)
    return cv2.addWeighted(image, 1.0 + amount, blurred, -amount, 0)


def enhance_page(
    bgr: np.ndarray,
    long_side: int = LONG_SIDE,
    *,
    sharpen: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """一页增强 → `(gray, color)`，两张都是 uint8、几何一致、长边 ≤ `long_side`。

    - `gray`：单通道，给切题/OCR 用（也是落盘的 `processed_path`），**默认不锐化**；
    - `color`：三通道 BGR，同一个背景场、同一条 LUT 逐通道套，笔迹色相不变，
      默认按 `COLOR_SHARPEN_AMOUNT` 锐化。

    `sharpen=None` 表示按档取默认；给了数就两档都按这个数（`0` 等于都关掉），
    测试与调参用这一路，不必改常量。

    输入是 OpenCV 的 BGR 数组（不是 PIL、不是 RGB）——调用方负责转换，
    这样 `scan_service` 与 `ocr.preprocess` 共用同一条通道序，不会一半 BGR 一半 RGB。
    """
    bgr = _limit_long_side(bgr, long_side)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    background = estimate_background(gray)
    flat = _normalise(gray, background)
    lut = build_lut()

    out_gray = cv2.LUT(flat.astype(np.uint8), lut)

    # 彩色档：同一张背景场、同一条 LUT，逐通道套（不是把灰度图直接扩成三通道）
    channels = [
        cv2.LUT(_normalise(channel, background).astype(np.uint8), lut)
        for channel in cv2.split(bgr)
    ]
    out_color = cv2.merge(channels)

    if sharpen is None:
        return (
            _sharpen(out_gray, amount=GRAY_SHARPEN_AMOUNT),
            _sharpen(out_color, amount=COLOR_SHARPEN_AMOUNT),
        )
    return _sharpen(out_gray, amount=sharpen), _sharpen(out_color, amount=sharpen)


def _as_bgr(image: Image.Image) -> np.ndarray:
    """PIL → BGR ndarray；透明图铺白底（透明处不该变成黑块）。"""
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        canvas = Image.new("RGB", image.size, "white")
        canvas.paste(image, mask=image.split()[-1])
        return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)
    if image.mode != "RGB":
        image = image.convert("RGB")
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def enhance_image(
    source: str | Path | Image.Image, long_side: int = LONG_SIDE
) -> tuple[Image.Image, Image.Image]:
    """PIL 进、`(灰度 PIL, 彩色 PIL)` 出，给需要 PIL 的调用方用。

    路径先整个读进内存再解码：允许"源和目标同一个文件"（上传链路就是拿
    `<uuid>.processed.jpg` 原地换成增强灰度），不会读到写了一半的自己。
    """
    if isinstance(source, Image.Image):
        image = source
    else:
        data = Path(source).read_bytes()
        image = Image.open(io.BytesIO(data))
    with_image = ImageOps.exif_transpose(image)
    with_image.load()
    gray, color = enhance_page(_as_bgr(with_image), long_side)
    return (
        Image.fromarray(gray, mode="L"),
        Image.fromarray(cv2.cvtColor(color, cv2.COLOR_BGR2RGB), mode="RGB"),
    )


def save_gray(
    source: str | Path | Image.Image,
    target: str | Path,
    *,
    long_side: int = LONG_SIDE,
    quality: int = OUTPUT_JPEG_QUALITY,
) -> Path:
    """增强成**灰度**并存成 JPEG，返回写下的路径（失败往上抛，由调用方兜）。

    只落灰度：切题/OCR/叠框用的是同一张，彩色档目前只给预览留口子
    （真需要时调 `enhance_image` 拿第二张，不必重跑一遍算子）。
    """
    path = Path(target)
    gray, _color = enhance_image(source, long_side)
    path.parent.mkdir(parents=True, exist_ok=True)
    gray.save(path, format="JPEG", quality=quality)
    return path
