"""`python -m cairn.ocr recognize|cut|orient|redmark|preprocess <图片路径>`。

- `recognize`：打印每行文本、置信度、框坐标（给 Lead 验识别质量）。
- `cut`：先判整页方向并摆正（`read_page`，与产品路径一致），再按题号切块，
  打印判定角度 / 题数 / 每题题号 / 并集外接框 / 题干前 40 字（给 Lead 验切题质量）。
- `orient`：先判整页方向（0/90/180/270）并摆正，打印判定角度、得分、
  摆正后的尺寸以及摆正后切出几题（给 Lead 验方向判定是否真的救回了躺倒页）。
- `redmark`：切题后逐题判"有没有红笔痕迹"，打印每题红像素占比与汇总
  （给 Lead 验预勾选候选错题准不准；只判有无，不判勾/叉）。
- `preprocess`：跑上传自动预处理（找纸边 → 透视矫正 → 去阴影），打印四个角的
  坐标、纸面占比、输出尺寸与耗时，把处理图写到第二个参数（给 Lead 人工核对
  "扫描化"到底做了些什么）。
"""

from __future__ import annotations

import sys
import time

from cairn.ocr import preprocess, redmark
from cairn.ocr.cut import QuestionBlock, cut_questions
from cairn.ocr.engine import OcrError, recognize_timed
from cairn.ocr.lines import group_rows
from cairn.ocr.orient import detect_orientation, read_page

USAGE = (
    "用法: python -m cairn.ocr recognize <图片路径> | cut <图片路径> "
    "| orient <图片路径> | redmark <图片路径> "
    "| preprocess <输入图> <输出图>"
)


def _tag(block: QuestionBlock) -> str:
    """题号标签：推测题号用 `?` 标出（`[?21]`），确认题号保持 `[ 1]`。"""
    return f"[{'?' if block.inferred_number else ''}{block.no:>2}]"


def _run_recognize(path: str) -> int:
    lines, seconds = recognize_timed(path)
    columns = group_rows(lines)
    total_rows = sum(len(rows) for rows in columns)
    print(f"共 {len(columns)} 栏、{total_rows} 行，耗时 {seconds:.2f}s")
    for col_no, rows in enumerate(columns, start=1):
        if len(columns) > 1:
            print(f"—— 第 {col_no} 栏：{len(rows)} 行 ——")
        for row_no, row in enumerate(rows, start=1):
            for line in sorted(row, key=lambda item: min(x for x, _ in item.box)):
                box = " ".join(f"({x},{y})" for x, y in line.box)
                print(f"[{col_no}.{row_no:>2}] {line.text}  conf={line.confidence:.2f}  box={box}")
    return 0


def _run_cut(path: str) -> int:
    """判方向 → 摆正 → 切题：与产品路径（`read_page` + `cut_questions`）一致。

    侧拍/倒拍的照片若直接按原图识别，题号行是竖条，切出来 0 题（paper-04 实测
    0 题 vs 摆正后 10 题）。所以这里必须走带方向判定的 `read_page`，不能退回
    `recognize_timed`——否则命令行会给出和产品相反的结论。
    """
    started = time.perf_counter()
    page = read_page(path)
    blocks = cut_questions(page.lines, page.width, page.height)
    elapsed = time.perf_counter() - started
    print(f"判定方向 {page.angle}°，共 {len(blocks)} 题，耗时 {elapsed:.2f}s")
    if page.angle:
        print(f"（原图已逆时针旋转 {page.angle}° 摆正后再切题）")
    if any(block.inferred_number for block in blocks):
        print("（题号带 ? / [推测] 的是按跳号补出来的候选题，卷面上没认出题号）")
    for block in blocks:
        mark = "[推测]" if block.inferred_number else ""
        print(f"{_tag(block)} bbox={block.bbox} {block.stem_text[:40]} {mark}".rstrip())
    return 0


