"""行聚类与拼接：把 OCR 的碎块变成人能读的多行文本。

版面可能是并排两页/多栏（家长拍书摊开一页拍两面是常态）：必须
**先按 x 空隙切栏，栏内再按 y 聚行**，顺序不能反——否则左右两栏
同一 y 的行会被缝成一句（评审实测 paper-03 上 18/32 个分组混栏）。
"""

from __future__ import annotations

from cairn.ocr.engine import OcrLine

# 栏缝判据 = 宽度下限 + 两侧贯穿覆盖，双信号同时满足才算一栏：
# - COLUMN_WIDTH_HEIGHTS：缝宽 ≥ 行高中位数 × 1.0（滤掉 4~7px 的识别碎缝）
# - COLUMN_MIN_COVERAGE：缝两侧各有 ≥ 60% 页高范围有文字（栏缝贯穿整页；
#   单栏页的页边/题号空白带只有零星文字，覆盖远达不到）
# 来源：评审卡（2026-10-31 整页 OCR 返工）原建议 max(页宽×0.06, 行高中位数×3)，
# 但真图实测不成立——paper-03 栏缝仅 27px（< 行高×3=72，< 页宽×0.06=104），
# 而单栏 paper-04 的最宽空白带反而有 33px，纯宽度判据分不开两类；
# 上述常数按两张真图的实测覆盖（栏缝 0.72/0.89 vs 页边带 0.28）标定。
COLUMN_WIDTH_HEIGHTS = 1.0
COLUMN_MIN_COVERAGE = 0.6


def _box_height(line: OcrLine) -> int:
    return max(y for _, y in line.box) - min(y for _, y in line.box)


def _left_x(line: OcrLine) -> int:
    return min(x for x, _ in line.box)


def _right_x(line: OcrLine) -> int:
    return max(x for x, _ in line.box)


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _zero_bands(lines: list[OcrLine]) -> list[tuple[int, int]]:
    """块 x 覆盖并集里的空隙（没有任何块横穿的区间），按 x 升序。"""
    ordered = sorted((_left_x(line), _right_x(line)) for line in lines)
    bands: list[tuple[int, int]] = []
    reach = ordered[0][1]
    for a, b in ordered[1:]:
        if a > reach:
            bands.append((reach, a))
        reach = max(reach, b)
    return bands


def _y_cover(lines: list[OcrLine]) -> int:
    """块 y 区间并集的总长度。"""
    ordered = sorted(
        (min(y for _, y in line.box), max(y for _, y in line.box))
        for line in lines
    )
    total = 0
    cur_a, cur_b = ordered[0]
    for a, b in ordered[1:]:
        if a > cur_b:
            total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    return total + cur_b - cur_a


def _split_columns(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """把块切成 1..N 栏（每栏按 left_x 排序）。

    栏缝 = 没有任何块横穿的 x 空白带，且同时满足：
    宽度 ≥ 行高中位数 × COLUMN_WIDTH_HEIGHTS，缝两侧各有
    ≥ COLUMN_MIN_COVERAGE 的页高范围被文字覆盖（贯穿整页）。
    """
    if not lines:
        return []
    if len(lines) == 1:
        return [sorted(lines, key=_left_x)]
    top = min(y for line in lines for _, y in line.box)
    bottom = max(y for line in lines for _, y in line.box)
    span = max(bottom - top, 1)
    min_width = max(_median([_box_height(line) for line in lines]) * COLUMN_WIDTH_HEIGHTS, 1.0)
    gutters: list[float] = []
    for a, b in _zero_bands(lines):
        if b - a < min_width:
            continue
        mid = (a + b) / 2
        left = [line for line in lines if _right_x(line) <= mid]
        right = [line for line in lines if _left_x(line) >= mid]
        if not left or not right:
            continue
        if (
            _y_cover(left) / span >= COLUMN_MIN_COVERAGE
            and _y_cover(right) / span >= COLUMN_MIN_COVERAGE
        ):
            gutters.append(mid)
    if not gutters:
        return [sorted(lines, key=_left_x)]
    columns: list[list[OcrLine]] = [[] for _ in range(len(gutters) + 1)]
    for line in lines:
        columns[sum(1 for g in gutters if _left_x(line) > g)].append(line)
    return [sorted(column, key=_left_x) for column in columns]


def _cluster_rows(
    lines: list[OcrLine], tolerance_ratio: float
) -> list[list[OcrLine]]:
    """栏内按 y 聚行（自上而下），容差沿用原逻辑。"""
    if not lines:
        return []
    tolerance = max(_median([_box_height(line) for line in lines]) * tolerance_ratio, 1.0)
    rows: list[list[OcrLine]] = []
    for line in sorted(lines, key=lambda item: item.y_center):
        if rows:
            anchor = sum(item.y_center for item in rows[-1]) / len(rows[-1])
            if abs(line.y_center - anchor) <= tolerance:
                rows[-1].append(line)
                continue
        rows.append([line])
    return rows


def group_rows(
    lines: list[OcrLine], tolerance_ratio: float = 0.6
) -> list[list[list[OcrLine]]]:
    """先切栏、栏内聚行，返回每栏的行列表。

    返回结构：`columns[c][r]` = 第 c 栏（左→右）第 r 行（上→下）的块列表。
    y 容差 = 该栏框高度的中位数 × tolerance_ratio：中位数对个别大标题/
    小注脚稳健，0.6 是印刷卷面的起点，攒了真样本再调。
    """
    return [_cluster_rows(column, tolerance_ratio) for column in _split_columns(lines)]


def _is_ascii_alnum_char(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9")


def _needs_space(left: str, right: str) -> bool:
    """相邻块一个以 ASCII 字母数字结尾、另一个以 ASCII 字母数字开头 → 补空格。"""
    return bool(left) and bool(right) and _is_ascii_alnum_char(left[-1]) and _is_ascii_alnum_char(right[0])


def _row_text(row: list[OcrLine]) -> str:
    parts = [line.text for line in sorted(row, key=lambda item: _left_x(item))]
    if not parts:
        return ""
    text = parts[0]
    for part in parts[1:]:
        if _needs_space(text, part):
            text += " "
        text += part
    return text


def to_text(columns: list[list[list[OcrLine]]]) -> str:
    """栏内按行拼接、栏间空一行；单栏输出与原版完全一致。

    绝不把两栏拼成一句：每栏独立成段，栏序 = 左→右。
    """
    column_texts = [
        "\n".join(_row_text(row) for row in rows if row)
        for rows in columns
    ]
    return "\n\n".join(text for text in column_texts if text)
