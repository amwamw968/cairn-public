"""上传图自动"扫描化"：找纸张四边 → 透视矫正 → 去阴影 + 提对比。

为什么要这一步：同一页卷子，扫描类 App 处理过的图切出 10 题、题号 10/10 直接
认出；手机原生拍的图题号靠猜、框只圈到标题行。差别不在识别模型，在**输入像不
像一张平整的扫描件**——拍歪、纸面反光、阴影、背景桌面都在干扰版面分析。

这里只做图像本身的三步，不碰切题与方向判定（`cut.py` / `orient.py` 一行不改）：

1. `find_page_quad`：在缩略图上找纸张的四个角（Otsu 分纸 → 最大轮廓 →
   多边形逼近成四边形）。找不到（纯白、满幅文字、极暗、纸张顶到画面边且与
   背景同亮度）就返回 None，调用方**退回原图**；
2. `warp_page`：按四角做透视变换，把纸拉成正视图；
3. `enhance`：用大尺度模糊估一张光照图，除回去消阴影，再用 CLAHE 提局部对比。
   **保留颜色**（不二值化、不转灰度输出）——颜色信息留着，后续要接红笔检测
   也不用重跑一遍上游。

约定：处理失败绝不抛给上传路径。`preprocess_page` 把异常收成"没处理"，
`preprocess_image` 永远返回一张能用的图（失败时就是原图）。

输出长边上限 3000px（等比缩放，`PAGE_MAX_SIDE`）：手机原图 4000px 级别送进
OCR 只会更慢，3000 足够看清手写小字；小图不放大。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

# 输出长边上限：3000px 够看清卷面小字，又不至于让 OCR 拖成十几秒
PAGE_MAX_SIDE = 3000

# 找四边时先缩到这个尺寸上做（原图 4000px 直接做阈值/Canny 又慢又碎）
DETECT_MAX_SIDE = 1000

# 纸张至少要占画面这么多，否则认成"没找到纸"（远景、桌面占大半）
MIN_PAGE_AREA_RATIO = 0.15

# 纸几乎铺满整个画面（没有可见的纸边）时同样按"没找到边界"处理：
# 这类图本来就是裁好的，硬找一条边去扭反而会切掉卷面。
MAX_PAGE_AREA_RATIO = 0.97

# 整图平均亮度低于这个值 = 极暗（没开灯/镜头盖/纯黑），分不出纸和背景
MIN_FRAME_BRIGHTNESS = 45.0

# 亮度标准差低于这个值 = 几乎均匀（纯白页/纯色图），没有可用的边
MIN_FRAME_CONTRAST = 8.0

# 四边形的每条边都要有"纸比背景亮"的落差，否则找到的是纸面上的暗线（折痕、
# 阴影边）而不是纸边。8/255 是压得很低的门槛：真实纸边通常 30+，
# 白桌面上的白纸（<8）会被判成"没有边界"从而退回原图——那正是想要的结果。
MIN_EDGE_STEP = 8.0

# 增强参数：光照图模糊尺度（相对短边）、CLAHE 的对比增益与分块
ILLUMINATION_SIGMA_RATIO = 0.08
CLAHE_CLIP_LIMIT = 2.0
CLAHE_TILE = 8

# 处理结果存 JPEG 时的质量：与题块裁图同档，肉眼看不出损失
OUTPUT_JPEG_QUALITY = 92


@dataclass(frozen=True)
class PageQuad:
    """纸张四角，顺序固定为 左上 / 右上 / 右下 / 左下（便于比较与打印）。"""

    points: tuple[tuple[int, int], ...]
    area_ratio: float

    def as_list(self) -> list[list[int]]:
        return [[int(x), int(y)] for x, y in self.points]


@dataclass(frozen=True)
class PreprocessResult:
    """一张图的处理结果。

    - `image`：处理后的图；`changed=False` 时就是**原图对象本身**（未重采样）；
    - `quad`：找到的纸张四角（原图坐标）；没找到为 None；
    - `seconds`：整条处理链的耗时，CLI 与 EVIDENCE 都看这个。
    """

    image: Image.Image
    changed: bool
    quad: PageQuad | None
    seconds: float
    source_size: tuple[int, int]
    size: tuple[int, int]


def _as_bgr(image: Image.Image) -> np.ndarray:
    """PIL → BGR ndarray（cv2 的通道序）；透明图铺白底，免得透明处变黑。"""
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        canvas = Image.new("RGB", image.size, "white")
        canvas.paste(image, mask=image.split()[-1])
        image = canvas
    elif image.mode != "RGB":
        image = image.convert("RGB")
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def _as_pil(bgr: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


def _order_corners(points: np.ndarray) -> np.ndarray:
    """把四个点排成 左上 / 右上 / 右下 / 左下。

    用"x+y 最小是左上、最大是右下；y-x 最小是右上、最大是左下"这套判据，
    比按角度排序稳：近似四边形偶尔会有一点凹凸，角度排序会来回翻。
    """
    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    sums = pts.sum(axis=1)
    diffs = (pts[:, 1] - pts[:, 0])
    ordered = np.empty((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(sums)]  # 左上
    ordered[2] = pts[np.argmax(sums)]  # 右下
    ordered[1] = pts[np.argmin(diffs)]  # 右上
    ordered[3] = pts[np.argmax(diffs)]  # 左下
    return ordered


def _quad_from_contour(contour: np.ndarray, frame_area: float) -> PageQuad | None:
    """一条轮廓 → 四边形（面积比例合格、四条边都不退化才要）。"""
    perimeter = cv2.arcLength(contour, True)
    if perimeter <= 0:
        return None
    for epsilon in (0.02, 0.04, 0.06):
        approx = cv2.approxPolyDP(contour, epsilon * perimeter, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        area_ratio = float(cv2.contourArea(approx)) / frame_area
        if not MIN_PAGE_AREA_RATIO <= area_ratio <= MAX_PAGE_AREA_RATIO:
            continue
        ordered = _order_corners(approx)
        sides = [
            float(np.hypot(*(ordered[index] - ordered[(index + 1) % 4])))
            for index in range(4)
        ]
        if min(sides) < 20.0:
            continue  # 退化成一条线/一个点，不是纸
        return PageQuad(
            points=tuple((int(x), int(y)) for x, y in ordered),
            area_ratio=area_ratio,
        )
    return None


def _has_clear_page_edges(
    gray: np.ndarray, points: tuple[tuple[int, int], ...], offset: int = 8
) -> bool:
    """四条边上都得有"里亮外暗"的落差，才认这是纸边。

    只做分割不做校验会踩一个真坑：纸面被阴影斜切一刀时，最亮的那块往往止于
    阴影分界线，而不是纸边——照着它去矫正，会把纸下半截整块切掉（paper-01/
    paper-02 真图就是这样：下边落差 4/0，只有纸内部亮度突变，没有纸边）。

    沿每条边取 9 个采样点，比较"往纸内 8px"与"往纸外 8px"的灰度中位数：
    全部四条边都 ≥ `MIN_EDGE_STEP` 才通过。越界的采样直接跳过（纸顶着画面边
    = 没有可见纸边，按约定本来就不处理）；某条边一个采样都取不到也算不通过。
    """
    height, width = gray.shape
    pts = np.asarray(points, dtype=np.float32)
    center = pts.mean(axis=0)
    for index in range(4):
        start, end = pts[index], pts[(index + 1) % 4]
        diffs: list[float] = []
        for ratio in np.linspace(0.15, 0.85, 9):
            point = start + (end - start) * ratio
            inward = center - point
            norm = float(np.linalg.norm(inward))
            if norm < 1e-6:
                continue
            step = inward / norm * offset
            inside = np.round(point + step).astype(int)
            outside = np.round(point - step).astype(int)
            if not (
                0 <= inside[0] < width
                and 0 <= inside[1] < height
                and 0 <= outside[0] < width
                and 0 <= outside[1] < height
            ):
                continue
            diffs.append(float(gray[inside[1], inside[0]]) - float(gray[outside[1], outside[0]]))
        if not diffs or float(np.median(diffs)) < MIN_EDGE_STEP:
            return False
    return True


def find_page_quad(image: Image.Image | np.ndarray) -> PageQuad | None:
    """找纸张的四条边；找不到（或压根没法判）返回 None。

    做法：缩到 1000px 以内 → 灰度 → Otsu 分出"纸（亮）"和"背景（暗）"→
    闭运算把字缝填上 → 取最大轮廓 → 多边形逼近成四边形。

    走亮度分割而不是纯边缘检测，是因为卷面上密密麻麻的字会被 Canny 认成
    几百条边，反而拼不出"纸"这个整体；而纸一定是画面里最大的一块亮区。

    以下几种情况明确放弃（返回 None，由调用方退回原图）：

    - 整图极暗/几乎均匀（纯白页、纯黑、纯色）；
    - 最大亮区太小（< 画面 15%）或几乎铺满（> 97%，没有可见的纸边）；
    - 亮区不是一个像样的凸四边形（满幅文字的碎块拼不出四角）；
    - 四边形的边没有"里亮外暗"的落差（见 `_has_clear_page_edges`：这种多半是
      纸面上的阴影分界线，照着它矫正会把纸切掉一块）。

    返回的坐标是**原图**坐标：在缩略图上找到的点会按缩放比换算回去。
    """
    bgr = image if isinstance(image, np.ndarray) else _as_bgr(image)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    if height < 20 or width < 20:
        return None
    if float(gray.mean()) < MIN_FRAME_BRIGHTNESS:
        return None
    if float(gray.std()) < MIN_FRAME_CONTRAST:
        return None

    scale = min(1.0, DETECT_MAX_SIDE / max(height, width))
    if scale < 1.0:
        small = cv2.resize(
            gray,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = gray
    small_h, small_w = small.shape

    blurred = cv2.GaussianBlur(small, (5, 5), 0)
    _threshold, paper = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    # 闭运算把字缝/折痕填上，开运算去掉纸边外的小噪点；核随图大小走（约 1%）
    kernel_size = max(3, int(round(min(small_h, small_w) * 0.01)) | 1)
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    paper = cv2.morphologyEx(paper, cv2.MORPH_CLOSE, kernel)
    paper = cv2.morphologyEx(paper, cv2.MORPH_OPEN, kernel)

    contours, _hierarchy = cv2.findContours(
        paper, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return None
    frame_area = float(small_h * small_w)
    edge_offset = max(4, int(round(min(height, width) * 0.01)))
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        candidate = _quad_from_contour(contour, frame_area)
        if candidate is None:
            continue
        if scale < 1.0:
            points = tuple(
                (int(round(x / scale)), int(round(y / scale)))
                for x, y in candidate.points
            )
        else:
            points = candidate.points
        clamped = tuple(
            (min(max(x, 0), width - 1), min(max(y, 0), height - 1)) for x, y in points
        )
        # 形状像四边形还不够：四条边得真有"纸/背景"落差，见 `_has_clear_page_edges`
        if _has_clear_page_edges(gray, clamped, offset=edge_offset):
            return PageQuad(points=clamped, area_ratio=candidate.area_ratio)
    return None


def _target_size(quad: PageQuad) -> tuple[int, int]:
    """按四角算正视图的宽高：对边长度的较大值，保证不丢分辨率。"""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = quad.points
    top = float(np.hypot(x1 - x0, y1 - y0))
    bottom = float(np.hypot(x2 - x3, y2 - y3))
    left = float(np.hypot(x3 - x0, y3 - y0))
    right = float(np.hypot(x2 - x1, y2 - y1))
    width = int(round(max(top, bottom)))
    height = int(round(max(left, right)))
    return max(width, 1), max(height, 1)


def warp_page(image: Image.Image, quad: PageQuad) -> Image.Image:
    """按四角做透视矫正，把纸拉成正视图（颜色不变、不裁内容）。"""
    bgr = _as_bgr(image)
    width, height = _target_size(quad)
    source = np.asarray(quad.points, dtype=np.float32)
    target = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(source, target)
    warped = cv2.warpPerspective(
        bgr,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return _as_pil(warped)


def enhance(image: Image.Image) -> Image.Image:
    """去阴影 + 提局部对比，**保留颜色**。

    阴影是"低频的光照不均"：把图缩小后大幅模糊得到光照图，逐像素除回去，
    纸面就回到接近均匀的白色；然后对 LAB 的 L 通道做 CLAHE，把局部对比拉起来
    （铅笔字、浅色印刷都能变清楚）。a/b 两个颜色通道原样保留，输出仍是彩色。

    缩小再模糊是为了快：在 3000px 原尺寸上做大核高斯要几百毫秒，而光照图本来
    就是低频信息，1/4 尺寸上估完放大回来肉眼无差。
    """
    bgr = _as_bgr(image)
    height, width = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    small = cv2.resize(
        gray, (max(1, width // 4), max(1, height // 4)), interpolation=cv2.INTER_AREA
    )
    sigma = max(3.0, min(small.shape) * ILLUMINATION_SIGMA_RATIO)
    background = cv2.GaussianBlur(small, (0, 0), sigmaX=sigma, sigmaY=sigma)
    background = cv2.resize(background, (width, height), interpolation=cv2.INTER_LINEAR)

    # 除以光照图：背景暗的地方提得多，阴影被拉平；下限 32 兜住纯黑区，
    # 免得把它放大成噪点雪花。
    gain = 255.0 / np.maximum(background.astype(np.float32), 32.0)
    lifted = np.clip(bgr.astype(np.float32) * gain[..., None], 0, 255).astype(np.uint8)

    lab = cv2.cvtColor(lifted, cv2.COLOR_BGR2LAB)
    lightness, channel_a, channel_b = cv2.split(lab)
    clahe = cv2.createCLAHE(
        clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=(CLAHE_TILE, CLAHE_TILE)
    )
    lightness = clahe.apply(lightness)
    merged = cv2.merge((lightness, channel_a, channel_b))
    return _as_pil(cv2.cvtColor(merged, cv2.COLOR_LAB2BGR))


def _limit_long_side(image: Image.Image, max_side: int = PAGE_MAX_SIDE) -> Image.Image:
    """长边超限就等比缩下来；不超过就**原样返回**（小图不放大）。"""
    width, height = image.size
    longest = max(width, height)
    if longest <= max_side:
        return image
    ratio = max_side / longest
    return image.resize(
        (max(1, int(round(width * ratio))), max(1, int(round(height * ratio)))),
        Image.LANCZOS,
    )


def _open_source(source: str | Path | Image.Image) -> Image.Image:
    """统一入口：路径就打开（按 EXIF 摆正），是图就直接用。"""
    if isinstance(source, Image.Image):
        image = source
    else:
        image = Image.open(source)
    image = ImageOps.exif_transpose(image)
    image.load()
    return image


def preprocess_page(source: str | Path | Image.Image) -> PreprocessResult:
    """完整链路：找边 → 透视矫正 → 去阴影提对比 → 限长边。

    找不到纸张四边时**不抛异常**，原样把原图交回去（`changed=False`）：上传这
    条路宁可少一步美化，也不能让家长看到报错。
    """
    started = time.perf_counter()
    original = _open_source(source)
    source_size = original.size
    try:
        quad = find_page_quad(original)
    except Exception:  # noqa: BLE001 —— 找边失败等同没找到，绝不往上抛
        quad = None
    if quad is None:
        return PreprocessResult(
            image=original,
            changed=False,
            quad=None,
            seconds=time.perf_counter() - started,
            source_size=source_size,
            size=original.size,
        )
    final = _limit_long_side(enhance(warp_page(original, quad)))
    return PreprocessResult(
        image=final,
        changed=True,
        quad=quad,
        seconds=time.perf_counter() - started,
        source_size=source_size,
        size=final.size,
    )


def preprocess_image(source: str | Path | Image.Image) -> Image.Image:
    """对外契约：给一张图（路径或 PIL 图），拿一张能直接送去 OCR 的图。

    处理失败/找不到纸张边界时返回原图——调用方不需要判空、也不会被异常打断。
    """
    return preprocess_page(source).image


def save_processed(
    source: str | Path | Image.Image,
    target: str | Path,
    *,
    quality: int = OUTPUT_JPEG_QUALITY,
) -> bool:
    """处理并存成 JPEG；**没找到纸张边界就不写盘**，返回是否写成功。

    不写盘是有意的：`scan.processed_path` 留空 = 读图链路回落原图，
    「处理过」这件事只以文件存在为准，不需要额外的状态列。
    """
    result = preprocess_page(source)
    if not result.changed:
        return False
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    result.image.save(path, format="JPEG", quality=quality)
    return True