def _run_orient(path: str) -> int:
    """判方向 → 摆正 → 直接在摆正后的图上切题（判定与切题共用一次 OCR）。"""
    started = time.perf_counter()
    angle, score = detect_orientation(path)
    page = read_page(path)  # 命中判定缓存：不再重复识别
    blocks = cut_questions(page.lines, page.width, page.height)
    elapsed = time.perf_counter() - started
    turn = "无需旋转，原图未重采样" if angle == 0 else f"已逆时针旋转 {angle}°"
    print(f"判定方向：{angle}°（{turn}），得分 {score:.1f}")
    print(f"摆正后尺寸：{page.width}×{page.height}")
    print(f"共 {len(blocks)} 题，耗时 {elapsed:.2f}s")
    if blocks:
        numbers = [block.no for block in blocks]
        print(f"题号 {numbers}")
        inferred = [block.no for block in blocks if block.inferred_number]
        if inferred:
            print(f"其中推测题号（卷面没认出，按跳号补）：{inferred}")
    for block in blocks:
        print(f"{_tag(block)} bbox={block.bbox} {block.stem_text[:40]}")
    return 0


def _run_redmark(path: str) -> int:
    """切题 → 逐题判红笔痕迹（只判有无）：打印比例、判定与有红笔的题号。

    `red_ratios` 吃的是**摆正后**的页图（`page.image`）与切题用的同一套 bbox，
    所以侧拍/倒拍页判的也是同一块区域，不会因为坐标错位把邻题的红笔算进来。
    """
    if not redmark.is_enabled():
        print(f"红笔检测已关闭（{redmark.ENV_DISABLE}=0）")
        return 0
    started = time.perf_counter()
    page = read_page(path)
    if page.image is None:
        # 判方向时图就没解开（read_page 允许 image=None）：没有像素就判不了红笔
        print(f"图片无法解码，判不了红笔：{path}", file=sys.stderr)
        return 1
    blocks = cut_questions(page.lines, page.width, page.height)
    boxes = [block.bbox for block in blocks]
    ratios = redmark.red_ratios(page.image, boxes)
    # 只扫一遍像素：比例算完直接跟同一个阈值比，不重复走 detect_red_marks
    flags = [ratio > redmark.RED_RATIO_THRESHOLD for ratio in ratios]
    elapsed = time.perf_counter() - started
    print(f"判定方向 {page.angle}°，共 {len(blocks)} 题，耗时 {elapsed:.2f}s")
    for block, ratio, flagged in zip(blocks, ratios, flags):
        print(
            f"{_tag(block)} {'红笔' if flagged else '无  '} "
            f"红像素 {ratio * 100:.2f}% bbox={block.bbox}"
        )
    hits = [block.no for block, flagged in zip(blocks, flags) if flagged]
    if hits:
        print(f"检出 {len(hits)} 题有红笔痕迹（会预勾）：{hits}")
    else:
        print("没检出红笔痕迹（一题都不预勾）")
    return 0


def _run_preprocess(source: str, target: str) -> int:
    """跑一遍上传预处理并把结果写成 JPEG：打印四角、纸面占比、尺寸与耗时。

    找不到纸张边界时**不写输出文件**，也不报错（与上传链路一致：没边界就用原图）。
    这张子命令是给 Lead 人工核对用的——四角坐标能直接看出纸被认在哪。
    """
    result = preprocess.preprocess_page(source)
    width, height = result.source_size
    out_width, out_height = result.size
    if result.quad is None:
        print(
            f"没找到纸张边界（纯白/满幅文字/极暗/纸铺满画面）："
            f"按原图处理，未写输出文件。耗时 {result.seconds:.2f}s"
        )
        print(f"原图尺寸 {width}×{height}")
        return 0
    quad = result.quad
    print(f"纸张四角（原图坐标，顺序 左上/右上/右下/左下）：{quad.as_list()}")
    for name, (x, y) in zip(("左上", "右上", "右下", "左下"), quad.points):
        print(f"  {name} ({x}, {y})")
    print(f"纸面占画面 {quad.area_ratio * 100:.1f}%")
    print(f"输入 {width}×{height} → 输出 {out_width}×{out_height}，耗时 {result.seconds:.2f}s")
    try:
        result.image.save(target, format="JPEG", quality=preprocess.OUTPUT_JPEG_QUALITY)
    except OSError as exc:
        print(f"写出失败：{exc}", file=sys.stderr)
        return 1
    print(f"已写出 {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {
        "recognize": _run_recognize,
        "cut": _run_cut,
        "orient": _run_orient,
        "redmark": _run_redmark,
    }
    if argv and argv[0] == "preprocess":
        if len(argv) != 3:
            print(USAGE, file=sys.stderr)
            return 2
        return _run_preprocess(argv[1], argv[2])
    if len(argv) != 2 or argv[0] not in commands:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        return commands[argv[0]](argv[1])
    except OcrError as exc:
        print(f"OCR 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
