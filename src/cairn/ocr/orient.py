"""整页方向判定与旋转校正（OCR 通道第三步：先判方向，再校小角度）。

家长侧拍/倒拍很常见：整页躺倒时文字基线在图像里是**竖着**走的，检测框变成
竖条，切题连题号行都找不到（paper-04 实测 0 题；逆时针 90° 摆正后 7 题）。

`detect_orientation` 返回的角度含义：**还需要逆时针转多少度才能摆正**
（`orient` 用 PIL `rotate(angle)`，正值即逆时针），取值 {0, 90, 180, 270}。

## 判定分三段，越往后越贵

1. **廉价预判（零 OCR）**：只读图片尺寸。宽高比给的先验是"竖图多半是
   0/180，横图多半是 90/270"，但这条先验**在真图上有一半不成立**：
   - `paper-03`（2560×1098 横图）需要 0°：它是并排两页的横向跨页；
   - `paper-01`/`paper-02`（549×1280 竖图）内容却是躺倒的（需要 90/270°）。
   所以它不排除任何角度，也不决定探哪条轴（多探一条轴只是白花一次探针）。
   它只用于"同一轴上两个方向分数完全相等"时的排序（见 `_candidates`）。
   定轴交给下面这条几何判据。
2. **低分辨率探针（长边 900px）**：只看行框几何、不看文字，回答"轴对不对"。
   文字横排时行框宽 ≫ 高（4 张真图 × 4 个角度实测：横排行占比 0.96~1.00，
   宽/高中位数 3~5）；整页躺倒时行框成了竖条（占比 0.00，中位数 0.25）。
   两个分布完全不重叠，所以轴判定用几何就够，一遍探针约 0.5~0.8s。
   **一遍 0° 探针就够定轴**：旋转 180° 不改变任何行框的朝向，所以 0° 探到
   横排就只剩 {0,180}；0° 是竖条就只剩 {90,270}（再探一次 90° 确认那条轴上
   真有横排文字，两轴都没有就说明是空白页/纯图页，不转）。
3. **全分辨率对比（只对选中轴上的 2 个方向做）**：同一轴的两个方向（0 vs 180、
   90 vs 270）几何完全一样，只能用文字分数量化：

       score = 行数 × 平均置信度 × 有效字符占比

   有效字符 = 汉字 / 字母数字 / 常见标点；OCR 出噪声时这个比例会掉。真图实测
   （全分辨率）：paper-03 0°=74.9 vs 180°=61.0；paper-04 90°=82.9 vs 270°=54.5，
   两页都选中正确方向。

## 为什么"像中文行的比例"不进打分

实测它分不开方向：paper-03 正向 0.735、倒置 0.754（倒置还略高）。原因是
RapidOCR 会按框角度把裁剪图转正、再用 cls 修 180°，所以躺倒/倒置的页面照样
读得出一片中文。故它只作为打分公式里的第三个因子（压噪声页），不单独用来判方向。
真正拉开方向差距的是**行数**：方向对时检测器吐得更全。

## 与 deskew 的顺序

先 orient（整页 90° 的倍数），再 deskew（±5° 小角度）。理由见 `deskew.py`：
deskew 用行框底边估倾斜，整页躺倒时底边仍是水平的，实测估计值恒为 0.00，
它对付不了 90°；先把整页摆正，±5° 的基线角度才有意义。

## 缓存

判定结果按 (路径, mtime, 大小) 缓存在进程内：`detect_orientation` → `orient`
→ `read_page` 是同一个调用方常见的三步，缓存让一整页只 OCR 一遍。
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image

from cairn.ocr.engine import ENV_DISABLE, OcrError, OcrLine, recognize

# 探针缩到的最长边：900px 时行框几何已经和全分辨率一致（实测横/竖分布不重叠），
# 单次探针 0.5~0.8s，比全分辨率（2560px，约 1.0s）省一半并且随尺寸线性缩放
PROBE_LONG_SIDE = 900
# 行框"宽/高 ≥ 1.5"算横排一行；真图横排行占比 0.96~1.00、竖条行 0.00
HORIZONTAL_BOX_RATIO = 1.5
# 横排行占比达到该值 → 文字在图像 x 方向排开，躺倒假设可以排除
HORIZONTAL_PAGE_RATIO = 0.8
# 探针至少这么多行才承认"这条轴有文字"（1~2 行多半是噪声）
PROBE_MIN_LINES = 3

# 缓存条目上限：一页判定结果（含全部识别行）不小，只留最近几页
_CACHE_MAX = 4


@dataclass(frozen=True)
class OrientedPage:
    """摆正后的一页：图 + 判定角度 + 得分 + 该图上的识别行。

    `image` 与 `lines` 是同一坐标系：angle=0 时就是原图（未重采样），
    非 0 时是 `原图.rotate(angle, expand=True)`（90° 倍数走 transpose 快路径，
    无插值），`lines` 是在这张图上重新识别的结果。
    """

    image: Image.Image | None
    angle: int
    score: float
    lines: list[OcrLine]

    @property
    def width(self) -> int:
        return 0 if self.image is None else self.image.width

    @property
    def height(self) -> int:
        return 0 if self.image is None else self.image.height


@dataclass(frozen=True)
class _Probe:
    """低分辨率探针结果：该角度下的行 + 横排行占比。"""

    lines: list[OcrLine]
    horizontal_ratio: float


@dataclass(frozen=True)
class _Decision:
    """一页的方向判定结论（进缓存的形态）。"""

    angle: int
    score: float
    lines: tuple[OcrLine, ...]


_CACHE: dict[tuple[str, int, int], _Decision] = {}


def _is_readable(ch: str) -> bool:
    """汉字 / 字母数字 / 常见标点算"有效字符"。"""
    return (
        "\u4e00" <= ch <= "\u9fff"
        or ch.isalnum()
        or ch in "，。、；：！？（）()“”‘’《》〈〉【】—…·+-×÷=%.,:/"
    )


def text_score(lines: list[OcrLine]) -> float:
    """方向打分：行数 × 平均置信度 × 有效字符占比（三者的乘积，越大越像正常卷面）。

    单一指标都不可靠：倒置页也能读出一片中文（平均置信度、有效字符占比都高），
    但检测器吐出的行数会明显变少；反过来噪声页行数不少、置信度低。乘起来才稳。
    """
    if not lines:
        return 0.0
    total_chars = sum(len(line.text) for line in lines)
    if total_chars == 0:
        return 0.0
    good = sum(1 for line in lines for ch in line.text if _is_readable(ch))
    mean_confidence = sum(line.confidence for line in lines) / len(lines)
    return len(lines) * mean_confidence * (good / total_chars)


def _horizontal_ratio(lines: list[OcrLine]) -> float:
    """行框宽/高 ≥ 1.5 的行占比：文字横排时接近 1，躺倒时接近 0。"""
    ratios: list[float] = []
    for line in lines:
        xs = [x for x, _ in line.box]
        ys = [y for _, y in line.box]
        height = max(ys) - min(ys)
        if height <= 0:
            continue
        ratios.append((max(xs) - min(xs)) / height)
    if not ratios:
        return 0.0
    return sum(1 for ratio in ratios if ratio >= HORIZONTAL_BOX_RATIO) / len(ratios)


def _candidates(path: str, image: Image.Image) -> tuple[int, int] | None:
    """用一遍 0° 探针定出"轴"，返回该轴上的两个候选角度（顺序 = 平分时先试谁）。

    旋转 180° 不改变行框朝向，所以：
    - 0° 探到横排 → 只剩 {0, 180}；
    - 0° 是竖条 → 只剩 {90, 270}（180° 必然也是竖条）；再探一次 90° 确认
      那条轴上真有横排文字，两条轴都没有就返回 None（空白页/纯图页）。
    宽高比只在这里当平分时的排序（真图上它不足以判方向，见模块注释）。
    """
    if _has_horizontal_text(_probe(path, image, 0)):
        return (0, 180)
    if not _has_horizontal_text(_probe(path, image, 90)):
        return None
    return (90, 270) if image.height >= image.width else (270, 90)


def _has_horizontal_text(probe: _Probe) -> bool:
    """这条轴上算不算"发现了横排文字"：行数够 + 横排行占比够。"""
    return (
        len(probe.lines) >= PROBE_MIN_LINES
        and probe.horizontal_ratio >= HORIZONTAL_PAGE_RATIO
    )


def _open_image(path: str) -> Image.Image:
    """打开并解码；失败归一成 OcrError（web 层可直接展示）。"""
    try:
        image = Image.open(path)
        image.load()
    except OSError as exc:
        raise OcrError(f"图片无法读取（解码失败）：{exc}") from exc
    return image


def _rotate(image: Image.Image, angle: int) -> Image.Image:
    """逆时针转 angle 度。90 的整数倍由 PIL 走 transpose 快路径，不插值。"""
    if not angle:
        return image
    return image.rotate(angle, expand=True, fillcolor="white")


def _downscale(image: Image.Image) -> Image.Image:
    """探针用的小图：长边缩到 PROBE_LONG_SIDE，不够小就原样返回（同一对象）。"""
    long_side = max(image.size)
    if long_side <= PROBE_LONG_SIDE:
        return image
    scale = PROBE_LONG_SIDE / long_side
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )


@contextmanager
def _temp_path(image: Image.Image) -> Iterator[str]:
    """识别入口只吃路径，旋转/缩放后的图先落临时文件，用完即删。"""
    handle, name = tempfile.mkstemp(prefix="cairn-orient-", suffix=".png")
    os.close(handle)
    try:
        image.save(name, format="PNG")
        yield name
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def _probe(path: str, image: Image.Image, angle: int) -> _Probe:
    """低分辨率探针：只回答"这个角度下行框是横排还是竖条"。"""
    probe_image = _downscale(_rotate(image, angle))
    if angle == 0 and probe_image is image:
        lines = recognize(path)  # 原图本来就够小，省一次落盘
    else:
        with _temp_path(probe_image) as temp:
            lines = recognize(temp)
    return _Probe(lines=lines, horizontal_ratio=_horizontal_ratio(lines))


def _measure(path: str, image: Image.Image, angle: int) -> tuple[float, list[OcrLine]]:
    """全分辨率识别一个候选角度，给出 (得分, 行)。

    angle=0 直接读原文件（不落盘中转），保证与旧行为逐字节一致：摆正的页
    不会因为方向判定多一次编码而坐标漂移。
    """
    if angle == 0:
        lines = recognize(path)
    else:
        with _temp_path(_rotate(image, angle)) as temp:
            lines = recognize(temp)
    return text_score(lines), lines


def _decide(path: str) -> _Decision:
    """完整判定：0° 探针定轴 → 轴上两个方向全分辨率比分数。"""
    try:
        image = _open_image(path)
    except OcrError:
        # 解不开的图（损坏/非常见格式）不猜方向：直接交给识别器——它认得出
        # 就出结果，认不出照旧抛 OcrError（口径与加方向判定之前一致）。
        lines = recognize(path)
        return _Decision(angle=0, score=text_score(lines), lines=tuple(lines))
    candidates = _candidates(path, image)
    if candidates is None:
        # 两条轴都探不到横排文字：不瞎转。仍读一遍原图，好让调用方拿得到行。
        lines = recognize(path)
        return _Decision(angle=0, score=text_score(lines), lines=tuple(lines))
    best_angle, best_score, best_lines = 0, -1.0, []
    for angle in candidates:
        score, lines = _measure(path, image, angle)
        # 严格大于：分数一样时保留候选里靠前的角度
        if score > best_score:
            best_angle, best_score, best_lines = angle, score, lines
    return _Decision(angle=best_angle, score=best_score, lines=tuple(best_lines))


def _cache_key(path: str) -> tuple[str, int, int]:
    stat = Path(path).stat()
    return (str(Path(path).resolve()), stat.st_mtime_ns, stat.st_size)


def _ensure_usable(path: str) -> None:
    """禁用开关与文件存在性先查：命中缓存也不能绕过这两个前置条件。"""
    if os.environ.get(ENV_DISABLE) == "1":
        raise OcrError(f"OCR 已被环境变量 {ENV_DISABLE}=1 禁用")
    if not Path(path).is_file():
        raise OcrError(f"图片不存在：{path}")


def _decide_cached(path: str) -> _Decision:
    """判定 + 进程内缓存（同一张图只 OCR 一遍）。"""
    _ensure_usable(path)
    key = _cache_key(path)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    decision = _decide(path)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = decision
    return decision


def detect_orientation(path: str) -> tuple[int, float]:
    """判定整页方向：返回 (需要逆时针转多少度才能摆正, 该方向的得分)。"""
    decision = _decide_cached(path)
    return decision.angle, decision.score


def orient(path: str) -> tuple[Image.Image, int]:
    """摆正整页：返回 (图, 角度)。

    angle=0 时**返回原图对象本身**，不重采样、不重编码；非 0 时是
    `原图.rotate(angle, expand=True)`，90° 倍数为无损转置。
    """
    decision = _decide_cached(path)
    image = _open_image(path)
    if decision.angle == 0:
        return image, 0
    return _rotate(image, decision.angle), decision.angle


def read_page(path: str) -> OrientedPage:
    """一次拿全：摆正后的图 + 角度 + 得分 + 该图上的识别行（复用缓存，不重复 OCR）。

    图解不开时 `image` 为 None（判定退化为 0°、行交给识别器），`width`/`height`
    随之为 0——调用方该用库里的原始尺寸兜底。
    """
    decision = _decide_cached(path)
    if decision.angle:
        image: Image.Image | None = _rotate(_open_image(path), decision.angle)
    else:
        try:
            image = _open_image(path)
        except OcrError:
            image = None
    return OrientedPage(
        image=image,
        angle=decision.angle,
        score=decision.score,
        lines=list(decision.lines),
    )
