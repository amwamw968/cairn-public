"""题号切题：把带坐标的文本行按题号切成一道道题（OCR 通道第二步）。

手写作答传统 OCR 认不出，必须切成题块再交给视觉大模型；本模块就是
「切块」：识别题号行，从题号行起、到下一个题号行之前的所有行都并入
该题的 `stem_text`。

规则（与任务卡一致）：

- **栏内切题**：复用 `lines.group_rows` 的分栏结果，跨栏绝不合并成
  同一题；栏内题号严格递增是真卷的常态，但重复/跳号**不抛异常**，
  按出现顺序出块，`no` 保留卷面原始编号。
- 题号只认**行首**：`[（(]?数字[)）.．、]`，支持全角数字；分隔符后
  紧跟 1~2 位数字视为小数（"2.5米"、"14.06秒"），不算题号，3 位以上
  （"1.2025辽宁…"年份开头）仍算；"4个百" 这类中间序号自然不匹配
  （分隔符必须紧跟数字）。
- 选项行（`A.`）不是题号，但内容并入当前题；页码/页眉页脚（纯数字、
  `第X页`、`- 3 -`）整行丢弃；首个题号之前的杂行（大标题等）丢弃。
- **行内嵌题号**：同一行里出现"上一个题号 + 1"的题号（`…应找回30元14.用2倍…`），
  在该处断开，后半截另起一题。真卷 OCR 常把下一题的行首并进上一题的文本行。
- **断头行**：印刷行不可能以右括号开头；以 `）)` 开头的行说明行首被 OCR 吞掉了。
  这种行不属于上一题（否则题号会永久丢失），但它自己的题号也没认出来——
  只有"下一个认出的题号正好比当前题号大 2"（卷面编号证明这里恰好缺一道）时
  才补成中间那道题；否则按旧行为并回上一题。
- **页缝补切**：跨页照片里右页的题不能排到左页前面。`lines.py` 的栏缝要求
  "没有任何块横穿"，可 OCR 常把行尾多框进栏缝，整页于是被当成一栏、题号按
  y 全局排序。这里补一条**同判据、只放宽穿越**的软栏缝（见 `_soft_gutter`）：
  两侧各自认出题号才算，通常仍是 1 段，行为不变。
- **跳号补号**：相邻两个认出的题号正好差 2（20 → 22）、且前题题号行后面还
  有正文行时，把那些行摘出来变成 `no=21` 的**候选题**，`inferred_number=True`。
  只补一个号位（20 → 23 连跳两个不猜）；大题标题行不进任何一侧的题干。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cairn.ocr.engine import OcrLine
from cairn.ocr.lines import (
    COLUMN_MIN_COVERAGE,
    COLUMN_WIDTH_HEIGHTS,
    _box_height,
    _left_x,
    _median,
    _right_x,
    _y_cover,
    group_rows,
)

# 行首题号：可选全角/半角左括号 + 1~2 位数字（含全角）+ 分隔符
_QUESTION_RE = re.compile(r"^\s*[（(]?([0-9０-９]{1,2})[)）.．、]")
# 选项行：A. / B、/ C．（不是题号，内容并入当前题）
_OPTION_RE = re.compile(r"^\s*[A-D][.．、]")
# 页码/页脚：纯数字、- 3 -、第X页
_PAGE_NO_RE = re.compile(
    r"^\s*(?:\d{1,3}|[-–—]\s*\d{1,3}\s*[-–—]?|第\s*\d+\s*页)\s*$"
)
# 大题标题：一、二、… + 顿号/点。不是题号，也不进候选题的题干
_SECTION_RE = re.compile(r"^\s*[一二三四五六七八九十百]+\s*[、.．]")

# 页缝补切允许的"越缝行"比例：lines.py 要求零穿越，跨页照片里
# OCR 常把行尾多框进栏缝（paper-04 有 5 行越缝），整页会被当一栏。
_GUTTER_MAX_CROSS_RATIO = 0.08

# 行内题号的候选形态：数字 + 分隔符（含右括号），行首那个由 _QUESTION_RE 负责
_INLINE_NO_RE = re.compile(r"[0-9０-９]{1,2}[)）.．、]")
# 印刷行不可能以右括号开头：出现即说明行首被 OCR 吞了
_BROKEN_HEAD_CHARS = "）)】》」』〕］｝}"

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_DIGIT_CHARS = set("0123456789０１２３４５６７８９")


def _question_no(text: str) -> int | None:
    """行首题号 → int（全角数字归一）；不是题号行返回 None。

    分隔符后紧跟数字是歧义场景："2.5米"、"14.06秒"（1~2 位小数）不是
    题号；"1.2025辽宁…"（年份开头，3 位以上）是真卷常见的题号行，
    仍算题号。
    """
    match = _QUESTION_RE.match(text)
    if not match:
        return None
    nxt = text[match.end() :].lstrip()
    if nxt and nxt[0] in _DIGIT_CHARS:
        run = len(nxt) - len(nxt.lstrip("".join(_DIGIT_CHARS)))
        if run < 3:  # 1~2 位数字 = 小数（2.5、14.06），不算题号
            return None
    return int(match.group(1).translate(_FULLWIDTH_DIGITS))


def _is_ascii_alnum_char(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9")


def _needs_space(left: str, right: str) -> bool:
    """相邻段一个以 ASCII 字母数字结尾、另一个以 ASCII 字母数字开头 → 补空格。

    复制自 lines.py（同逻辑，不改动原模块）：中文相接不加空格。
    """
    return bool(left) and bool(right) and _is_ascii_alnum_char(left[-1]) and _is_ascii_alnum_char(right[0])


def _join_texts(parts: list[str]) -> str:
    """按中英间距规则拼接：ASCII 字母数字交界补一个空格，其余直接相连。"""
    if not parts:
        return ""
    text = parts[0]
    for part in parts[1:]:
        if _needs_space(text, part):
            text += " "
        text += part
    return text


def _is_broken_head(text: str) -> bool:
    """行首是右括号 → 印刷行不可能这样开头，说明行首那截被 OCR 吞了。"""
    return bool(text) and text[0] in _BROKEN_HEAD_CHARS


def _is_section_line(text: str) -> bool:
    """大题标题（`三、计算题。（共26分）`）：不是题号，也不是题干。"""
    return bool(_SECTION_RE.match(text))


def _row_chunks(row: list[OcrLine]) -> list[list[OcrLine]]:
    """把一行按明显的横向断裂（相邻块 x 间隙 > 行高）切成若干块。

    同一视觉行里左右两段相隔一整个行高以上，说明它们不是同一句
    （答案栏、被 OCR 缝在一起的相邻两行），切开后再判断行首。
    """
    ordered = sorted(row, key=_left_x)
    if len(ordered) <= 1:
        return [ordered] if ordered else []
    height = max(_box_height(line) for line in ordered)
    chunks = [[ordered[0]]]
    for line in ordered[1:]:
        if _left_x(line) - _right_x(chunks[-1][-1]) > height:
            chunks.append([line])
        else:
            chunks[-1].append(line)
    return chunks


def _split_inline(text: str, expected_no: int | None) -> list[str]:
    """行内嵌题号：`…应找回30元14.用2倍…` 在 `14.` 处断开，后半截另起一题。

    只认"正好是下一道题号"（`expected_no`）且分隔符后不是数字（排除小数
    `14.06秒`）、前一个字符不是数字（排除 `3014.` 这类整体数字）。
    一行里连续嵌了两道题就反复切。切不开返回单元素列表。
    """
    if expected_no is None:
        return [text]
    pieces: list[str] = []
    rest = text
    no = expected_no
    while True:
        split = _split_inline_once(rest, no)
        if split is None:
            pieces.append(rest)
            return pieces
        pieces.append(split[0])
        rest = split[1]
        no += 1


def _split_inline_once(text: str, no: int) -> tuple[str, str] | None:
    """找一处行内题号 `no` 并切成 (前段, 后段)；找不到返回 None。"""
    for match in _INLINE_NO_RE.finditer(text, 1):
        start = match.start()
        if text[start - 1] in _DIGIT_CHARS:
            continue
        if _question_no(text[start:]) != no:
            continue
        rest = text[match.end():].strip()
        head = text[:start].rstrip()
        if head and rest:
            return head, text[start:]
    return None


@dataclass
class QuestionBlock:
    """一道题：卷面原始题号、所有行的并集外接框、合并题干、行索引。

    `inferred_number=True` 表示题号不是卷面上认出来的，而是按"跳号 +
    中间有正文"推出来的候选题——展示层必须标出来，不能当确定题号。
    """

    no: int
    bbox: tuple[int, int, int, int]
    stem_text: str
    line_indexes: list[int]
    inferred_number: bool = False


def _make_block(
    no: int,
    members: list[OcrLine],
    texts: list[str],
    index_of: dict[int, int],
) -> QuestionBlock:
    xs = [x for line in members for x, _ in line.box]
    ys = [y for line in members for _, y in line.box]
    return QuestionBlock(
        no=no,
        bbox=(min(xs), min(ys), max(xs), max(ys)),
        stem_text=_join_texts(texts),
        line_indexes=[index_of[id(line)] for line in members],
    )


def _soft_gutter(lines: list[OcrLine]) -> float | None:
    """一"栏"其实是横排两页时，返回两页之间那条软栏缝的 x；否则 None。

    判据与 `lines.py._split_columns` 同源（缝要够宽、两侧各有
    ≥`COLUMN_MIN_COVERAGE` 的页高范围有文字、两侧都认出了题号），
    只把"零块横穿"放宽成"越缝行 ≤8%"：OCR 会把行尾多框进栏缝，
    跨页照片因此整页落成一栏，题号按 y 全局排序 → 右页 24 排到左页
    19 前面。候选缝取"越缝行最少"的那条。

    "两侧都有题号"是关键护栏：单栏页把正文从中间劈开时，右半截通常
    没有题号，不满足；正常单栏页的行为因此不变。
    """
    if len(lines) < 4:
        return None
    min_gap = max(_median([_box_height(line) for line in lines]) * COLUMN_WIDTH_HEIGHTS, 1.0)
    top = min(y for line in lines for _, y in line.box)
    bottom = max(y for line in lines for _, y in line.box)
    span = max(bottom - top, 1)
    edges = sorted({_left_x(line) for line in lines})
    best: tuple[int, float] | None = None  # (越缝行数, 缝中点)
    for low, high in zip(edges, edges[1:]):
        if high - low < min_gap:
            continue
        mid = (low + high) / 2
        left = [line for line in lines if _left_x(line) < mid]
        right = [line for line in lines if _left_x(line) >= mid]
        crossing = sum(1 for line in left if _right_x(line) > mid)
        if crossing > len(lines) * _GUTTER_MAX_CROSS_RATIO:
            continue
        if _y_cover(left) / span < COLUMN_MIN_COVERAGE:
            continue
        if _y_cover(right) / span < COLUMN_MIN_COVERAGE:
            continue
        if not any(_question_no(line.text) is not None for line in left):
            continue
        if not any(_question_no(line.text) is not None for line in right):
            continue
        if best is None or crossing < best[0]:
            best = (crossing, mid)
    return best[1] if best else None


class _ColumnCutter:
    """一栏的切题状态机。

    三种输入：题号行（开新题）、断头行（行首被吞，先挂起）、续行。
    挂起的断头行只在"下一个题号 = 当前题号 + 2"时才补号成新题——卷面
    编号自己证明了这里恰好缺一道；否则并回上一题（旧行为），不猜。
    """

    def __init__(self, index_of: dict[int, int]) -> None:
        self._index_of = index_of
        self._blocks: list[QuestionBlock] = []
        self._cur_no: int | None = None
        self._cur_members: list[OcrLine] = []
        self._cur_texts: list[str] = []
        self._pend_members: list[OcrLine] = []
        self._pend_texts: list[str] = []
        self._pending = False

    @property
    def current_no(self) -> int | None:
        return self._cur_no

    def numbered(self, no: int, members: list[OcrLine], text: str) -> None:
        """题号行：先给挂起的断头行定性，再收口上一题、开新题。"""
        if self._pending:
            self._resolve_pending(no)
        self._close()
        self._cur_no = no
        self._cur_members = list(members)
        self._cur_texts = [text]

    def broken_head(self, members: list[OcrLine], text: str) -> None:
        """断头行：行首被 OCR 吞了，先挂起等下一个题号来定性。"""
        if not self._cur_members:
            return  # 首个题号之前的杂行：老规则丢弃
        if self._pending:
            self._merge_pending()  # 连续两个断头：前一个定不了号，并回上一题
        self._pending = True
        self._pend_members = list(members)
        self._pend_texts = [text]

    def continuation(self, members: list[OcrLine], text: str) -> None:
        """续行/选项行：并入挂起的断头行（若在等定号），否则并入当前题。"""
        if self._pending:
            self._pend_members.extend(members)
            self._pend_texts.append(text)
        elif self._cur_members:
            self._cur_members.extend(members)
            self._cur_texts.append(text)

    def finish(self) -> list[QuestionBlock]:
        """栏尾收口：最后一段断头行定不了号，按旧行为并回上一题。"""
        if self._pending:
            self._merge_pending()
        self._close()
        return self._blocks

    def _resolve_pending(self, next_no: int) -> None:
        if self._cur_no is not None and next_no == self._cur_no + 2:
            self._close()
            self._blocks.append(
                _make_block(
                    self._cur_no + 1,
                    self._pend_members,
                    self._pend_texts,
                    self._index_of,
                )
            )
            self._clear_pending()
            return
        self._merge_pending()

    def _merge_pending(self) -> None:
        self._cur_members.extend(self._pend_members)
        self._cur_texts.extend(self._pend_texts)
        self._clear_pending()

    def _clear_pending(self) -> None:
        self._pend_members = []
        self._pend_texts = []
        self._pending = False

    def _close(self) -> None:
        if self._cur_members:
            self._blocks.append(
                _make_block(self._cur_no, self._cur_members, self._cur_texts, self._index_of)
            )
        self._cur_members = []
        self._cur_texts = []


def _split_at_gutter(chunk: list[OcrLine], gutter: float | None) -> list[list[OcrLine]]:
    """按软栏缝把一块文本劈成左右两段（单栏时原样返回）。

    `_row_chunks` 的断缝阈值是行高：左页行尾多框进栏缝、右页行首紧挨着，
    这一整行会被缝成一块，右页的题号因此被并进左页题目的续行。
    """
    if gutter is None:
        return [chunk]
    left = [line for line in chunk if _left_x(line) < gutter]
    right = [line for line in chunk if _left_x(line) >= gutter]
    return [part for part in (left, right) if part]


def _feed_chunk(cutter: _ColumnCutter, chunk: list[OcrLine]) -> None:
    """一块文本喂给某栏的状态机：可能开新题、可能是断头行、也可能是续行。"""
    text = _join_texts([line.text for line in chunk])
    # 选项行永远不是题号新起点；其余行看行首题号
    start_no = None if _OPTION_RE.match(text) else _question_no(text)
    if start_no is not None:
        # 行内还可能接着嵌下一题（题号行被并进上一题的文本行）
        pieces = _split_inline(text, start_no + 1)
        cutter.numbered(start_no, chunk, pieces[0])
        next_no = start_no + 1
    elif _is_broken_head(text):
        cutter.broken_head(chunk, text)
        return
    else:
        current_no = cutter.current_no
        if current_no is None:
            cutter.continuation(chunk, text)
            return
        pieces = _split_inline(text, current_no + 1)
        cutter.continuation(chunk, pieces[0])
        next_no = current_no + 1
    for piece in pieces[1:]:
        cutter.numbered(next_no, chunk, piece)
        next_no += 1


def _block_from(
    indexes: list[int], lines: list[OcrLine], no: int, *, inferred: bool = False
) -> QuestionBlock:
    """按行索引造题块（补号时要把上一题的行摘出来重组，不能再用状态机）。"""
    members = [lines[i] for i in indexes]
    xs = [x for line in members for x, _ in line.box]
    ys = [y for line in members for _, y in line.box]
    return QuestionBlock(
        no=no,
        bbox=(min(xs), min(ys), max(xs), max(ys)),
        stem_text=_join_texts([line.text for line in members]),
        line_indexes=list(indexes),
        inferred_number=inferred,
    )


def _infer_missing(blocks: list[QuestionBlock], lines: list[OcrLine]) -> list[QuestionBlock]:
    """相邻两题跳了一个号、且前题题号行后面还有正文行 → 补一个候选题。

    只补**一个**号位（20 → 22 补 21）；20 → 23 连跳两个号不猜，保持缺号。
    卷面编号自己证明这里恰好缺一道，但缺的是哪一截正文说不准——所以
    摘出来的题打 `inferred_number=True`，展示层必须标成"题号未识别"。
    大题标题行两头都不进；前题只剩题号行、没有正文行时不补。
    """
    result: list[QuestionBlock] = []
    for i, block in enumerate(blocks):
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if nxt is None or nxt.no != block.no + 2:
            result.append(block)
            continue
        head = block.line_indexes[:1]
        if not head or _question_no(lines[head[0]].text) != block.no:
            result.append(block)  # 断头行补出来的题：没有题号行可留，不猜
            continue
        if set(head) & set(nxt.line_indexes):
            result.append(block)  # 同一行被行内切成了两题：不重复占用
            continue
        moved = [
            index
            for index in block.line_indexes[1:]
            if index not in nxt.line_indexes and not _is_section_line(lines[index].text)
        ]
        if not moved:
            result.append(block)  # 中间没有正文（只有大题标题）：不补
            continue
        result.append(_block_from(head, lines, block.no))
        result.append(_block_from(moved, lines, block.no + 1, inferred=True))
    return result


def cut_questions(
    lines: list[OcrLine], page_width: int, page_height: int
) -> list[QuestionBlock]:
    """按题号把整页文本行切成题块（栏内切题，跨栏不合并）。

    阅读顺序 = 栏序（左→右）× 栏内 y（上→下）：`group_rows` 的每一栏
    再按 `_soft_gutter` 判断是不是"跨页被当成一栏"，是就拆成左右两段
    并行喂给各自的状态机——右页的题绝不会排到左页前面。

    `page_width` / `page_height` 当前不参与判据（题号识别纯文本驱动），
    保留在签名里供下一卡按 bbox 裁题块图用。无题号的页返回空列表，
    不抛异常。
    """
    if not lines:
        return []
    index_of = {id(line): i for i, line in enumerate(lines)}
    blocks: list[QuestionBlock] = []
    for column in group_rows(lines):
        gutter = _soft_gutter([line for row in column for line in row])
        cutters = [_ColumnCutter(index_of)] if gutter is None else [
            _ColumnCutter(index_of),
            _ColumnCutter(index_of),
        ]
        for row in column:
            ordered = sorted(row, key=_left_x)
            if _PAGE_NO_RE.match(_join_texts([line.text for line in ordered])):
                continue  # 页码/页脚：整行丢弃，不进题干
            for chunk in _row_chunks(ordered):
                # 越缝的行先把块劈开：OCR 会把左页行尾和右页行首缝进同一块
                for part in _split_at_gutter(chunk, gutter):
                    cutter = (
                        cutters[1]
                        if len(cutters) > 1 and _left_x(part[0]) >= gutter
                        else cutters[0]
                    )
                    _feed_chunk(cutter, part)
        for cutter in cutters:
            blocks.extend(_infer_missing(cutter.finish(), lines))
    return blocks
