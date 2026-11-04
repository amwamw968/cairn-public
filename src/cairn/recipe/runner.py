"""按配方跑一页纸：一步一步来，每步留痕，关掉的工具不许把后面的步骤一起拖死。

调用方只管两件事：给 `ctx`（这页的图在哪、是哪张 scan），然后看 `RunResult`。
`RunResult.steps` 就是页面上那行痕迹的来源，`RunResult.data` 里是需要的东西
（`page` / `blocks` / `rows`）——哪一步没做成，对应的键就不在。

状态只有四种：

    ran                  做成了
    skipped_disabled     这一步的工具被开关关了（AC4：后面的步骤照跑）
    skipped_unavailable  工具开着，但这步现在做不了（缺前置：切题离不开认字）
    failed_fallback      工具抛错了，`on_error` 允许兜底（三种兜底方式都在这）

四种之外只有一种情况会往上抛：**这步没有兜底**（`on_error` 写成 keep/skip/ask
以外的值）。AC7 的变异自证就是拿这个口子做的。

关于 `preprocess`（摆正）：透视矫正在上传时已经做完（`scan_service.save_upload`，
产物是 `*.processed.jpg`），配方里的这一步只决定"这一页用摆正后的那张，还是
原图"——和 `scan_service.read_image_path` 的取图顺序完全一致，行为零变化。
方向摆正（0/90/180/270）由 `ocr.read_page` 一起做，落在"切题"那步的第一次读图里。

关于 `enhance`（增强）：和摆正是同一个语义——增强图也是上传时落好的（同一个
`*.processed.jpg`，内容是增强后的灰度），这一步只负责"确认这一页有没有增强图、
后面的切题/认字该读哪张"。有就记 `用增强后的图`，没有（工具关了、找不到纸边、
老照片）就记 `这一页没有增强图，用原图`——两种都算 `ran`，因为这步的职责是选图。

关于 `crop`（裁切）：真活也在上传时干完了（`scan_service.save_upload`），裁完的图
另存成 `<uuid>.crop.jpg`，原图一个字节不改。这一步把上传时记下的那句话原样念出来：
裁了、跳过（纸已满幅 / 和背景分不开 / 太小 / 不是四角 / 被截断 / 边不清楚）。老照片
没有那条记录，就现判一次，能裁就裁到临时文件、后面几步用裁好的那张。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from cairn import ocr, tools

from .spec import ON_ERROR_KEEP, ON_ERROR_MODES, ON_ERROR_SKIP, Recipe

__all__ = [
    "FAILED_FALLBACK",
    "RAN",
    "SKIPPED_DISABLED",
    "SKIPPED_UNAVAILABLE",
    "RunResult",
    "StepResult",
    "StepUnavailable",
    "run",
]

RAN = "ran"
SKIPPED_DISABLED = "skipped_disabled"
SKIPPED_UNAVAILABLE = "skipped_unavailable"
FAILED_FALLBACK = "failed_fallback"

# 兜底时在原因后面加的一句：让人一眼看出这步是怎么被放过的
_FALLBACK_TAIL = {
    ON_ERROR_KEEP: "（保持原样继续）",
    ON_ERROR_SKIP: "（这步先不做）",
}


class StepUnavailable(Exception):
    """这一步现在做不了，但不是坏掉：缺前置工具、缺文件、缺数据。"""


@dataclass(frozen=True)
class StepResult:
    """一步的痕迹：叫什么、什么结局、花了多久、为什么。"""

    name: str
    status: str
    ms: float
    note: str | None = None


@dataclass
class RunResult:
    """一份配方的跑完结果。`data` 就是调用方传进来的 `ctx`（执行器往里写）。"""

    recipe: Recipe
    steps: list[StepResult] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    @property
    def statuses(self) -> list[str]:
        return [step.status for step in self.steps]

    @property
    def all_ran(self) -> bool:
        return all(status == RAN for status in self.statuses)

    @property
    def missing_tools(self) -> dict[str, str]:
        """被开关关掉的工具 → 原因（按配方顺序）。页面据此决定说哪句话。"""
        missing: dict[str, str] = {}
        for step, result in zip(self.recipe.steps, self.steps):
            if result.status == SKIPPED_DISABLED:
                missing.setdefault(step.tool, result.note or "")
        return missing

    @property
    def failure_note(self) -> str | None:
        """第一条"没做成"的原因，给页面显示；全都做成了就是 None。"""
        for result in self.steps:
            if result.status in (FAILED_FALLBACK, SKIPPED_UNAVAILABLE) and result.note:
                return result.note
        return None

    def trace(self) -> str:
        """一行痕迹：`摆正 用原图 · 裁切 完成（…） · 切题 切出 18 题 1.1s`。

        做成的步骤说人话（`StepResult.note`），没有话可说才退化成 ✓；
        关掉的那步写 `裁切 已关闭`，没做成写 `✗`，做不了写 `—`。
        """
        marks = []
        for step in self.steps:
            if step.status == SKIPPED_DISABLED:
                marks.append(f"{step.name} 已关闭")
            elif step.status == RAN:
                marks.append(f"{step.name} {step.note}" if step.note else f"{step.name} ✓")
            elif step.status == FAILED_FALLBACK:
                marks.append(f"{step.name} ✗")
            else:
                marks.append(f"{step.name} —")
        total = sum(step.ms for step in self.steps) / 1000
        return " · ".join(marks) + f" {total:.1f}s"


def run(recipe: Recipe, ctx: dict) -> RunResult:
    """按 `recipe.steps` 的顺序跑一遍，往 `ctx` 里写结果。"""
    result = RunResult(recipe=recipe, data=ctx)
    for step in recipe.steps:
        if not tools.is_enabled(step.tool):
            # 工具被关：这一步不算数，但**后面的步骤照跑**（AC4）
            result.steps.append(
                StepResult(
                    name=step.name,
                    status=SKIPPED_DISABLED,
                    ms=0.0,
                    note=tools.disabled_reason(step.tool),
                )
            )
            continue
        handler = _HANDLERS.get(step.tool)
        started = time.perf_counter()
        try:
            if handler is None:
                raise StepUnavailable(f"工具 {step.tool} 还没有配方执行器")
            note = handler(ctx)
        except StepUnavailable as exc:
            # 工具开着但前置不满足：不是失败，别当失败记
            result.steps.append(
                StepResult(step.name, SKIPPED_UNAVAILABLE, _ms(started), str(exc))
            )
        except Exception as exc:  # noqa: BLE001 —— 兜不兜底由 on_error 决定
            if step.on_error not in ON_ERROR_MODES:
                raise  # 没兜底：往上抛（AC7 的变异口子）
            tail = _FALLBACK_TAIL.get(step.on_error, "")
            result.steps.append(
                StepResult(
                    step.name,
                    FAILED_FALLBACK,
                    _ms(started),
                    f"{exc}{tail}" if str(exc) else type(exc).__name__,
                )
            )
        else:
            result.steps.append(StepResult(step.name, RAN, _ms(started), note))
    return result


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


# --------------------------------------------------------------- 各步的执行器
# 键是 `cairn.tools` 里的英文短名。配方里写 cut 就走这里，不在配方里写代码。


def _step_preprocess(ctx: dict) -> str:
    """摆正：这一页用哪张图（摆正后的处理图优先，没有就用原图），与取图顺序一致。"""
    path = Path(ctx["path"])
    ctx["image_path"] = path
    on_processed = bool(getattr(ctx.get("scan"), "processed_path", None))
    ctx["image_source"] = "processed" if on_processed else "original"
    return "用摆正后的图" if on_processed else "用原图"


def _step_enhance(ctx: dict) -> str:
    """增强：用上传时落好的增强图（`processed_path` 指向它，内容是压白压黑的灰度）。

    真活在上传时干完了（`scan_service._write_processed`）：这段只回答"后面几步
    该读哪张图"，和 `_step_preprocess` 同一个语义——有增强图就用它，没有就照实
    说"用原图"，但**都算跑过**（这步的职责是选图，不是造图）。不抛
    `StepUnavailable`：那样这条 note 会顶掉后面真正的失败原因（`failure_note`
    取第一条非 ran 的），页面就把"认字失败"错报成"没有增强图"。

    挑的是 `processed_path` 那一张（不是 `ctx["path"]`）：这条路默认走的是
    `read_image_path`，它优先给裁好的彩色页——增强这一步的意义就是把后面几步
    换成灰度版，得点名要那张。
    """
    from cairn import scan_service  # 局部导入：配方层不拖 SQLAlchemy 进导入期

    scan = ctx.get("scan")
    db_url = ctx.get("db_url")
    processed = (
        scan_service.processed_image_path(scan, db_url)
        if scan is not None and db_url
        else None
    )
    if processed is None:
        return "这一页没有增强图，用原图"
    ctx["image_path"] = processed
    return "用增强后的图"


def _step_crop(ctx: dict) -> str:
    """裁切：把上传时那句人话念出来；老照片现判一次并裁到临时文件。

    上传时真活已经干完（`scan_service.save_upload`），裁好的图另存在原图旁边
    （`<uuid>.crop.jpg`，原图一个字节不改），结论记在 `<uuid>.crop.json`。
    这里**不重算**：图已经裁过了，再判只会得到"纸已满幅"，等于对家长说假话。
    没有记录（早于这个功能的照片、命令行直接喂一张样本）才现判：能裁就裁到
    系统临时目录，并把后面几步要读的图换成它。

    有记录时也把后面几步要读的图换成裁好的那张（`crop_path_for_scan`，没裁过
    就回落原图）：**裁好的图就是后续切题/裁块的底图**。默认配方 scan-entry
    不带增强，切题看到的就是这张裁好的彩色页；要增强的话，后面的「增强」一步
    会再把它换成 `processed_path`。
    """
    from cairn import scan_service  # 局部导入：配方层不拖 SQLAlchemy 进导入期

    scan = ctx.get("scan")
    db_url = ctx.get("db_url")
    if scan is not None and db_url:
        recorded = scan_service.crop_note_for(scan, db_url)
        if recorded:
            try:
                ctx["image_path"] = scan_service.crop_path_for_scan(scan, db_url)
            except ValueError:  # 路径被改脏：不换图，照旧读原来那张
                pass
            return recorded
    note, cropped = scan_service.crop_page_now(ctx.get("image_path") or ctx["path"])
    if cropped is not None:
        ctx["image_path"] = cropped
    return note


def _step_cut(ctx: dict) -> str:
    """切题：把整页切成一道一道。切题离不开认字，所以先要那页的识别行。"""
    if not tools.is_enabled("ocr"):
        # 切题是纯文本驱动的：认不了字就切不了题（与改造前的判断一字不差）
        raise StepUnavailable(tools.disabled_reason("ocr") or "文字识别已关闭")
    page = _page(ctx)
    blocks = ocr.cut_questions(page.lines, page.width, page.height)
    ctx["blocks"] = blocks
    return f"切出 {len(blocks)} 题"


def _step_ocr(ctx: dict) -> str:
    """认字：把识别行整理成页面要显示的样子（按栏、按行，一栏一段文本）。"""
    page = _page(ctx)
    ctx["lines"] = page.lines
    ctx["rows"] = _rows(page.lines)
    return f"{len(ctx['rows'])} 行"


def _page(ctx: dict):
    """这一页的识别结果：只读一次，切题与认字共用。

    图用"摆正"那步选出来的那张；那步被关掉（或没写进配方）就退回 `ctx["path"]`
    的原图——与 `read_image_path` 的回落一致，没摆正也能认字。

    失败的异常也记在 `ctx` 里：后一步不要再撞一次同样的墙（一次识别可能要几秒）。
    """
    if "page" in ctx:
        return ctx["page"]
    if "page_error" in ctx:
        raise ctx["page_error"]
    path = ctx.get("image_path") or ctx.get("path")
    if path is None:
        raise StepUnavailable("没有这一页的图")
    ctx["image_path"] = path
    try:
        ctx["page"] = ocr.read_page(str(path))
    except Exception as exc:  # noqa: BLE001 —— 原样留到下一步再抛
        ctx["page_error"] = exc
        raise
    return ctx["page"]


def _rows(lines) -> list[dict]:
    """识别行 → 页面表格：栏号、行号、整行文本、每块的字/置信度/坐标。"""
    rows: list[dict] = []
    for col_no, col_rows in enumerate(ocr.group_rows(lines), start=1):
        for row in col_rows:
            rows.append(
                {
                    "column": col_no,
                    "no": len(rows) + 1,
                    "text": ocr.to_text([[row]]),
                    "lines": [
                        {
                            "text": line.text,
                            "confidence": line.confidence,
                            "box": line.box,
                        }
                        for line in sorted(row, key=lambda item: min(x for x, _ in item.box))
                    ],
                }
            )
    return rows


_HANDLERS = {
    "preprocess": _step_preprocess,
    "crop": _step_crop,
    "enhance": _step_enhance,
    "cut": _step_cut,
    "ocr": _step_ocr,
}
