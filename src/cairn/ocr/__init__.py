"""整页 OCR（RapidOCR）：图 → 判方向摆正 → 带坐标的文本行 → 按题号切块。

外加红笔痕迹检测（`redmark`）：题框里有没有红笔，只判有无、不判勾/叉。
以及上传自动预处理（`preprocess`）：找纸张四边 → 透视矫正 → 去阴影提对比，
结果只服务切题/OCR，处理不了就退回原图。

`preprocess` 是**惰性导出**：它拖着 cv2 + numpy，只有真要处理图的那条路才 import；
`import cairn.ocr` 本身仍然很轻（切题/方向判定不需要 cv2）。
"""

from importlib import import_module

from cairn.ocr import redmark
from cairn.ocr.cut import QuestionBlock, cut_questions
from cairn.ocr.deskew import deskew, deskew_image, estimate_angle
from cairn.ocr.engine import OcrError, OcrLine, recognize
from cairn.ocr.lines import group_rows, to_text
from cairn.ocr.orient import (
    OrientedPage,
    detect_orientation,
    orient,
    read_page,
    text_score,
)
from cairn.ocr.redmark import detect_in_page, detect_red_marks, red_ratios

# 惰性导出的名字（PEP 562）：见包开头说明
_PREPROCESS_NAMES = frozenset(
    {
        "preprocess",
        "PageQuad",
        "PreprocessResult",
        "preprocess_image",
        "preprocess_page",
        "find_page_quad",
        "warp_page",
        "enhance",
        "save_processed",
    }
)

# `enhance`（模块）与 `preprocess.enhance`（摆正尾段的去阴影函数）同名，
# 所以模块本身不走这个名字：要它用 `import_module("cairn.ocr.enhance")`，
# 平时只从这里拿三个函数。
_ENHANCE_NAMES = frozenset({"enhance_page", "enhance_image", "save_gray"})


def __getattr__(name: str):
    """`from cairn.ocr import preprocess` 时才把 cv2/numpy 拖进来。"""
    if name in _PREPROCESS_NAMES:
        # 必须走 import_module：`from cairn.ocr import preprocess` 会再触发本函数
        module = import_module("cairn.ocr.preprocess")
        return module if name == "preprocess" else getattr(module, name)
    if name in _ENHANCE_NAMES:
        return getattr(import_module("cairn.ocr.enhance"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "OcrError",
    "OcrLine",
    "OrientedPage",
    "QuestionBlock",
    "recognize",
    "group_rows",
    "to_text",
    "cut_questions",
    "deskew",
    "deskew_image",
    "estimate_angle",
    "detect_orientation",
    "orient",
    "read_page",
    "text_score",
    "redmark",
    "detect_red_marks",
    "detect_in_page",
    "red_ratios",
    "preprocess",
    "PageQuad",
    "PreprocessResult",
    "preprocess_image",
    "preprocess_page",
    "find_page_quad",
    "warp_page",
    "enhance",
    "save_processed",
    "enhance_page",
    "enhance_image",
    "save_gray",
]
