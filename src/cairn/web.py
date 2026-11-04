"""Minimal web UI (B4): three screens over the existing service layer.

Routes (no business logic here — every action delegates to a cairn service):

    GET      /                 redirect to /entry
    GET/POST /entry            manual entry (entry_service.create_error_item)
    POST     /entry/batch/preview   parse a paste into editable rows (cairn.parse)
    POST     /entry/batch/commit    persist the kept rows (create_error_items)
    GET      /tools           八样工具开着还是关了、关掉之后页面变成什么样
    GET      /practice         today's one question, answer hidden (fetch_practice)
    GET      /practice?item_id=N    that one item's ask face (get_practice_item)
    POST     /practice/reveal  show answer + analysis (practice_service.submit_answer)
    POST     /practice/grade   parent rates Again/Hard/Good/Easy (review_service.grade)
    GET      /strategies       available review strategies (registry, read-only)
    POST     /strategies/select    choose the learner's default strategy (unknown id → 400)
    GET      /practice/similar 同类题（按知识点标签捞，similar_service，只读）
    POST     /practice/similar/tag  补标（concept_service.attach_to_question）
    GET      /review-queue     pending low-confidence queue (graduation_service)
    POST     /review-queue/confirm   parent confirms a doubtful entry
    POST     /review-queue/reject    parent rejects a doubtful entry
    GET      /books            教材基本库：左选书、右按单元列出条目（book_service）
    POST     /books/add        勾选的条目批量加入错题库（book_service -> entry_service）
    POST     /books/{book_id}/units/{unit_id}/generate  一整个单元出成听写题（book_service）
    GET      /stats            three-subject statistics (stats_service, read-only)
    GET      /devices          摄像头/高拍仪设备与能力（capture，只读能力）
    POST     /devices/{id}/snapshot  抓一帧存盘入库（scan_service）
    GET      /upload           浏览器上传页（前端质量门：canvas 算指标、先判再传）
    POST     /scans/upload     上传原图 + 质量指标 JSON（multipart；scan_service.save_upload）
    GET      /scans            已拍列表（新的在前）
    GET      /scans/{id}       单张详情：默认原图（可切「增强后」）+ 设备 + 落盘路径 + 人工确认表单
    POST     /scans/{id}/confirm  人工确认入库（entry_service 建题，scan.question_id 回链）
    GET      /scans/{id}/image 图字节（?source=original|processed，默认走 read_image_path）
    GET      /scans/{id}/ocr   整页 OCR + 切题预览（现算，不落库）
    GET      /scans/{id}/questions  标错页：切题落库 + 原图叠加题框 + 逐题勾选（有红笔的预勾）
    POST     /scans/{id}/questions  只把勾选的题入库（scan_service -> entry_service）
    GET      /scans/{id}/frame      家长自己框题：摆正后的整页图 + 原生拖框
    POST     /scans/{id}/frame      收 N 个框，整批入库（scan_service.store_manual_frames）
    GET      /scans/{id}/upright    摆正后的整页图字节（0° 时原样发原图）
    GET      /api/scans/{id}/crop-info  这一页的裁切结论 + 四角 + 原图尺寸（只读 sidecar）
    GET      /scans/{id}/crop  裁边确认页：原图 + 四条线 + 四个可拖的角（Phase 2-c）
    POST     /scans/{id}/crop  家长改四角 → 从原图重裁（或 skip），结论追加进 sidecar
    GET      /questions/{id}   错题详情：题干 + 答案 + 卷面上裁下来的题块原图
    POST     /questions/{id}/answer  家长补录/清空标准答案（空 = NULL，超长拒收）
    POST     /questions/{id}/edit    人工改题干/答案/解析（空题干 400；留 edited_at/by）
    GET      /questions/{id}/crop  题块原图字节（没有就 404，页面显示"暂无原图"）
    GET      /exam             考试列表 + 新建表单（exam_service）
    POST     /exam             新建一场考试
    POST     /exam/delete      删除一场考试（过期记录不会自动删，只能家长点）
    GET      /export           JSON backup download (export_service)
    GET      /export/anki      Anki deck package download (anki_export)
    POST     /api/cut-feedback         one page's cut-correction snapshot (scan_service)
    GET      /api/cut-feedback/stats   four rates + mean IoU for a window
    GET      /api/cut-feedback/export  JSONL export (images opt-in)
    GET      /import           upload a backup into an empty DB (import_service)
    POST     /import           restore the uploaded .json (import_service.load_backup)
    GET      /tutor            讲解页（四段式引导的壳，段落文本走下面的 POST）
    POST     /tutor/start      建会话并送达 ask_thought（只提问，绝不给答案）
    POST     /tutor/next       推进一段（ask_thought → hint_1/2/3 → explain；讲完 409）
    POST     /tutor/reveal     孩子不想猜了，直接跳 explain（含答案）

Form bodies are parsed here, explicitly as UTF-8. We do not use FastAPI's
Form(...): starlette decodes application/x-www-form-urlencoded bodies as
latin-1 (starlette/datastructures.py:411), which turns UTF-8 Chinese into
mojibake before it ever reaches a service.

A single default learner is created on startup (no auth, no multi-user).
The engine is built in the lifespan hook so importing this module never
touches the database; DB URL comes from CAIRN_DB_URL (default ./cairn.db).
"""

from __future__ import annotations

import hashlib
import json
import io
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from cairn import (
    anki_export,
    book_service,
    capture,
    duplicate,
    entry_service,
    exam_service,
    export_service,
    glance_service,
    graduation_service,
    import_service,
    ocr,
    practice_service,
    recipe,
    review_service,
    scan_service,
    similar_service,
    stats_service,
    tools,
)
from cairn.workflow import api as workflow_api
from cairn.collection_api import router as collection_router
from cairn.recap import assessment as recap_assessment, service as recap_service
from cairn.models.recap import RecapAttempt
from cairn.workflow import execution as workflow_execution
from cairn.workflow import images as workflow_images
from cairn.workflow import human_actions as workflow_human_actions
from cairn.workflow import review as workflow_review
from cairn.workflow.scan_handlers import handlers as scan_workflow_handlers
from cairn.workflow.service import (WorkflowError, active_scan_runtime, ensure_collection_workflow, ensure_default_workflow,
                                    ensure_runnable_default)
from cairn.capture import CaptureError
from cairn.digitize import Digitized, DigitizeError, describe
from cairn.entry_service import EntryPayload, ValidationError
from cairn.parse import audio_cue, detect_lang, parse_pasted_text
from cairn.strategies.base import UnknownStrategyError
from cairn.strategies.registry import (
    ENV_DEFAULT_STRATEGY,
    default_strategy_id,
    get_strategy,
    list_strategies,
    strategy_id_for,
)
from cairn import tutor
from cairn.models import (
    Collection,
    ErrorItem,
    Learner,
    Question,
    REVIEW_STATUS_ARCHIVED,
    Review,
    Scan,
    Strategy,
    Unit,
    init_db,
    make_engine,
    make_session_factory,
)
# ScanQuestion 不在 models/__init__ 的导出表里（切题表是 v16 加的，包导出沿用旧清单），
# 直接从模块导入，不去动包导出面。
from cairn.models.scan import ScanQuestion
from cairn.models.learner import utcnow
from cairn.models.workflow_run import WorkflowReviewItem, WorkflowRun, WorkflowRunStep
from cairn.models.workflow import Workflow, WorkflowVersion

_TEMPLATES_DIR = Path(__file__).parent / "templates"
# 设计令牌与基础组件（tokens.css / base.css）。挂在 /static，
# 页面用 <link rel="stylesheet" href="/static/…"> 引用；纯静态文件，不经路由函数。
_STATIC_DIR = Path(__file__).parent / "static"
_DEFAULT_DB_URL = "sqlite:///./cairn.db"
# 家长补录标准答案的字数上限（详情页表单 / POST /questions/{id}/answer）。
# 超长多半是粘贴了整段解析，直接拒收比悄悄截断好：截断的答案会判错孩子。
ANSWER_MAX_CHARS = 2000
# 人工改题留痕用的时区（T3-a）：家里这块表按北京时间看，库里存 ISO8601 字符串，
# 带 +08:00 偏移，页面原样截到分钟显示，不在模板里再算一次时区。
_EDIT_TZ = timezone(timedelta(hours=8))
# 一次最多念多少字（与 scripts/tts_server.py 的上限一致；超过了拒收，不截断）。
SPEECH_MAX_CHARS = 200
# 工具被关掉时页面上的那句话：说清"缺了哪一步"和"你还能怎么干"。
CUT_OFF_NOTICE = "自动切题已关闭，请自己在照片上把题框出来（点上面的「我自己框」）。"
OCR_OFF_NOTICE = "文字识别已关闭，认不出文字和题号；请用「我自己框」把题框出来。"

# 上传这一条链默认走哪份配方（`src/cairn/recipes/*.yaml` 的名字）。
# 用 scan-entry（摆正 → 裁切 → 切题 → 认字，默认不带增强）；scan-enhance 留着不删，
# 想连增强一起跑就换它。关掉某个工具时那一步记 skipped_disabled，其余步骤照跑。
SCAN_RECIPE = "scan-entry"

# 复核页（P5-b）能选的模型后端：与 `cairn.digitize.client.PROVIDERS` 同一份取值，
# 但这一页要自己判（非法值 → 400 留在页面上），所以不把表单校验押在调用层异常上。
DIGITIZE_PROVIDERS = ("local", "cloud")
DIGITIZE_DEFAULT_PROVIDER = "local"
# 页面只给孩子家长看，不说产品名：本机 / 云端。
DIGITIZE_PROVIDER_LABELS = {"local": "本地", "cloud": "云端"}

# 裁边确认页（Phase 2-c）顶部那句人话：sidecar 里的 reason → 家长读得懂的一句话。
# 八条**逐字照卡**（docs/PLAN.md 的 P2-c 卡），谁改一个字，`tests/test_crop_page_ui.py`
# 的逐字断言就红。`note` 是给流水线看的（"跳过（纸太小…）"），不是给家长看的。
_CROP_REASON_TEXT = {
    "ok": "自动裁切完成，看一眼四个角对不对",
    "not_found": "没找到纸张，请把整张卷子拍进来",
    "already_full_page": "这张已经是整页了，不用裁",
    "cannot_separate_background": "纸和背景分不开，建议换深色桌面重拍",
    "paper_too_small": "卷子在画面里太小，靠近一点重拍",
    "degenerate_mask": "边缘没看清，请把四个角拖到纸上",
    "paper_cut_by_frame": "卷子被画面切掉了边，请重拍一张完整的",
    "weak_edge": "有一条边不太确定，请核对",
}
# 老卷子（上传早于 P2-a，没有 sidecar）没有 reason：不硬套上面八条里的任何一句。
_CROP_REASON_UNKNOWN = "这张照片没有自动裁切建议，请自己核对四个角"


def _crop_reason_text(reason: str | None) -> str:
    """reason → 页面上那句话；认不出的（含 None）用"没有裁切记录"那句。"""
    return _CROP_REASON_TEXT.get(reason or "", _CROP_REASON_UNKNOWN)


# 结论条的三档（卡 D2b · C）。文案是产品口径，逐字用：
#   正常「边缘对齐 4/4」／可疑「有一条边不确定」／失败沿用该闸门原来那句中文。
_CROP_VERDICT_OK = "边缘对齐 4/4"
_CROP_VERDICT_WEAK = "有一条边不确定"
# 这两条闸门都是"某条边没看清"，对家长是同一件事：先核对那条边。
_CROP_SUSPECT_REASONS = ("weak_edge", "degenerate_mask")


def _crop_verdict(reason: str | None) -> tuple[str, str, str]:
    """reason → (结论短句, 结论条的修饰类, 四边贴合度)。

    三档一一对应：ok = 正常（四条边都贴合）；weak_edge / degenerate_mask = 可疑
    （有一条边不确定）；其余闸门 = 失败，沿用 `_CROP_REASON_TEXT` 里原来那句。
    修饰类给模板拼 class，空串表示"正常档不加修饰"。
    """
    if reason == "ok":
        return _CROP_VERDICT_OK, "", "4/4"
    if reason in _CROP_SUSPECT_REASONS:
        return _CROP_VERDICT_WEAK, "verdict--warn", "3/4"
    return _crop_reason_text(reason), "verdict--fail", "—"


def _crop_facts(record: dict | None, scan) -> dict[str, str]:
    """数据条三格要显示的字（卡 D2b · C：纸占画面 / 纸面比周围亮 / 四边贴合度）。

    只念 sidecar 里的事实：老卷子没有亮度字段就显示「—」，不拿 0 冒充读数。
    """
    facts = record or {}
    area_ratio = facts.get("area_ratio")
    delta = facts.get("background_delta")
    width = facts.get("width") if facts.get("width") is not None else scan.width
    height = facts.get("height") if facts.get("height") is not None else scan.height

    if isinstance(area_ratio, (int, float)) and not isinstance(area_ratio, bool):
        area_pct = f"{round(float(area_ratio) * 100)}%"
    else:
        area_pct = "—"

    if isinstance(delta, int) and not isinstance(delta, bool):
        # 正数带 + 号（"比周围亮 135"）；0 就写 0，不写成 +0
        delta_text = f"+{delta}" if delta > 0 else str(delta)
    else:
        delta_text = "—"

    return {
        "area_pct": area_pct,
        "delta_text": delta_text,
        "edges_text": _crop_verdict(facts.get("reason"))[2],
        "size_text": (
            f"{width}×{height}"
            if isinstance(width, int) and isinstance(height, int)
            else "原图尺寸未知"
        ),
    }


def _stage_hint(stage: str, record: dict | None, stored: int) -> str:
    """主按钮下面那行等宽小字：机器给的依据，不是建议。

    三种状态各有各的依据，模板不写死任何一条：
    `ready` 报题数；`crop` 念 sidecar 里那句跳过原因（没有记录就说明没记录）；
    `select` 报纸占画面多少——家长拖过四角的那次重裁，sidecar 里的 `area_ratio`
    还是机器当初那个读数，拿它报数会撒谎，只说"按你拖的四角重裁过"。
    """
    if stage == scan_service.STAGE_READY:
        return f"已收好 {stored} 道"
    if stage == scan_service.STAGE_CROP:
        note = (record or {}).get("note")
        return note if isinstance(note, str) and note else "还没有裁切记录"
    if (record or {}).get("human") == "adjusted":
        return "按你拖的四角重裁过"
    ratio = (record or {}).get("area_ratio")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        return f"自动裁切完成 · 纸占 {round(float(ratio) * 100)}%"
    return "自动裁切完成"


def _undo_dict_fill(
    session: Session, item: ErrorItem, payload: EntryPayload
) -> None:
    """词典关掉时把家长的话原样写回题面。

    补全发生在 `entry_service` 内部（本卡写范围不含 service），所以在 web 层
    撤销：content 还原成家长填的那句，朗读 cue 用 `parse.audio_cue` 重算——
    这两条正是服务层查不到词典时**本该**得到的值，不是另写一套业务逻辑。
    调用方负责 commit。
    """
    question = session.get(Question, item.question_id)
    if question is None:
        return
    lang = payload.lang if payload.lang in ("en", "zh") else detect_lang(payload.answer)
    question.content = payload.content
    question.audio_text = (payload.audio_text or "").strip() or audio_cue(
        payload.content, payload.answer, lang
    )


def _undo_dict_fill_batch(
    session: Session, items, payloads: list[EntryPayload]
) -> None:
    """批量入库的同一件事：按 answer 找回行（批内按 answer 去重，键唯一）。"""
    by_answer = {(payload.answer or "").strip(): payload for payload in payloads}
    for item in items:
        question = session.get(Question, item.question_id)
        if question is None:
            continue
        payload = by_answer.get((question.answer or "").strip())
        if payload is not None:
            _undo_dict_fill(session, item, payload)


def _drop_processed(session: Session, scan: Scan, db_url: str) -> None:
    """预处理关掉时把 service 顺手写下的处理图丢掉、列置空。

    预处理发生在 `scan_service.save_upload` 内部（本卡写范围不含 service），
    所以由 web 层在调用点兜住：落库结果与"从来没处理过"一致——原图一个字节
    不改、`processed_path` 是 NULL、读图链路回落原图。
    """
    if not scan.processed_path:
        return
    stale = scan_service.processed_image_path(scan, db_url)
    if stale is not None:
        stale.unlink(missing_ok=True)
    scan.processed_path = None
    session.commit()


async def _read_form(request: Request) -> dict[str, str]:
    """Parse the urlencoded body as UTF-8, explicitly (no latin-1 fallback)."""
    body = await request.body()
    if not body:
        return {}
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


def _edit_stamp_now() -> str:
    """这次人工编辑的时间戳：UTC+8 的 ISO8601，精确到秒（如 2026-11-02T04:12:33+08:00）。"""
    return datetime.now(_EDIT_TZ).isoformat(timespec="seconds")


def _date_text(lang: str = "zh") -> str:
    """页面头部日期（如 `11月2日 周一`），按 UTC+8 的自然日取，与录入时区同源。"""
    now = datetime.now(_EDIT_TZ)
    if lang == "en":
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        return f"{weekdays[now.weekday()]}, {months[now.month - 1]} {now.day}"
    return f"{now.month}月{now.day}日 周{'一二三四五六日'[now.weekday()]}"


def _app_version() -> str:
    """版本号只住在 pyproject.toml：先直接读它，读不到再退回包元数据。"""
    try:
        text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("cairn")
    except Exception:
        return ""


def _capture_day_word(captured_at: datetime) -> str:
    """这张卷子是哪天拍的——「今天」/「昨天」/ 更早回空串（卡片就不写日期词）。

    库里 naive 存 UTC；按 UTC+8 自然日比，和头部日期同一时区。文案红线：
    「昨天拍的」不许套在今天刚拍的卷子上，所以日期词按真实拍摄日走。
    """
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    captured_day = captured_at.astimezone(_EDIT_TZ).date()
    delta = (datetime.now(_EDIT_TZ).date() - captured_day).days
    return {0: "今天", 1: "昨天"}.get(delta, "")


def _today_view(request: Request, session: Session) -> dict:
    """「今天」页装配（G2-2）：所有数字真实计数，多处同源。

    - mode：todo（今天有 N 题要练）/ done（今天的练习做完了，剧本 B4）/
      empty（今天还没有要复习的题）。
    - due_count = practice_today().remaining——「今天有 N 题要练」的 N，与
      「开始今天的练习」取的题池同源（新题 + 到期题里还没练的部分）。
    - pending_*：还没圈完的卷子（stage != ready），**最久等的那张先续走**
      （断点只在卷子粒度，蓝图 §3.2）。
    - glance_text：近况一句话（glance_service，与「最近怎么样」页同源同数；
      两个数都为 0 时整行不出现，0 不上屏）。
    - due_tomorrow：明天（UTC+8 自然日）到期的题数——「还有 M 道题明天见」。
    - badge_today == due_count、badge_book == 题库总数：侧栏徽标与今日卡同源。
    """
    learner_id = request.app.state.learner_id
    now = utcnow()
    stat = stats_service.practice_today(session, learner_id, now=now, tz=_EDIT_TZ)
    glance = glance_service.build_glance(session, learner_id, now=now, tz=_EDIT_TZ)

    scans = scan_service.list_scans(session, learner_id)
    pending = [
        s
        for s in scans
        if scan_service.stage_for(session, s, request.app.state.db_url)
        != scan_service.STAGE_READY
    ]
    first_pending = pending[-1] if pending else None  # 列表新→旧，倒着取最久等的
    pending_count = len(pending)
    pending_href = None
    pending_text = ""
    pending_sub = ""
    if first_pending is not None:
        stage = scan_service.stage_for(
            session, first_pending, request.app.state.db_url
        )
        has_run = session.scalar(select(WorkflowRun.id).where(WorkflowRun.scan_id == first_pending.id)) is not None
        step = "workflow" if has_run else ("crop" if stage == scan_service.STAGE_CROP else "select")
        pending_href = f"/scans/{first_pending.id}/{step}"
        word = _capture_day_word(first_pending.captured_at)
        if has_run:
            label = {scan_service.STAGE_CROP: "等着裁边", scan_service.STAGE_SELECT: "等着圈选",
                     scan_service.STAGE_REVIEW: "等着核对", scan_service.STAGE_FAILED: "需要重试"}.get(stage, "等着处理")
            pending_text = f"{word}拍的卷子{label}"
            pending_sub = f"{word}拍的{label}"
        else:
            pending_text = f"{word}拍的卷子还没圈"
            pending_sub = f"{word}拍的还没圈"

    now8 = now.astimezone(_EDIT_TZ)
    tomorrow8 = now8.date() + timedelta(days=1)
    day_after8 = tomorrow8 + timedelta(days=1)
    due_tomorrow = 0
    for (due,) in session.execute(
        select(ErrorItem.next_due_at).where(
            ErrorItem.learner_id == learner_id,
            ErrorItem.review_status != REVIEW_STATUS_ARCHIVED,
            ErrorItem.next_due_at.is_not(None),
        )
    ):
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if tomorrow8 <= due.astimezone(_EDIT_TZ).date() < day_after8:
            due_tomorrow += 1

    if glance.practiced_this_week and glance.repeated_wrong:
        glance_text = (
            f"这周练了 {glance.practiced_this_week} 题 · "
            f"{glance.repeated_wrong} 题还在反复错"
        )
    elif glance.practiced_this_week:
        glance_text = f"这周练了 {glance.practiced_this_week} 题"
    elif glance.repeated_wrong:
        glance_text = f"{glance.repeated_wrong} 题还在反复错"
    else:
        glance_text = ""

    if stat.remaining > 0:
        mode = "todo"
    elif stat.completed > 0:
        mode = "done"
    else:
        mode = "empty"

    return {
        "date_text": _date_text(),
        "nav_active": "today",
        "nav_off": () if stat.total_items else ("book",),
        "mode": mode,
        "due_count": stat.remaining,
        "done_count": stat.completed,
        "due_tomorrow": due_tomorrow,
        "pending_count": pending_count,
        "pending_href": pending_href,
        "pending_text": pending_text,
        "pending_sub": pending_sub,
        "glance_text": glance_text,
        "badge_today": stat.remaining,
        "badge_book": stat.total_items,
    }


def _edited_at_text(edited_at: str | None) -> str | None:
    """库里的时间戳 → 页面上那行小字（`2026-11-02 04:12`，秒不显示）。

    只截串、不解析：存进去的就是带偏移的 ISO8601，换算时区只会把它挪错。
    认不出的形状（手工改过的库）原样回显，页面至少不 500。
    """
    if not edited_at:
        return None
    return edited_at.replace("T", " ")[:16]


async def _read_json(request: Request) -> dict:
    """Parse a JSON body; not JSON → try urlencoded form（curl 手测友好）。"""
    body = await request.body()
    if body:
        try:
            payload = json.loads(body.decode("utf-8"))
            if isinstance(payload, dict):
                return payload
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return dict(await _read_form(request))


async def _read_form_pairs(request: Request) -> list[tuple[str, str]]:
    """Same decoding as _read_form, but keeps repeated keys (checkbox 多选)。"""
    body = await request.body()
    if not body:
        return []
    return parse_qsl(body.decode("utf-8"), keep_blank_values=True)


def _file_sha256(path: Path) -> str:
    """SHA256 of a file on disk, streamed (page images are a few MB).

    Used as `image_sha256` in the cut-feedback snapshot: it identifies the sheet
    the bboxes were measured on, so records from different uploads of the same
    photograph line up (docs/CUT-FEEDBACK-SPEC.md).
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cut_params_hash() -> str:
    """Fingerprint of the cut parameters this page's data was produced with.

    `params_hash` exists so records made under different thresholds stay
    separable (docs/CUT-FEEDBACK-SPEC.md). What shapes a select-page snapshot
    here is the recipe's step list plus the manual-frame minimum side; hashing a
    canonical JSON of exactly those keeps the value stable between submissions
    of the same page (which is what makes the overwrite-on-resubmit path work).
    """
    try:
        spec = recipe.load(SCAN_RECIPE)
        steps = [[step.name, step.tool, step.on_error] for step in spec.steps]
    except recipe.RecipeError:  # 配方坏了也别挡页面：只剩名字参与指纹
        steps = []
    payload = {
        "recipe": SCAN_RECIPE,
        "steps": steps,
        "min_side": scan_service.MANUAL_MIN_FRAME_SIDE,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _cut_feedback_bound(raw: str | None, *, end: bool) -> str | None:
    """Turn a YYYY-MM-DD query parameter into an ISO8601 UTC text bound.

    `created_at` is stored as ISO8601 text, so lexical comparison is correct and
    the `(created_at)` index stays usable. `end=True` makes the bound exclusive
    by moving to the next midnight, which keeps the whole `to` day included.
    """
    value = (raw or "").strip()
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"日期格式应为 YYYY-MM-DD：{value}"
        ) from exc
    if end:
        day = day + timedelta(days=1)
    return f"{day.isoformat()}T00:00:00+00:00"


def _overlay_style(
    bbox: tuple[int, int, int, int] | None, page_width: int, page_height: int
) -> str | None:
    """题框 → 叠加在原图上的绝对定位样式（百分比，页面缩放后仍然对齐）。

    bbox 是**摆正后**页面的坐标，所以百分比要按摆正后的尺寸算；图片本身的
    旋转与缩放由模板里的 img 样式负责，框只在这个坐标系里定位。
    """
    if bbox is None or page_width <= 0 or page_height <= 0:
        return None
    x0, y0, x1, y1 = bbox
    x0, x1 = max(0, min(x0, page_width)), max(0, min(x1, page_width))
    y0, y1 = max(0, min(y0, page_height)), max(0, min(y1, page_height))
    if x1 <= x0 or y1 <= y0:
        return None
    return (
        f"left:{x0 / page_width * 100:.2f}%;"
        f"top:{y0 / page_height * 100:.2f}%;"
        f"width:{(x1 - x0) / page_width * 100:.2f}%;"
        f"height:{(y1 - y0) / page_height * 100:.2f}%;"
    )


def _frame_fields(pairs: list[tuple[str, str]]) -> list[tuple[int, str]]:
    """家长拖的框：`frame_<n>=x0,y0,x1,y1`，按 n 排好（表单字段顺序不可靠）。"""
    fields: list[tuple[int, str]] = []
    for key, value in pairs:
        if not key.startswith("frame_"):
            continue
        index = key[len("frame_"):]
        if index.isdigit():
            fields.append((int(index), value))
    fields.sort(key=lambda item: item[0])
    return fields


def _parse_frames(
    fields: list[tuple[int, str]], page_width: int, page_height: int
) -> list[tuple[int, int, int, int]] | None:
    """表单里的框 → 页面像素坐标；有一个读不出来就整体返回 None（宁可不收）。

    夹进页面范围并摆正左上/右下：家长是缩放后拖的，坐标偶尔差一两像素到页外。
    """
    frames: list[tuple[int, int, int, int]] = []
    for _index, value in fields:
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            return None
        try:
            x0, y0, x1, y1 = (round(float(part)) for part in parts)
        except ValueError:
            return None
        box = scan_service.clip_frame((x0, y0, x1, y1), page_width, page_height)
        if box is None:
            return None
        frames.append(box)
    return frames


def _red_mark_flags(
    image_path: Path | None, rows: list[ScanQuestion], angle: int
) -> list[bool]:
    """逐题判"题框里有没有红笔"，长度与 rows 对齐（只判有无，不判勾/叉）。

    库里存的 bbox 是**摆正后**的页面坐标，`angle` 就是这次切题时的方向角，
    所以按这个角把原图转过来再判，框才不会整体错位。

    三种情况一律给"没有红笔"，页面照常出（标错页不能因为红笔检测 500）：
    没给图片路径、没开检测（CAIRN_RED_MARK=0）、图读不了。
    """
    flags = [False] * len(rows)
    if image_path is None or not rows or not ocr.redmark.is_enabled():
        return flags
    targets: list[tuple[int, tuple[int, int, int, int]]] = []
    for index, row in enumerate(rows):
        box = scan_service.row_bbox(row)
        if box is not None:
            targets.append((index, box))
    if not targets:
        return flags
    detected = ocr.detect_in_page(image_path, [box for _, box in targets], angle)
    for (index, _), flagged in zip(targets, detected):
        flags[index] = flagged
    return flags


def _tutor_payload(message) -> dict:
    """/tutor/* 的统一 JSON 返回：当前段文本 + 新 stage。"""
    return {
        "session_id": message.session_id,
        "stage": message.stage,
        "text": message.text,
        "leak_blocked": bool(message.leak_blocked),
    }


def create_app(db_url: str | None = None) -> FastAPI:
    """Build the web app. *db_url* overrides CAIRN_DB_URL (used by tests)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(app.state.db_url)
        init_db(engine)
        factory = make_session_factory(engine)
        with factory() as session:
            learner = session.scalars(
                select(Learner).order_by(Learner.id.asc()).limit(1)
            ).first()
            if learner is None:
                learner = Learner(name="default")
                session.add(learner)
                session.commit()
            book_service.ensure_default_books(session)
            # 策略目录行与代码 registry 对齐（幂等）：新策略上线后老库也补上
            # 目录行，learner.strategy_id 的 FK 才写得进去。
            for entry in list_strategies():
                if session.get(Strategy, entry["id"]) is None:
                    session.add(Strategy(id=entry["id"], name=entry["name"]))
            ensure_default_workflow(session)
            ensure_collection_workflow(session)
            ensure_runnable_default(session)
            session.commit()
            app.state.learner_id = learner.id
        app.state.session_factory = factory
        # 摄像头后端只建一次：AVFoundation 的导入与设备发现都不便宜，但也不该按请求重建。
        # 真后端不可用时工厂回退假设备并打一次警告，页面顶部会常驻标注。
        app.state.cameras = capture.get_backend(
            os.environ.get("CAIRN_CAMERA_BACKEND", "auto")
        )
        # 出题配额生效值（BUKA 配额卡）：启动时打印，家长可对照环境变量核查。
        _size, _green, _red = practice_service.batch_config()
        print(
            f"[cairn] practice batch config: "
            f"size={_size} green_ratio={_green} red_ratio={_red}"
        )
        # 默认复习策略生效值（策略层卡）：learner 未自选策略时用这个。
        print(
            f"[cairn] default review strategy: {default_strategy_id()} "
            f"(env {ENV_DEFAULT_STRATEGY})"
        )
        yield
        engine.dispose()

    app = FastAPI(title="Cairn", lifespan=lifespan)
    canvas_origins = [
        origin.strip() for origin in os.environ.get(
            "CAIRN_WORKFLOW_CANVAS_ORIGINS", "http://localhost:5173"
        ).split(",") if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=canvas_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(workflow_api.router)
    app.include_router(collection_router)
    app.state.db_url = db_url or os.environ.get("CAIRN_DB_URL", _DEFAULT_DB_URL)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # 静态资源（设计令牌 / 基础组件）。不碰任何业务路由：/static 只发文件。
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    def render(request: Request, name: str, /, *, status_code: int = 200, **ctx) -> object:
        # v3 壳的公共变量：日期一律给真值；导航高亮页自己传（默认无高亮）
        ctx.setdefault("date_text", _date_text())
        section = {
            "scans": "capture", "scan": "capture", "scan_frame": "capture",
            "scan_questions": "capture", "devices": "capture", "digitize": "book",
            "books": "book", "exam": "today", "tutor": "today",
            "import": "settings", "tools": "settings",
        }.get(name, "")
        ctx.setdefault("nav_active", section)
        ctx.setdefault("ui_lang", "en" if request.query_params.get("lang") == "en" else "zh")
        return templates.TemplateResponse(
            request, f"{name}.html", ctx, status_code=status_code
        )

    def _render_practice(request: Request, session: Session | None, /, **ctx) -> object:
        """练习页统一出口：倒计时条（没考试时为 None，模板整块不出现）+
        策略选择控件（当前策略与可选列表；?switched=1 时给切换提示）。

        朗读关掉（CAIRN_TOOL_TTS=0）时把 `is_dictation` 按下去：practice.html
        用它控制「播放」按钮，按钮就不出现（模板不在本卡写范围，不改模板）。
        """
        item = ctx.get("item")
        if item is not None and not tools.is_enabled("tts"):
            ctx["item"] = replace(item, is_dictation=False)
        extra: dict = {
            "strategy_switched": request.query_params.get("switched") == "1"
        }
        if session is not None:
            learner = session.get(Learner, request.app.state.learner_id)
            extra["strategy_options"] = list_strategies()
            extra["current_strategy_id"] = strategy_id_for(learner)
            extra.update(_practice_numbers(session, request.app.state.learner_id))
        return render(
            request, "practice", exam_bar=_exam_bar(request, session), **extra, **ctx
        )

    def _practice_numbers(session: Session, learner_id: int) -> dict:
        """练习页要的两组真实数（G2-5）：

        - practice_x / practice_total：「练一题（第 x/N 题）」——与「开始今天的练习」
          的题池同源（practice_today：x = 已练 + 1，N = 已练 + 还剩）。
        - passed_today / again_today：今天的结算句「x 题过关，y 题明天再来」——
          按今天（UTC+8 自然日）的自评定：good/easy = 过关，again/hard = 明天再来。
        """
        now = utcnow()
        stat = stats_service.practice_today(session, learner_id, now=now, tz=_EDIT_TZ)
        total = stat.completed + stat.remaining
        x = stat.completed + 1 if stat.remaining else max(stat.completed, 1)
        day_start = now.astimezone(_EDIT_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        passed = 0
        again = 0
        for rating, reviewed_at in session.execute(
            select(Review.rating, Review.reviewed_at).where(
                Review.error_item_id.in_(
                    select(ErrorItem.id).where(ErrorItem.learner_id == learner_id)
                )
            )
        ):
            moment = reviewed_at
            if moment is not None and moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            if moment is None or moment.astimezone(_EDIT_TZ) < day_start:
                continue
            if rating in ("good", "easy"):
                passed += 1
            else:
                again += 1
        return {
            "practice_x": x,
            "practice_total": total,
            "passed_today": passed,
            "again_today": again,
        }

    def _render_similar(
        request: Request, session: Session, item_id: int, error: str | None = None
    ):
        """同类页统一出口：view 取不到（题不存在/不是这个 learner）时只报错，不编数据。"""
        try:
            view = similar_service.build_similar_view(
                session, request.app.state.learner_id, item_id
            )
        except ValidationError as exc:
            return _render_practice(
                request, session, mode="similar", view=None, error=error or str(exc)
            )
        return _render_practice(request, session, mode="similar", view=view, error=error)

    @app.get("/")
    def index(request: Request):
        """v3 主入口：「今天」页。三态装配见 `_today_view`（数字全真实、同源）。"""
        with request.app.state.session_factory() as session:
            ctx = _today_view(request, session)
        return render(request, "today", **ctx)

    @app.get("/tools")
    def tools_page(request: Request):
        """运维排查页：八个工具谁开着、谁关了、为什么关，外加"当前配方怎么走"。

        只读 `tools.status()`（registry 是唯一事实源）与上传默认配方（YAML 是
        唯一事实源），页面不做任何判断——加一个工具只在 registry 里加一行，改一步
        顺序只改 YAML，这里自动跟着变。
        """
        rows = tools.status()
        recipe_error = None
        recipe_rows: list[dict] = []
        current = None
        try:
            current = recipe.load(SCAN_RECIPE)
        except recipe.RecipeError as exc:
            # 配方坏了不该把排查页也弄崩：把原因写在这一节里
            recipe_error = str(exc)
        else:
            recipe_rows = [
                {
                    "name": step.name,
                    "tool": step.tool,
                    "title": tools.get(step.tool).title,
                    "enabled": tools.is_enabled(step.tool),
                }
                for step in current.steps
            ]
        return render(
            request,
            "tools",
            rows=rows,
            disabled_count=sum(1 for row in rows if not row["enabled"]),
            recipe=current,
            recipe_rows=recipe_rows,
            recipe_error=recipe_error,
        )

    @app.post("/v1/speech")
    async def speech(request: Request):
        """朗读出口：文字 → WAV。tts 关掉时 503，调用方换"看文字"。

        合成与缓存走 `cairn.voice.tts`（与 scripts/tts_server.py 同一套），
        web 只做参数校验和状态码，不另写一套语音逻辑。
        """
        if not tools.is_enabled("tts"):
            return JSONResponse(
                {"error": tools.disabled_reason("tts")}, status_code=503
            )
        payload = await _read_json(request)
        text = str(payload.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "没有要念的文字"}, status_code=400)
        if len(text) > SPEECH_MAX_CHARS:
            return JSONResponse(
                {"error": f"一次最多念 {SPEECH_MAX_CHARS} 字"}, status_code=400
            )
        # 惰性导入：不念就不拖语音层（voice 包会拉 router/turn）。
        from cairn.voice import tts as voice_tts

        try:
            audio = voice_tts.synthesize(
                text, voice=str(payload.get("voice") or "").strip() or None
            )
        except voice_tts.TTSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return Response(content=audio, media_type="audio/wav")

    @app.get("/entry")
    def entry_form(request: Request):
        return render(request, "entry")

    @app.post("/entry")
    async def entry_submit(request: Request):
        form = await _read_form(request)
        payload = EntryPayload(
            subject=form.get("subject", ""),
            question_type=form.get("question_type", ""),
            content=form.get("content", ""),
            answer=form.get("answer", ""),
            analysis=form.get("analysis") or None,
            lang=_explicit_lang(form.get("lang")),
            audio_text=form.get("audio_text") or None,
        )
        # 「仍然添加」= the parent already saw the similar warning once
        force = form.get("force") == "1"
        with request.app.state.session_factory() as session:
            try:
                match = entry_service.find_entry_duplicate(
                    session, request.app.state.learner_id, payload
                )
                if match is not None and not match.is_exact and not force:
                    return render(
                        request,
                        "entry",
                        form=payload,
                        duplicate_warning=duplicate.describe_similar(match),
                        force_offer=True,
                    )
                item = entry_service.create_error_item(
                    session,
                    request.app.state.learner_id,
                    payload,
                    reject_duplicates=True,
                )
                if not tools.is_enabled("dict"):
                    # 词典关掉：service 内部补过的释义/拼音撤掉，按家长原话落库
                    _undo_dict_fill(session, item, payload)
                    session.commit()
            except ValidationError as exc:
                return render(request, "entry", error=str(exc), form=payload)
        return render(request, "entry", created_id=item.id)

    @app.post("/entry/batch/preview")
    async def batch_preview(request: Request):
        """解析粘贴内容，渲染可改可删的预览列表（不写库）。

        每行都查重：先比库里已有的，再比本批前面几行（互查）；命中的行在
        预览里写明原因、勾选框默认不勾，家长可以自己勾回来。
        """
        form = await _read_form(request)
        pasted = form.get("pasted", "")
        with request.app.state.session_factory() as session:
            candidates = entry_service.existing_candidates(
                session, request.app.state.learner_id
            )
        preview = []
        for row_no, item in enumerate(parse_pasted_text(pasted), start=1):
            match = duplicate.find_duplicate(item.content, item.answer, candidates)
            preview.append(
                {
                    "content": item.content,
                    "answer": item.answer,
                    "lang": item.lang,
                    "audio_text": item.audio_text,
                    "duplicate_note": (
                        duplicate.describe_preview_row(match, row_no, item.content)
                        if match is not None
                        else ""
                    ),
                }
            )
            candidates.append(
                duplicate.Candidate(content=item.content, answer=item.answer)
            )
        return render(request, "entry", preview=preview, pasted=pasted)

    @app.post("/entry/batch/commit")
    async def batch_commit(request: Request):
        """预览页确认后批量入库：一次事务，重复词跳过并计数。"""
        form = await _read_form(request)
        payloads = _batch_payloads(form)
        if not payloads:
            return render(request, "entry", error="没有可提交的词条")
        with request.app.state.session_factory() as session:
            try:
                result = entry_service.create_error_items(
                    session, request.app.state.learner_id, payloads
                )
            except ValidationError as exc:
                return render(request, "entry", error=str(exc))
            if not tools.is_enabled("dict"):
                _undo_dict_fill_batch(session, result.created, payloads)
                session.commit()
        return render(
            request,
            "entry",
            batch_result={
                "created": result.created_count,
                "skipped": result.skipped_count,
            },
        )

    @app.get("/practice")
    def practice(request: Request, item_id: int | None = None):
        """今日一题；带 `?item_id=` 时给指定错题的答题面（同类题入口用）。

        两条路都只拿 `PracticeItem`（结构上没有 answer），后续揭示/判分不变。
        打开练习页时顺带跑一次 `plan_exam_pull_forward`（幂等、只改 next_due_at），
        冲刺期的「提前拉题」因此不需要额外的定时任务。
        """
        today = _today()
        with request.app.state.session_factory() as session:
            exam_service.plan_exam_pull_forward(
                session, request.app.state.learner_id, today
            )
            if item_id is not None:
                item = practice_service.get_practice_item(
                    session, request.app.state.learner_id, item_id
                )
                if item is None:
                    return _render_practice(
                        request,
                        session,
                        mode="ask",
                        item=None,
                        error="这道题不在你的题库里",
                    )
            else:
                items = practice_service.fetch_practice(
                    session, request.app.state.learner_id, utcnow(), limit=10
                )
                item = items[0] if items else None
            return _render_practice(request, session, mode="ask", item=item)

    def _recap_page(request: Request, item_id: int, lang: str, *, error: str = "",
                    text: str = "", request_key: str | None = None, status_code: int = 200,
                    learner_id: int | None = None):
        owner = request.app.state.learner_id if learner_id is None else learner_id
        with request.app.state.session_factory() as session:
            try:
                _, question = recap_service._item(session, owner, item_id)
            except recap_service.RecapError:
                raise HTTPException(status_code=404, detail="Question not available")
            pending = session.scalar(select(RecapAttempt).where(
                RecapAttempt.learner_id == owner,
                RecapAttempt.error_item_id == item_id,
                RecapAttempt.status == "needs_followup",
            ).order_by(RecapAttempt.id.desc()).limit(1))
            return render(request, "recap", status_code=status_code, lang=lang, ui_lang=lang,
                          date_text=_date_text(lang), item_id=item_id, question=question,
                          pending=pending, error=error, text=text,
                          request_key=request_key or secrets.token_hex(16))

    @app.get("/practice/recap/{item_id}")
    def recap_page(request: Request, item_id: int, lang: str = "zh"):
        return _recap_page(request, item_id, "en" if lang == "en" else "zh")

    @app.post("/practice/recap/{item_id}")
    async def recap_start(request: Request, item_id: int):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        lang = "en" if form.get("lang") == "en" else "zh"
        text = form.get("text", "")
        key = form.get("request_key", "")
        def save():
            with request.app.state.session_factory() as session:
                try:
                    attempt = recap_service.start(session, learner_id, item_id,
                                                  key, text, unsure=form.get("action") == "unsure")
                    attempt_id = attempt.id
                    session.commit()
                    return attempt_id
                except Exception:
                    session.rollback()
                    raise
        try:
            attempt_id = await run_in_threadpool(save)
        except recap_service.RecapError as exc:
            error = "Unable to save. Check your text or refresh and try again." if lang == "en" else str(exc)
            return _recap_page(request, item_id, lang, error=error, text=text, request_key=key,
                               status_code=400, learner_id=learner_id)
        return RedirectResponse(f"/practice/recap/result/{attempt_id}?lang={lang}", status_code=303)

    def _recap_result(request: Request, attempt_id: int, lang: str, *, error: str = "",
                      text: str = "", status_code: int = 200, learner_id: int | None = None):
        owner = request.app.state.learner_id if learner_id is None else learner_id
        with request.app.state.session_factory() as session:
            try:
                attempt = recap_service.get_attempt(session, owner, attempt_id)
                _, question = recap_service._item(session, owner, attempt.error_item_id)
            except recap_service.RecapError:
                raise HTTPException(status_code=404, detail="Recall not available")
            prompt = recap_assessment.followup_prompt(attempt.focus, question.answer, lang)
            return render(request, "recap_result", status_code=status_code, lang=lang, ui_lang=lang,
                          date_text=_date_text(lang), attempt=attempt, question=question,
                          prompt=prompt, error=error, text=text)

    @app.get("/practice/recap/result/{attempt_id}")
    def recap_result(request: Request, attempt_id: int, lang: str = "zh"):
        return _recap_result(request, attempt_id, "en" if lang == "en" else "zh")

    @app.post("/practice/recap/result/{attempt_id}/followup")
    async def recap_followup(request: Request, attempt_id: int):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        lang = "en" if form.get("lang") == "en" else "zh"
        text = form.get("text", "")
        def save():
            with request.app.state.session_factory() as session:
                try:
                    recap_service.follow_up(session, learner_id, attempt_id, text)
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
        try:
            await run_in_threadpool(save)
        except recap_service.RecapError as exc:
            error = "Unable to save. Check your text or start again." if lang == "en" else str(exc)
            return _recap_result(request, attempt_id, lang, error=error, text=text,
                                 status_code=400, learner_id=learner_id)
        return RedirectResponse(f"/practice/recap/result/{attempt_id}?lang={lang}", status_code=303)

    @app.get("/practice/similar")
    def practice_similar(request: Request, item_id: int | None = None):
        """同类题列表：取数与判断全在 similar_service，页面只渲染。"""
        if item_id is None:
            return _render_practice(request, None, mode="similar", view=None, error="缺少 item_id")
        with request.app.state.session_factory() as session:
            return _render_similar(request, session, item_id)

    @app.post("/practice/similar/tag")
    async def practice_similar_tag(request: Request):
        """老数据补标：选已有知识点或新建一个，挂回原题后回到同类页。"""
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        item_id = _int_field(form, "item_id")
        if item_id is None:
            return RedirectResponse("/practice", status_code=303)
        with request.app.state.session_factory() as session:
            try:
                similar_service.tag_source(
                    session,
                    learner_id,
                    item_id,
                    concept_id=_int_field(form, "concept_id"),
                    new_name=form.get("new_concept") or None,
                )
            except (ValidationError, ValueError) as exc:
                # 名称/知识点不合法：留在页面上重填，不吞掉错误
                return _render_similar(request, session, item_id, error=str(exc))
        return RedirectResponse(f"/practice/similar?item_id={item_id}", status_code=303)

    @app.post("/practice/reveal")
    async def practice_reveal(request: Request):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        item_id = _int_field(form, "item_id")
        if item_id is None:
            return _render_practice(
                request, None, mode="ask", item=None, error="item_id 无效"
            )
        with request.app.state.session_factory() as session:
            try:
                if tools.is_enabled("grading"):
                    revealed, grade = practice_service.submit_and_judge(
                        session,
                        learner_id,
                        item_id,
                        form.get("learner_answer", ""),
                    )
                else:
                    # 判题关掉：只揭示、不判定，一律交家长两键（复用 ManualGrade，
                    # 一行簿记都不写，与"没有标准答案"是同一个分支）。
                    revealed = practice_service.submit_answer(
                        session,
                        learner_id,
                        item_id,
                        form.get("learner_answer", ""),
                    )
                    grade = practice_service.ManualGrade()
            except ValidationError as exc:
                return _render_practice(
                    request, session, mode="ask", item=None, error=str(exc)
                )
            return _render_practice(
                request,
                session,
                mode="reveal",
                revealed=revealed,
                learner_answer=form.get("learner_answer", ""),
                grade=grade,
                # 空答案分支（ManualGrade）：模板据此换成「看原图 + 两键」，
                # 不再显示自动判定与四档评分。普通 GradeResult 没有这个字段。
                needs_manual=bool(getattr(grade, "needs_manual", False)),
            )

    @app.post("/practice/grade")
    async def practice_grade(request: Request):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        item_id = _int_field(form, "item_id")
        if item_id is None:
            return _render_practice(
                request, None, mode="graded", error="item_id 无效"
            )
        with request.app.state.session_factory() as session:
            try:
                review = review_service.grade(
                    session, item_id, form.get("rating", ""), now=utcnow(), learner_id=learner_id
                )
            except ValidationError as exc:
                return _render_practice(request, session, mode="graded", error=str(exc))
            return _render_practice(
                request,
                session,
                mode="graded",
                graded_rating=review.rating,
                next_due_at=review.next_due_at,
            )

    @app.get("/strategies")
    def strategies_index():
        """可用复习策略列表（id + describe），registry 是唯一事实源。"""
        return [
            {"id": entry["id"], "describe": entry["describe"]}
            for entry in list_strategies()
        ]

    @app.post("/strategies/select")
    async def strategies_select(request: Request):
        """把一个孩子的默认复习策略写成 learner.strategy_id。

        不存在的 id → 400（不许 500）；合法 id 的目录行由 lifespan 预种，
        FK 不会拦。账本（review 行）不动，新计划从下一次评分起按新策略算。
        """
        form = await _read_form(request)
        strategy_id = (form.get("strategy_id") or "").strip()
        try:
            get_strategy(strategy_id)
        except UnknownStrategyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with request.app.state.session_factory() as session:
            learner = session.get(Learner, request.app.state.learner_id)
            learner.strategy_id = strategy_id
            session.commit()
        return RedirectResponse("/practice?switched=1", status_code=303)

    @app.get("/tutor")
    def tutor_page(request: Request, question_id: int | None = None):
        """讲解页（四段式引导）。只做壳：段落文本全由 POST /tutor/* 返回。"""
        return render(request, "tutor", question_id=question_id)

    @app.post("/tutor/start")
    async def tutor_start(request: Request):
        """建讲解会话，送达第一段 ask_thought（只提问，绝不给答案）。"""
        learner_id = request.app.state.learner_id
        payload = await _read_json(request)
        try:
            question_id = int(payload.get("question_id") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=404, detail="题目不存在") from None
        def start():
            with request.app.state.session_factory() as session:
                try:
                    message = tutor.start_tutor(session, learner_id, question_id)
                except tutor.QuestionNotFound as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                except tutor.TutorConflict as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                return _tutor_payload(message)
        return await run_in_threadpool(start)

    @app.post("/tutor/next")
    async def tutor_next(request: Request):
        """推进一段；孩子说了话就带上（原话存档，不参与生成判定）。"""
        learner_id = request.app.state.learner_id
        payload = await _read_json(request)
        try:
            session_id = int(payload.get("session_id") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=404, detail="讲解会话不存在") from None
        child_text = payload.get("child_text") or None
        def advance():
            with request.app.state.session_factory() as session:
                try:
                    message = tutor.next_tutor(session, learner_id, session_id, child_text)
                except (tutor.SessionNotFound, tutor.QuestionNotFound) as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                except (tutor.StageExhausted, tutor.TutorConflict) as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                return _tutor_payload(message)
        return await run_in_threadpool(advance)

    @app.post("/tutor/reveal")
    async def tutor_reveal(request: Request):
        """孩子不想再猜了：直接跳到 explain（完整解析，含答案）。"""
        learner_id = request.app.state.learner_id
        payload = await _read_json(request)
        try:
            session_id = int(payload.get("session_id") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=404, detail="讲解会话不存在") from None
        def reveal():
            with request.app.state.session_factory() as session:
                try:
                    message = tutor.reveal_tutor(session, learner_id, session_id)
                except (tutor.SessionNotFound, tutor.QuestionNotFound) as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                except tutor.TutorConflict as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                return _tutor_payload(message)
        return await run_in_threadpool(reveal)

    @app.get("/review-queue")
    def review_queue(request: Request, subject: str | None = None):
        """待确认队列（v3 叫「看看读得对不对」）；`?subject=英语` 只列该学科。

        另列 needs_variant（连着答对同类的题才能学会那批）：原题 streak 已够但没有
        同类的题答对记录 —— 让家长看得出"为什么这道还不走"。接口语义不动，只换渲染。
        """
        with request.app.state.session_factory() as session:
            queue = graduation_service.pending_review_queue(
                session, request.app.state.learner_id
            )
            rows = [
                _queue_row(session, item, db_url=request.app.state.db_url)
                for item in queue
            ]
            blocked = [
                _queue_row(session, item, db_url=request.app.state.db_url)
                for item in graduation_service.needs_variant_items(
                    session, request.app.state.learner_id
                )
            ]
        if subject:
            rows = [row for row in rows if row["subject"] == subject]
            blocked = [row for row in blocked if row["subject"] == subject]
        return render(
            request,
            "confirm",
            rows=rows,
            blocked=blocked,
            date_text=_date_text(),
            nav_active="capture",
        )

    @app.post("/review-queue/confirm")
    async def review_confirm(request: Request):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        item_id = _int_field(form, "item_id")
        if item_id is None:
            return RedirectResponse("/review-queue", status_code=303)
        with request.app.state.session_factory() as session:
            try:
                graduation_service.confirm_review(session, item_id, learner_id=learner_id)
            except ValidationError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse("/review-queue", status_code=303)

    @app.post("/review-queue/reject")
    async def review_reject(request: Request):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        item_id = _int_field(form, "item_id")
        if item_id is None:
            return RedirectResponse("/review-queue", status_code=303)
        with request.app.state.session_factory() as session:
            try:
                graduation_service.reject_review(session, item_id, learner_id=learner_id)
            except ValidationError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse("/review-queue", status_code=303)

    @app.get("/books")
    def books(
        request: Request,
        book_id: int | None = None,
        q: str | None = None,
        added: int | None = None,
        skipped: int | None = None,
        error: str | None = None,
        unit_words: int | None = None,
        unit_created: int | None = None,
        unit_skipped: int | None = None,
    ):
        """教材基本库：左边选书，右边按单元列条目，可勾选加入错题库。"""
        with request.app.state.session_factory() as session:
            view = book_service.build_books_view(
                session, request.app.state.learner_id, book_id, q
            )
            # 每个单元「已生成几条」+ unit_id（按钮的 POST 地址要用）：
            # 生成判据与 generate_dictation 同一套，页面上看到的数和点下去的结果一致。
            gen_by_unit = {}
            if view.selected is not None:
                gen_by_unit = {
                    state.unit_no: state
                    for state in book_service.unit_dictation_state(
                        session, request.app.state.learner_id, view.selected.id
                    )
                }
        flash = None
        if added is not None or skipped is not None or error:
            flash = {"added": added or 0, "skipped": skipped or 0, "error": error}
        unit_flash = None
        if unit_created is not None or unit_skipped is not None:
            unit_flash = {
                "words": unit_words or 0,
                "created": unit_created or 0,
                "skipped": unit_skipped or 0,
            }
        return render(
            request,
            "books",
            view=view,
            flash=flash,
            unit_flash=unit_flash,
            gen_by_unit=gen_by_unit,
        )

    @app.post("/books/add")
    async def books_add(request: Request):
        """勾选的教材条目批量入库，然后回到原来那本书（保留搜索词）。"""
        form = await _read_form(request)
        book_id = _int_field(form, "book_id")
        query = form.get("q", "")
        word_ids = _picked_word_ids(form)
        back = {"book_id": book_id} if book_id is not None else {}
        if query:
            back["q"] = query
        try:
            with request.app.state.session_factory() as session:
                result = book_service.add_words_to_library(
                    session, request.app.state.learner_id, word_ids
                )
        except ValidationError as exc:
            back["error"] = str(exc)
        else:
            back["added"] = result.created_count
            back["skipped"] = result.skipped_count
        return RedirectResponse(f"/books?{urlencode(back)}", status_code=303)

    @app.post("/books/{book_id}/units/{unit_id}/generate")
    async def books_unit_generate(request: Request, book_id: int, unit_id: int):
        """点一下，把这一单元的单词全变成听写题；回页面显示「新增/跳过」摘要。"""
        with request.app.state.session_factory() as session:
            unit = session.get(Unit, unit_id)
            if unit is None or unit.book_id != book_id:
                raise HTTPException(status_code=404, detail="单元不存在")
            created, skipped = book_service.generate_dictation(
                session, request.app.state.learner_id, unit_id
            )
        back = {
            "book_id": book_id,
            # 本单元词数 = 每条词要么新建、要么跳过，两个数相加就是词表条数
            "unit_words": created + skipped,
            "unit_created": created,
            "unit_skipped": skipped,
        }
        return RedirectResponse(f"/books?{urlencode(back)}", status_code=303)

    @app.get("/stats")
    def stats(request: Request):
        """最近怎么样（v3 · E.11）：一句话总结最大 + 简表（这周练了几题 / 几题过关 /
        哪几题还在反复错）。近况与今天页同源（glance_service）；聚合仍在 stats_service。"""
        learner_id = request.app.state.learner_id
        now = utcnow()
        with request.app.state.session_factory() as session:
            report = stats_service.build_stats(session, learner_id, now=now)
            glance = glance_service.build_glance(session, learner_id, now=now, tz=_EDIT_TZ)
            # 本周过关：这周练过、且自评为「我懂了/早就会」的题（去重）
            day_start = (now.astimezone(_EDIT_TZ) - timedelta(days=6)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            passed_ids: set[int] = set()
            repeated_rows: list[dict] = []
            items = session.scalars(
                select(ErrorItem).where(ErrorItem.learner_id == learner_id)
            ).all()
            item_by_id = {item.id: item for item in items}
            for review in session.scalars(
                select(Review).where(Review.error_item_id.in_(item_by_id))
            ):
                moment = review.reviewed_at
                if moment is not None and moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                if moment is None or moment.astimezone(_EDIT_TZ) < day_start:
                    continue
                if review.rating in ("good", "easy"):
                    passed_ids.add(review.error_item_id)
            for item in items:
                if item.wrong_count >= 2 or item.restored_at is not None:
                    question = session.get(Question, item.question_id)
                    if question is not None:
                        repeated_rows.append(
                            {
                                "question_id": question.id,
                                "content": question.content,
                                "wrong_count": item.wrong_count,
                            }
                        )
            repeated_rows.sort(key=lambda row: row["wrong_count"], reverse=True)
        return render(
            request,
            "stats",
            report=report,
            glance=glance,
            passed_this_week=len(passed_ids),
            repeated_rows=repeated_rows,
            nav_active="today",
        )

    @app.get("/settings")
    def settings_page(request: Request):
        """设置（v3 · E.12）：孩子年级 / 每天练几题 / 数据导出备份 / 关于。"""
        with request.app.state.session_factory() as session:
            learner = session.get(Learner, request.app.state.learner_id)
            grade_level = learner.grade_level if learner else "primary"
            daily_target = learner.daily_target if learner else 5
            collection_flow = session.get(Workflow, "collection")
            has_collection_flow = bool(collection_flow and collection_flow.active_version_id)
        app_version = _app_version()
        return render(
            request,
            "settings",
            grade_level=grade_level,
            daily_target=daily_target,
            version=app_version,
            saved=request.query_params.get("saved") == "1",
            has_collection_flow=has_collection_flow,
            nav_active="settings",
        )

    def _collection_practice_page(request: Request, *, result=None, error=None):
        from cairn.collection_package import validated_version
        from cairn.collection_service import CollectionError
        from cairn.workflow.contracts import validate_collection_runtime
        from cairn.workflow.execution import RunError

        locale = "en" if request.query_params.get("lang") == "en" else "zh-Hans"
        manifest = None
        with request.app.state.session_factory() as session:
            workflow = session.get(Workflow, "collection")
            version = session.get(WorkflowVersion, workflow.active_version_id) if workflow and workflow.active_version_id else None
            try:
                runtime = json.loads(version.runtime_json) if version else None
                if not runtime or runtime.get("version_id") != version.version_id:
                    raise ValueError
                validate_collection_runtime(runtime.get("steps"))
                document, _ = validated_version(session, request.app.state.learner_id,
                                                runtime["steps"][0]["params"]["version_id"])
                manifest = document["manifest"]
            except (CollectionError, RunError, ValueError, TypeError, KeyError):
                pass
        return render(request, "collection_practice", manifest=manifest, result=result, error=error,
                      locale=locale, nav_active="settings")

    @app.get("/collection-practice")
    def collection_practice_page(request: Request):
        return _collection_practice_page(request)

    @app.post("/collection-practice")
    def collection_practice_start(request: Request):
        from cairn.collection_service import CollectionError
        from cairn.workflow.collection_runner import start_active_collection_workflow

        learner_id = request.app.state.learner_id
        with request.app.state.session_factory() as session:
            try:
                result = start_active_collection_workflow(session, learner_id)
                session.commit()
            except CollectionError as exc:
                session.rollback()
                return _collection_practice_page(request, error=str(exc))
        return _collection_practice_page(request, result=result)

    @app.post("/settings")
    async def settings_save(request: Request):
        """保存设置：年级（选填）与每天练几题（1-50）。303 回本页带成功提示。"""
        form = await _read_form(request)
        grade_level = (form.get("grade_level") or "").strip() or "primary"
        try:
            daily_target = int((form.get("daily_target") or "5").strip())
        except ValueError:
            daily_target = 5
        daily_target = max(1, min(50, daily_target))
        with request.app.state.session_factory() as session:
            learner = session.get(Learner, request.app.state.learner_id)
            if learner is not None:
                learner.grade_level = grade_level
                learner.daily_target = daily_target
                session.commit()
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/exam")
    def exam_page(request: Request):
        """考试列表 + 新建表单；未过期在前（带倒计时），过期只标「已结束」。"""
        today = _today()
        with request.app.state.session_factory() as session:
            view = exam_service.build_exam_view(
                session, request.app.state.learner_id, today
            )
        return render(
            request,
            "exam",
            view=view,
            subjects=exam_service.SUBJECTS,
            sprint_days=exam_service.SPRINT_DAYS,
            form=None,
        )

    @app.post("/exam")
    async def exam_create(request: Request):
        form = await _read_form(request)
        today = _today()
        with request.app.state.session_factory() as session:
            try:
                exam_service.set_exam(
                    session,
                    request.app.state.learner_id,
                    form.get("name", ""),
                    _parse_exam_date(form.get("exam_date", "")),
                    form.get("subject") or None,
                    form.get("note") or None,
                )
            except ValidationError as exc:
                view = exam_service.build_exam_view(
                    session, request.app.state.learner_id, today
                )
                return render(
                    request,
                    "exam",
                    view=view,
                    subjects=exam_service.SUBJECTS,
                    sprint_days=exam_service.SPRINT_DAYS,
                    form=form,
                    error=str(exc),
                )
        return RedirectResponse("/exam", status_code=303)

    @app.post("/exam/delete")
    async def exam_delete(request: Request):
        """家长手动删一场考试；过期记录不会自动消失，历史留在这里。"""
        form = await _read_form(request)
        exam_id = _int_field(form, "exam_id")
        if exam_id is not None:
            with request.app.state.session_factory() as session:
                exam_service.delete_exam(
                    session, request.app.state.learner_id, exam_id
                )
        return RedirectResponse("/exam", status_code=303)

    def _render_devices(request: Request, error: str | None = None):
        """设备页统一出口：单台读能力失败只记在那一行，枚举整体失败才顶到页头。"""
        backend = request.app.state.cameras
        views: list[scan_service.DeviceView] = []
        try:
            views = scan_service.build_device_views(backend)
        except CaptureError as exc:
            error = error or f"设备枚举失败：{exc}"
        return render(
            request,
            "devices",
            views=views,
            backend=backend,
            error=error,
            mode_labels=capture.MODE_LABELS,
        )

    @app.get("/devices")
    def devices_page(request: Request):
        """本机摄像头/高拍仪与能力；只读能力，不改任何采集参数。"""
        return _render_devices(request)

    @app.post("/devices/{device_id}/snapshot")
    def device_snapshot(request: Request, device_id: str):
        """抓一帧 → 落盘 → 入库；成功跳详情页，失败留在设备页报错（不留脏数据）。"""
        backend = request.app.state.cameras
        learner_id = request.app.state.learner_id
        try:
            frame = scan_service.grab_frame(backend, device_id)
        except (CaptureError, OSError) as exc:
            return _render_devices(request, error=f"抓拍失败：{exc}")
        with request.app.state.session_factory() as session:
            try:
                version_id, workflow_mode = active_scan_runtime(session, learner_id=learner_id)
                scan = scan_service.store_frame(
                    session,
                    frame,
                    learner_id,
                    db_url=request.app.state.db_url,
                    start_workflow=workflow_mode,
                    workflow_version_id=version_id,
                )
            except (CaptureError, OSError, WorkflowError) as exc:
                return _render_devices(request, error=f"抓拍失败：{exc}")
            scan_id = scan.id
        return RedirectResponse(f"/scans/{scan_id}/workflow" if workflow_mode else f"/scans/{scan_id}", status_code=303)

    @app.get("/upload")
    def upload_page(request: Request):
        """浏览器上传页：质量门在前端（canvas 算指标），不合格可看原因后仍硬传。

        短边下限（`CAIRN_MIN_IMAGE_SIDE`，默认 1000）随页面下发：前端提示、服务端硬拒
        用的是同一个数，不会出现"前端说能传、后端拒了"。
        """
        return render(
            request,
            "capture",
            min_side=scan_service.min_image_side(),
            error=None,
            date_text=_date_text(),
            nav_active="capture",
        )

    @app.post("/scans/upload")
    async def scan_upload(
        request: Request,
        file: UploadFile = File(...),
        quality: str = Form(""),
    ):
        """上传通道：原图 + 前端指标 JSON。硬校验失败 400，指标坏 JSON 静默忽略。"""
        learner_id = request.app.state.learner_id
        data = await file.read(scan_service.MAX_UPLOAD_BYTES + 1)
        return await run_in_threadpool(_save_scan_upload, request, learner_id, data, quality)

    def _save_scan_upload(request: Request, learner_id: int, data: bytes, quality: str):
        """Keep image processing and SQLite lock waits off the event loop."""
        with request.app.state.session_factory() as session:
            try:
                version_id, workflow_mode = active_scan_runtime(session, learner_id=learner_id)
                scan = scan_service.save_upload(
                    session,
                    learner_id,
                    data,
                    db_url=request.app.state.db_url,
                    quality=quality or None,
                    defer_processing=workflow_mode,
                    workflow_version_id=version_id,
                )
            except (scan_service.UploadError, WorkflowError) as exc:
                # 上传页是表单直传：浏览器会把 JSON 的 detail 拍成一行裸文本，
                # 所以把拒绝原因回渲染到页面提示区，状态码仍是 400。
                return render(
                    request,
                    "capture",
                    min_side=scan_service.min_image_side(),
                    error=str(exc),
                    status_code=409 if isinstance(exc, WorkflowError) else 400,
                    date_text=_date_text(),
                    nav_active="capture",
                )
            if not workflow_mode and not tools.is_enabled("preprocess"):
                # 预处理关掉：存原图、不摆正。service 里已经试过一遍，这里把产物
                # 丢掉并把列置空（原图不动），读图链路自然回落原图。
                _drop_processed(session, scan, request.app.state.db_url)
            scan_id = scan.id
        return RedirectResponse(f"/scans/{scan_id}/workflow" if workflow_mode else f"/scans/{scan_id}", status_code=303)

    @app.get("/scans")
    def scans_page(request: Request):
        """已拍列表：每行指路（整行链到详情），不在这里做事。

        每行多一列「下一步」：`待裁边` / `待选题` / `已入库 N`。状态是
        `scan_service` 算的，页面只查表——列表页要是自己判一遍，两边迟早对不上。
        """
        with request.app.state.session_factory() as session:
            scans = scan_service.list_scans(session, request.app.state.learner_id)
            stages = {
                scan.id: scan_service.stage_for(session, scan, request.app.state.db_url)
                for scan in scans
            }
            counts = {
                scan.id: scan_service.stored_question_count(session, scan)
                for scan in scans
            }
        return render(
            request,
            "scans",
            scans=scans,
            stages=stages,
            counts=counts,
            local_time=scan_service.local_time,
        )

    @app.get("/scans/{scan_id}")
    def scan_detail(request: Request, scan_id: int, source: str | None = None):
        """单张详情：默认看**原图**，有增强图时页面上可以切到「增强后」。

        默认原图是有意的：这张页面上家长要对图抄题、认自己的字，先给最原始的那张；
        想看压白之后的对比，点「增强后」（`?source=processed`）。
        要增强图但这页没有就回落到原图，页面上那行切换也不显示。
        """
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
            if run is not None and run.status != "completed":
                return RedirectResponse(f"/scans/{scan.id}/workflow", status_code=303)
            captured_at = scan_service.local_time(scan.captured_at)
            question = (
                session.get(Question, scan.question_id)
                if scan.question_id is not None
                else None
            )
            processed_ready = (
                scan_service.processed_image_path(scan, request.app.state.db_url)
                is not None
            )
            stage = scan_service.stage_for(session, scan, request.app.state.db_url)
            stage_hint = _stage_hint(
                stage,
                scan_service.crop_record_for(scan, request.app.state.db_url),
                scan_service.stored_question_count(session, scan),
            )
            # 已入库的卷子给一个「复核题目」入口：取这一页**第一条**入库的题
            # （切题行按 id 升序 = 卷面顺序；从详情页确认的卷子只有 scan.question_id）。
            first_question_id = session.scalars(
                select(ScanQuestion.question_id)
                .where(
                    ScanQuestion.scan_id == scan.id,
                    ScanQuestion.question_id.is_not(None),
                )
                .order_by(ScanQuestion.id.asc())
            ).first()
            if first_question_id is None:
                first_question_id = scan.question_id
        view = "processed" if source == "processed" and processed_ready else "original"
        return render(
            request,
            "scan",
            scan=scan,
            captured_at=captured_at,
            question=question,
            processed_ready=processed_ready,
            view=view,
            stage=stage,
            stage_hint=stage_hint,
            first_question_id=first_question_id,
            workflow_mode=run is not None,
        )

    @app.post("/scans/{scan_id}/confirm")
    async def scan_confirm(request: Request, scan_id: int):
        """人工确认入库：建题（复用 entry_service）并回链 scan.question_id。

        重复确认不重复建题（scan_service.confirm_scan 兜住）；字段缺失渲染详情页
        报错，不写半条数据。成功跳练习页。
        """
        form = await _read_form(request)
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            if session.scalar(select(WorkflowRun.id).where(WorkflowRun.scan_id == scan.id)) is not None:
                raise HTTPException(status_code=409, detail="请通过工作流处理这张照片")
            captured_at = scan_service.local_time(scan.captured_at)
            try:
                question, _created = scan_service.confirm_scan(
                    session,
                    request.app.state.learner_id,
                    scan,
                    subject=form.get("subject", ""),
                    question_type=form.get("question_type", ""),
                    content=form.get("content", ""),
                    answer=form.get("answer", ""),
                )
            except entry_service.ValidationError as exc:
                stage = scan_service.stage_for(
                    session, scan, request.app.state.db_url
                )
                return render(
                    request,
                    "scan",
                    scan=scan,
                    captured_at=captured_at,
                    question=None,
                    confirm_error=str(exc),
                    processed_ready=False,
                    view="original",
                    stage=stage,
                    stage_hint=_stage_hint(
                        stage,
                        scan_service.crop_record_for(scan, request.app.state.db_url),
                        scan_service.stored_question_count(session, scan),
                    ),
                )
        return RedirectResponse(f"/practice?added={question.id}", status_code=303)

    @app.get("/scans/{scan_id}/image")
    def scan_image(request: Request, scan_id: int, source: str | None = None):
        """这一页的图字节：**默认走 `read_image_path`（裁好的 → 增强图 → 原图）**。

        路径一律从库里读并夹在库目录内，越界按 404 处理。切题、OCR、叠框、
        手动框都基于同一张图，所以展示也必须发同一张——否则 `bbox_json.page`
        和看到的图不是一套尺寸，红框会整体错位。

        `?source=original` 发的是**上传时那张原图**（一个字节都没改，家长要
        核对自己的字就看这张）、`?source=processed` 发增强图；要增强图但这页
        没有（工具关了、找不到纸边）就安静退回原图，不 404——家长点一下不该
        看到报错。

        响应头 `X-Cairn-Source` 标明这次发的是 crop / processed / original，
        排查"框为什么偏了"时一眼能看出用的是哪张。
        """
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                original = scan_service.image_path(scan, request.app.state.db_url)
                if source == "original":
                    path, served = original, "original"
                elif source == "processed":
                    processed = scan_service.processed_image_path(
                        scan, request.app.state.db_url
                    )
                    path = processed or original
                    served = "processed" if processed is not None else "original"
                else:
                    path = scan_service.read_image_path(scan, request.app.state.db_url)
                    if path == original:
                        served = "original"
                    elif path == scan_service.crop_path_for_scan(
                        scan, request.app.state.db_url
                    ):
                        served = "crop"
                    else:
                        served = "processed"
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            media_type = scan_service.media_type_for(path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="照片文件不在了")
        return FileResponse(
            path, media_type=media_type, headers={"X-Cairn-Source": served}
        )

    def _workflow_waiting_node(session: Session, scan: Scan, expected: str) -> WorkflowRun | None:
        run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
        if run is None:
            return None
        try:
            progress = workflow_execution.snapshot(session, scan)
            if (progress["status"] != "waiting" or progress["cursor"] >= len(progress["steps"])
                    or progress["steps"][progress["cursor"]]["node_id"] != expected):
                raise workflow_execution.RunError("当前流程尚未进入该步骤")
            return run
        except workflow_execution.RunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/scans/{scan_id}/workflow")
    def scan_workflow_page(request: Request, scan_id: int, lang: str = "zh"):
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
            if run is None:
                raise HTTPException(status_code=404, detail="没有工作流进度")
            try:
                progress = workflow_execution.snapshot(session, scan)
            except workflow_execution.RunError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if progress["status"] == "waiting" and progress["cursor"] < len(progress["steps"]):
                node = progress["steps"][progress["cursor"]]["node_id"]
                destinations = {"crop": "crop", "select": "select", "review": "workflow-review"}
                if node in destinations:
                    suffix = "?lang=en" if lang == "en" else ""
                    return RedirectResponse(f"/scans/{scan.id}/{destinations[node]}{suffix}", status_code=303)
            labels = {
                "upright": ("摆正", "Straighten"), "crop": ("裁边", "Crop"),
                "enhance": ("增强", "Enhance"), "cut": ("切题", "Split questions"),
                "select": ("圈选", "Select"), "recognize": ("认字", "Read text"),
                "review": ("核对", "Review"), "strategy": ("安排复习", "Schedule review"),
                "guided_tutor": ("准备引导讲解", "Enable guided help"),
                "text_recap": ("准备文字复述", "Enable text recall"),
                "collection_append": ("收录到题集", "Add to collection"),
            }
            english = lang == "en"
            steps = [{"label": labels[item["node_id"]][1 if english else 0], "status": item["status"]}
                     for item in progress["steps"] if item["node_id"] in labels]
            return render(request, "workflow_status", scan=scan, progress=progress, steps=steps,
                          can_skip_cut=(progress["status"] == "failed" and progress["cursor"] < len(progress["steps"])
                                        and progress["steps"][progress["cursor"]]["node_id"] == "cut"),
                          can_skip_collection=(progress["status"] == "failed" and progress["cursor"] < len(progress["steps"])
                                               and progress["steps"][progress["cursor"]]["node_id"] == "collection_append"),
                          lang="en" if english else "zh", ui_lang="en" if english else "zh",
                          date_text=_date_text("en" if english else "zh"), nav_active="capture")

    @app.post("/scans/{scan_id}/workflow-continue")
    async def scan_workflow_continue(request: Request, scan_id: int):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        lang = "en" if form.get("lang") == "en" else "zh"

        def advance_run():
            with request.app.state.session_factory() as session:
                scan = scan_service.get_scan(session, learner_id, scan_id)
                if scan is None:
                    raise HTTPException(status_code=404, detail="没有这张照片")
                run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
                if run is None:
                    raise HTTPException(status_code=404, detail="没有工作流进度")
                try:
                    workflow_execution.advance(session, run, scan_workflow_handlers(request.app.state.db_url),
                                               retry=run.status == "failed")
                    session.commit()
                except workflow_execution.RunError as exc:
                    session.rollback()
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

        await run_in_threadpool(advance_run)
        return RedirectResponse(f"/scans/{scan_id}/workflow?lang={lang}", status_code=303)

    @app.post("/scans/{scan_id}/workflow-manual-cut")
    async def scan_workflow_manual_cut(request: Request, scan_id: int):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        lang = "en" if form.get("lang") == "en" else "zh"

        def skip():
            with request.app.state.session_factory() as session:
                scan = scan_service.get_scan(session, learner_id, scan_id)
                if scan is None:
                    raise HTTPException(status_code=404, detail="没有这张照片")
                run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
                if run is None:
                    raise HTTPException(status_code=404, detail="没有工作流进度")
                try:
                    workflow_execution.skip_failed_cut(session, run, scan_workflow_handlers(request.app.state.db_url))
                    session.commit()
                except workflow_execution.RunError as exc:
                    session.rollback()
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

        await run_in_threadpool(skip)
        return RedirectResponse(f"/scans/{scan_id}/workflow?lang={lang}", status_code=303)

    @app.post("/scans/{scan_id}/workflow-skip-collection")
    async def scan_workflow_skip_collection(request: Request, scan_id: int):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        lang = "en" if form.get("lang") == "en" else "zh"
        def skip():
            with request.app.state.session_factory() as session:
                scan = scan_service.get_scan(session, learner_id, scan_id)
                if scan is None:
                    raise HTTPException(status_code=404, detail="没有这张照片")
                run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
                if run is None:
                    raise HTTPException(status_code=404, detail="没有工作流进度")
                try:
                    workflow_execution.skip_failed_collection(session, run, scan_workflow_handlers(request.app.state.db_url))
                    session.commit()
                except workflow_execution.RunError as exc:
                    session.rollback()
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
        await run_in_threadpool(skip)
        return RedirectResponse(f"/scans/{scan_id}/workflow?lang={lang}", status_code=303)

    @app.get("/scans/{scan_id}/crop")
    def scan_crop_page(request: Request, scan_id: int, lang: str = "zh"):
        """裁边确认页（Phase 2-c）：家长看一眼四个角，拖一拖，再决定用不用。

        和同路径的 `POST` 不是一条路由：那一条收四个角重裁（P2-b），方法不同，
        FastAPI 按 (path, method) 分发，这里只负责把页面发出去。

        页面上那句话按 sidecar 的 `reason` 取：三档（`_crop_verdict`）里正常 / 可疑
        两档是固定短句，其余闸门沿用 `_CROP_REASON_TEXT` 的原句。只念已经落盘的事实、
        **不重跑检测**；四角与尺寸不在这里渲染，由
        `/api/scans/{id}/crop-info` 交给前端画。照片行不存在 → 404（与
        `/scans/{id}` 一致），不是 500。
        """
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(
                session, request.app.state.learner_id, scan_id
            )
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            run = _workflow_waiting_node(session, scan, "crop")
            record = scan_service.crop_record_for(scan, request.app.state.db_url)
        reason = (record or {}).get("reason")
        verdict, verdict_modifier, _edges = _crop_verdict(reason)
        ui_lang = "en" if run is not None and lang == "en" else "zh"
        if ui_lang == "en":
            verdict = {
                "ok": "Edges aligned 4/4", "weak_edge": "One edge needs checking",
                "degenerate_mask": "One edge needs checking", "not_found": "Paper not found; capture the whole page",
                "already_full_page": "The page already fills the image",
                "cannot_separate_background": "Paper is hard to distinguish from the background",
                "paper_too_small": "The page is too small; move closer and retake",
                "paper_cut_by_frame": "The page is cut off; retake the photo",
            }.get(reason, "Check the corners of this page")
        return render(
            request,
            "crop",
            scan=scan,
            reason=reason,
            reason_text=_crop_reason_text(reason),
            verdict=verdict,
            verdict_modifier=verdict_modifier,
            facts=_crop_facts(record, scan),
            ui_lang=ui_lang, workflow_mode=run is not None,
            date_text=_date_text(ui_lang),
            nav_active="capture",
        )

    @app.get("/scans/{scan_id}/workflow-review")
    def scan_workflow_review_page(request: Request, scan_id: int, lang: str = "zh"):
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
            if run is None:
                raise HTTPException(status_code=404, detail="没有复核流程")
            try:
                progress = workflow_execution.snapshot(session, scan)
            except workflow_execution.RunError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if progress["status"] == "waiting":
                cursor = progress["cursor"]
                if cursor >= len(progress["steps"]) or progress["steps"][cursor]["node_id"] != "review":
                    raise HTTPException(status_code=409, detail="当前步骤不是复核")
            elif progress["status"] == "failed":
                if not any(step["node_id"] == "review" and step["status"] == "completed"
                           for step in progress["steps"]):
                    raise HTTPException(status_code=409, detail="尚未完成复核")
            elif progress["status"] != "completed":
                raise HTTPException(status_code=409, detail="当前步骤不是复核")
            items = session.execute(
                select(WorkflowReviewItem, Question)
                .join(Question, Question.id == WorkflowReviewItem.question_id)
                .where(WorkflowReviewItem.run_id == run.id, Question.learner_id == scan.learner_id)
                .order_by(WorkflowReviewItem.id)
            ).all()
            if not items:
                raise HTTPException(status_code=404, detail="没有待复核题目")
            pending = [(item, question) for item, question in items if item.status == "pending"]
            if (progress["status"] == "waiting") != bool(pending):
                raise HTTPException(status_code=409, detail="复核进度不一致")
            crop_ids = set()
            for _, question in items:
                _, row = _question_and_crop_row(session, request.app.state.learner_id, question.id)
                if row is not None:
                    try:
                        if scan_service.crop_image_path(row, request.app.state.db_url).is_file():
                            crop_ids.add(question.id)
                    except ValueError:
                        pass
            try:
                workflow_image = workflow_images.selected_image(session, scan, request.app.state.db_url)
            except (workflow_execution.RunError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return render(request, "workflow_review", scan=scan, items=items, pending=pending, crop_ids=crop_ids,
                          workflow_image=bool(workflow_image),
                          can_skip_collection=(progress["status"] == "failed" and progress["cursor"] < len(progress["steps"])
                                               and progress["steps"][progress["cursor"]]["node_id"] == "collection_append"),
                          progress=progress, lang="en" if lang == "en" else "zh",
                          ui_lang="en" if lang == "en" else "zh",
                          date_text=_date_text("en" if lang == "en" else "zh"), nav_active="capture")

    @app.post("/scans/{scan_id}/workflow-review/{question_id}")
    async def scan_workflow_review_decide(request: Request, scan_id: int, question_id: int):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        decision = form.get("decision", "")
        lang = "en" if form.get("lang") == "en" else "zh"
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
            if run is None:
                raise HTTPException(status_code=404, detail="没有复核流程")
            try:
                workflow_review.decide(session, run, question_id, decision,
                                       scan_workflow_handlers(request.app.state.db_url))
                session.commit()
            except workflow_execution.RunError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/scans/{scan_id}/workflow-review?lang={lang}", status_code=303)

    @app.post("/scans/{scan_id}/workflow-retry")
    async def scan_workflow_retry(request: Request, scan_id: int):
        learner_id = request.app.state.learner_id
        form = await _read_form(request)
        lang = "en" if form.get("lang") == "en" else "zh"
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
            if run is None:
                raise HTTPException(status_code=404, detail="没有工作流进度")
            review_done = session.scalar(select(WorkflowRunStep.id).where(
                WorkflowRunStep.run_id == run.id, WorkflowRunStep.node_id == "review",
                WorkflowRunStep.status == "completed",
            ))
            if review_done is None:
                raise HTTPException(status_code=409, detail="尚未完成复核")
            try:
                if run.status == "failed":
                    workflow_execution.advance(session, run,
                                               scan_workflow_handlers(request.app.state.db_url), retry=True)
                    session.commit()
            except workflow_execution.RunError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/scans/{scan_id}/workflow-review?lang={lang}", status_code=303)

    @app.get("/api/scans/{scan_id}/workflow")
    def scan_workflow_status(request: Request, scan_id: int):
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                return workflow_execution.snapshot(session, scan)
            except workflow_execution.RunError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/scans/{scan_id}/workflow-image")
    def scan_workflow_image(request: Request, scan_id: int):
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                path = workflow_images.selected_image(session, scan, request.app.state.db_url)
            except (workflow_execution.RunError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if path is None:
                raise HTTPException(status_code=404, detail="流程尚未选择照片")
            return FileResponse(path, media_type=scan_service.media_type_for(path))

    @app.get("/api/scans/{scan_id}/crop-info")
    def scan_crop_info(request: Request, scan_id: int):
        """这一页上传时记下的裁切结论，给前端叠框用（只读，**不重跑检测**）。

        `corners` 是**原图坐标**（`[tl, tr, br, bl]` 顺时针），配 `width`/`height`
        一起发；拿 `crop.jpg` 当参考尺寸会整体错位——那张是裁完再摆正的。

        老卷子没有 `<uuid>.crop.json`（上传早于这一步）：照样 200，结论字段
        全 null（含 D2b 的三个亮度字段），`has_crop_file` 按实际有没有
        `<uuid>.crop.jpg` 说；尺寸退回库里记的那组（上传时前端报的），库里也没有才是 null。
        照片行不存在才是 404。
        """
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(
                session, request.app.state.learner_id, scan_id
            )
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            record = scan_service.crop_record_for(scan, request.app.state.db_url)
            try:
                original = scan_service.image_path(scan, request.app.state.db_url)
                has_crop_file = (
                    scan_service.crop_path_for_scan(scan, request.app.state.db_url)
                    != original
                )
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            stored_width, stored_height = scan.width, scan.height
            scan_id_for_url = scan.id

        facts = record or {}
        width = facts.get("width")
        height = facts.get("height")
        return JSONResponse(
            {
                "scan_id": scan_id_for_url,
                "reason": facts.get("reason"),
                "note": facts.get("note"),
                "area_ratio": facts.get("area_ratio"),
                "corners": facts.get("corners"),
                "paper_level": facts.get("paper_level"),
                "edge_level": facts.get("edge_level"),
                "background_delta": facts.get("background_delta"),
                "width": width if width is not None else stored_width,
                "height": height if height is not None else stored_height,
                "has_crop_file": has_crop_file,
                "image_url": f"/scans/{scan_id_for_url}/image?source=original",
                "crop_url": (
                    f"/scans/{scan_id_for_url}/image?source=crop"
                    if has_crop_file
                    else None
                ),
            }
        )

    @app.post("/scans/{scan_id}/crop")
    async def scan_crop_apply(request: Request, scan_id: int):
        """家长拖过的四角 → **从原图重裁**，另存 `<uuid>.crop.png`。

        body 两选一：

        - `{"corners": [[x,y]×4]}`：原图坐标、顺时针 tl→tr→br→bl。四角不合格
          （不是 4 点 / 越界 / 不凸 / 面积 <5% / 最短边 <长边 10%）一律 400，
          而且**一个文件都不动**：校验和编码都在写盘之前。
        - `{"skip": true}`：家长不动，不重裁、不出新图，只把 sidecar 标
          `human="skipped"`，返回当前状态。

        重裁永远读 `scan.path` 那张原图（不读上一次的裁后图，避免二次重采样）；
        原图只读，字节与上传时一致。返回的 width/height/area_ratio 说的是**新裁
        出来的这张**。
        """
        learner_id = request.app.state.learner_id
        body = await _read_json(request)
        return await run_in_threadpool(_apply_scan_crop, request, scan_id, body, learner_id)

    def _apply_scan_crop(request: Request, scan_id: int, body: dict, learner_id: int):
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(
                session, learner_id, scan_id
            )
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            run = _workflow_waiting_node(session, scan, "crop")
            result_box: list[dict] = []

            def apply_crop(target):
                if body.get("skip") is True:
                    return scan_service.mark_crop_skipped(target, request.app.state.db_url)
                return scan_service.apply_human_corners(target, request.app.state.db_url, body.get("corners"))

            def workflow_crop_action(action_session, target, params):
                result_box.append(apply_crop(target))
                workflow_human_actions.crop_confirmed(request.app.state.db_url)(action_session, target, params)

            try:
                if run is not None:
                    workflow_execution.resume(session, run, "crop", workflow_crop_action,
                                              scan_workflow_handlers(request.app.state.db_url))
                    session.commit()
                    result = result_box[0]
                else:
                    result = apply_crop(scan)
            except scan_service.CropInputError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except workflow_execution.RunError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            scan_id_for_url = scan.id
        return JSONResponse(
            {
                "ok": True,
                "human": result["human"],
                "crop_url": (
                    f"/scans/{scan_id_for_url}/image?source=crop"
                    if result["crop_file"] is not None
                    else None
                ),
                "width": result["width"],
                "height": result["height"],
                "area_ratio": result["area_ratio"],
                "next_url": f"/scans/{scan_id_for_url}/workflow" if run is not None else None,
            }
        )

    @app.get("/scans/{scan_id}/ocr")
    def scan_ocr(request: Request, scan_id: int):
        """整页 OCR 预览（通道卡 B 前半）：现算现展示，不落库。

        走法不在这里决定：`SCAN_RECIPE` 指的 YAML 说了算（默认 scan-entry：摆正 →
        裁切 → 切题 → 认字），`recipe.run` 按配方跑、每步留痕，页面顶部那行痕迹就是
        `result.trace()`。关掉的工具只会让那一步变成"已关闭"，后面的步骤照跑（AC4）。

        每次进页都重新识别（页面上的「重新识别」就是再跑一遍本路由）；
        引擎不可用/识别失败在页面上报错，不让家长看堆栈。

        识别用的图走 `read_image_path`（优先预处理图）：跟标错页叠框、手动框裁图
        是同一张，三处坐标才能对上。
        """
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
            if run is not None and run.status != "completed":
                return RedirectResponse(f"/scans/{scan.id}/workflow", status_code=303)
            try:
                path = scan_service.read_image_path(scan, request.app.state.db_url)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="照片文件不在了")
        result = recipe.run(
            recipe.load(SCAN_RECIPE),
            {"path": path, "scan": scan, "db_url": request.app.state.db_url},
        )
        # 认字关掉时页面说的话由 web 管（与改造前同一句）；其余失败照 runner 记的原因
        error = OCR_OFF_NOTICE if "ocr" in result.missing_tools else result.failure_note
        page = result.data.get("page")
        blocks = result.data.get("blocks")
        return render(
            request,
            "reading",
            scan=scan,
            rows=result.data.get("rows"),
            questions=(
                None
                if blocks is None
                else [
                    {"no": block.no, "bbox": block.bbox, "stem": block.stem_text}
                    for block in blocks
                ]
            ),
            error=error,
            angle=None if page is None else page.angle,
            score=None if page is None else page.score,
            # 图解不开时 read_page 给不出尺寸（width/height=0），用库里的兜底
            page_width=scan.width if page is None else page.width or scan.width,
            page_height=scan.height if page is None else page.height or scan.height,
            trace=result.trace(),
            date_text=_date_text(),
            nav_active="capture",
        )

    def _load_question_rows(request: Request, scan, path: Path):
        """读这张照片的切题行；一条都没有时现切一次并落库（v16）。

        已经有行就不再跑 OCR：家长重复进页、或者 OCR 引擎没装（CAIRN_OCR_DISABLE）
        时，页面照样能用——落库过的那批题不依赖引擎。
        没有行时按上传默认配方（`SCAN_RECIPE`）跑一遍（与整页识别页同一条链），切出来的块
        在这里落库。返回 (rows, error)；做不成时 rows 为空、error 是给人看的一句话。
        """
        with request.app.state.session_factory() as session:
            rows = scan_service.list_questions(session, scan.id)
        if rows:
            return rows, None
        result = recipe.run(
            recipe.load(SCAN_RECIPE),
            {"path": path, "scan": scan, "db_url": request.app.state.db_url},
        )
        missing = result.missing_tools
        # 切题关掉：不自动切，一行都不落库（已经切过的照常显示）。
        # 页面自己会提示"请自己框"（CUT_OFF_NOTICE），这里不重复说。
        if "cut" in missing:
            return [], None
        # 文字识别关掉：切题要靠认字，认不了就引导家长自己框。
        if "ocr" in missing:
            return [], OCR_OFF_NOTICE
        blocks = result.data.get("blocks")
        page = result.data.get("page")
        if blocks is None or page is None:
            return [], result.failure_note
        with request.app.state.session_factory() as session:
            rows = scan_service.store_questions(
                session,
                scan.id,
                blocks,
                page_width=page.width or scan.width,
                page_height=page.height or scan.height,
                angle=page.angle or 0,
                # 题块图从**摆正后**的页面上裁：bbox 就是这张图的坐标
                page_image=page.image,
                scan=scan,
                db_url=request.app.state.db_url,
            )
        return rows, None

    def _render_questions(
        request: Request,
        scan,
        rows,
        *,
        image_path: Path | None = None,
        error: str | None = None,
        notice: str | None = None,
        warnings: tuple[str, ...] = (),
        subject: str = "数学",
        question_type: str = "填空",
    ):
        """标错页统一出口：把库里每行算成模板要的展示字段。"""
        geometry = scan_service.page_geometry(rows)
        page_width, page_height, angle = geometry or (0, 0, 0)
        red_flags = _red_mark_flags(image_path, rows, angle)
        questions = []
        for index, row in enumerate(rows):
            stem = (row.stem_text or "").strip()
            questions.append(
                {
                    "no": row.no,
                    "stem": stem,
                    "excerpt": stem if len(stem) <= 40 else f"{stem[:40]}…",
                    "imported": row.question_id is not None,
                    "is_wrong": row.is_wrong,
                    # 题号是推测出来的（迁移 v18）：页面标一句"请核对"，不挡勾选
                    "inferred": bool(row.inferred_number),
                    # 预勾选候选：题框里有红笔就默认勾上，家长可以取消（AC5）
                    "red_mark": red_flags[index],
                    "short": len(stem) < scan_service.MIN_STEM_CHARS,
                    "style": _overlay_style(
                        scan_service.row_bbox(row), page_width, page_height
                    ),
                }
            )
        # 底图是**未摆正**的那张：宽高换过来再按百分比铺满"摆正后"的容器，
        # 再反向转回 -angle，页面上看到的就是摆正后的卷子 + 摆正后的题框。
        # 尺寸按**实际发出去的那张**算（可能是预处理图，和原图不一样大）；
        # 量不出来（老图/坏字节）才用库里记的原图尺寸兜底。
        img_style = None
        if page_width and page_height:
            served = scan_service.image_size(image_path) if image_path else None
            orig_width, orig_height = served or (
                scan.width or page_width,
                scan.height or page_height,
            )
            img_style = (
                f"width:{orig_width / page_width * 100:.2f}%;"
                f"height:{orig_height / page_height * 100:.2f}%;"
                f"transform:translate(-50%,-50%) rotate({-angle}deg);"
            )
        return render(
            request,
            "scan_questions",
            scan=scan,
            questions=questions,
            total=len(questions),
            page_width=page_width,
            page_height=page_height,
            angle=angle,
            img_style=img_style,
            red_count=sum(1 for question in questions if question["red_mark"]),
            inferred_count=sum(1 for question in questions if question["inferred"]),
            error=error,
            notice=notice,
            warnings=list(warnings),
            subject=subject,
            question_type=question_type,
            min_stem=scan_service.MIN_STEM_CHARS,
        )

    def _workflow_selection_image(session: Session, scan: Scan, db_url: str) -> Path | None:
        run = _workflow_waiting_node(session, scan, "select")
        if run is None:
            return None
        try:
            path = workflow_images.selected_image(session, scan, db_url)
            if path is None:
                raise workflow_execution.RunError("流程尚未选择照片")
            return path
        except workflow_execution.RunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/scans/{scan_id}/workflow-selection")
    async def scan_workflow_selection_done(request: Request, scan_id: int):
        learner_id = request.app.state.learner_id
        body = await _read_json(request)
        return await run_in_threadpool(_finish_workflow_selection, request, scan_id, body, learner_id)

    def _finish_workflow_selection(request: Request, scan_id: int, body: dict, learner_id: int):
        subject = body.get("subject", "数学")
        question_type = body.get("question_type", "填空")
        if (not isinstance(subject, str) or not subject.strip() or len(subject) > 32
                or not isinstance(question_type, str) or not question_type.strip() or len(question_type) > 32):
            raise HTTPException(status_code=400, detail="科目或题型无效")
        written_files: list[Path] = []
        commit_attempted = False

        def cleanup() -> None:
            for path in written_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).warning("Could not clean an interrupted workflow crop", exc_info=True)

        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
            if run is None:
                raise HTTPException(status_code=404, detail="没有工作流进度")
            try:
                verify = workflow_human_actions.selection_confirmed(
                    request.app.state.db_url, body.get("auto_numbers"), body.get("manual_frames")
                )
                select_done = session.scalar(select(WorkflowRunStep.id).where(
                    WorkflowRunStep.run_id == run.id, WorkflowRunStep.node_id == "select",
                    WorkflowRunStep.status == "completed",
                ))
                if select_done is None:
                    action = workflow_human_actions.submit_selection(
                        request.app.state.db_url, body.get("auto_numbers"), body.get("manual_frames"),
                        written_files, subject.strip(), question_type.strip(),
                    )
                    workflow_execution.resume(session, run, "select", action,
                                              scan_workflow_handlers(request.app.state.db_url))
                    progress = workflow_execution.snapshot(session, scan)
                    commit_attempted = True
                    session.commit()
                else:
                    verify(session, scan, {})
                    progress = workflow_execution.snapshot(session, scan)
            except workflow_execution.RunError as exc:
                session.rollback()
                if not commit_attempted:
                    cleanup()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception:
                session.rollback()
                if not commit_attempted:
                    cleanup()
                raise
            next_url = f"/scans/{scan.id}/workflow"
            if progress["status"] == "waiting" and progress["cursor"] < len(progress["steps"]):
                if progress["steps"][progress["cursor"]]["node_id"] == "review":
                    next_url = f"/scans/{scan.id}/workflow-review"
            return {"status": progress["status"], "next_url": next_url}

    @app.get("/scans/{scan_id}/questions")
    def scan_questions_page(request: Request, scan_id: int):
        """标错页：整页图叠加题框，逐题勾"这道错了"。第一次进页现切题并落库。

        切题结果落 `scan_question`（幂等）；入库发生在下面的 POST，本页只读。
        底图与切题图同一张（`read_image_path`：优先预处理图），框才对得上。
        """
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                workflow_path = _workflow_selection_image(session, scan, request.app.state.db_url)
                if workflow_path is not None:
                    return RedirectResponse(f"/scans/{scan.id}/select", status_code=303)
                path = scan_service.read_image_path(scan, request.app.state.db_url)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="照片文件不在了")
        rows, error = _load_question_rows(request, scan, path)
        return _render_questions(
            request,
            scan,
            rows,
            image_path=path,
            error=error,
            notice=None if tools.is_enabled("cut") else CUT_OFF_NOTICE,
        )

    @app.post("/scans/{scan_id}/questions")
    async def scan_questions_submit(request: Request, scan_id: int):
        """只把勾选的题入库；已入库的不重复建，题干残缺的拒绝并提示补录。"""
        pairs = await _read_form_pairs(request)
        form = dict(pairs)
        selected = [
            int(value) for key, value in pairs if key == "no" and value.strip().isdigit()
        ]
        subject = (form.get("subject") or "").strip() or "数学"
        question_type = (form.get("question_type") or "").strip() or "填空"
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                workflow_path = _workflow_selection_image(session, scan, request.app.state.db_url)
                if workflow_path is not None:
                    raise HTTPException(status_code=409, detail="请使用工作流圈选提交")
                path = workflow_path or scan_service.read_image_path(scan, request.app.state.db_url)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            rows = scan_service.list_questions(session, scan.id)
        if not rows and workflow_path is None:
            # 直接 POST（没先打开 GET 页）：先补一次切题落库，再按勾选入库
            rows, error = _load_question_rows(request, scan, path)
            if error:
                return _render_questions(
                    request,
                    scan,
                    [],
                    image_path=path,
                    error=error,
                    subject=subject,
                    question_type=question_type,
                )
            if not rows and not tools.is_enabled("cut"):
                # 切题关掉：没有可勾的题，回页面说清楚，不抛异常
                return _render_questions(
                    request,
                    scan,
                    [],
                    image_path=path,
                    notice=CUT_OFF_NOTICE,
                    subject=subject,
                    question_type=question_type,
                )
        with request.app.state.session_factory() as session:
            result = scan_service.import_wrong_questions(
                session,
                request.app.state.learner_id,
                scan,
                selected,
                subject=subject,
                question_type=question_type,
            )
            rows = scan_service.list_questions(session, scan.id)
        notice = f"已收好 {result.created_count} 题"
        if result.existing:
            notice += f"，{len(result.existing)} 题之前已收好（未重复建题）"
        if not selected:
            notice = "没有勾选任何题，一道都没收"
        warnings = tuple(f"第 {no} 题题干需人工补录" for no in result.short)
        return _render_questions(
            request,
            scan,
            rows,
            image_path=path,
            notice=notice,
            warnings=warnings,
            subject=subject,
            question_type=question_type,
        )

    def _render_select(
        request: Request,
        scan,
        rows,
        *,
        image_sha256: str = "",
        error: str | None = None,
        workflow_path: Path | None = None,
        workflow_params_hash: str | None = None,
        workflow_preselect: bool = False,
        collection_subject: str | None = None,
        ui_lang: str = "zh",
    ):
        """圈选入库页统一出口：把每行的题框摊成模板要的 `data-*`（原图坐标）。

        `page_geometry` 给的宽高就是 `bbox_json` 的坐标系（摆正后的页面），没有
        切题行时退回照片自己的尺寸——空态只要图能显示，框一个都没有。

        除框以外还带三样给 `cut_feedback` 用的东西（Phase 3-b）：底图指纹、
        配方名、参数指纹。页面自己算不出这三样，前端只是把它们原样回传。
        """
        geometry = scan_service.page_geometry(rows)
        fixed_size = scan_service.image_size(workflow_path) if workflow_path else None
        page_width, page_height, _angle = geometry or (
            *(fixed_size or (scan.width or 0, scan.height or 0)), 0
        )
        red_flags = [False] * len(rows)
        if workflow_path and workflow_preselect:
            try:
                red_flags = _red_mark_flags(workflow_path, rows, 0)
            except Exception:
                logging.getLogger(__name__).warning("Red-mark detection failed", exc_info=True)
        boxes = []
        for index, row in enumerate(rows):
            bbox = scan_service.row_bbox(row)
            if bbox is None:
                continue  # 没有坐标的行走不了这页（图上没框可点），坏数据不炸整页
            boxes.append(
                {
                    "no": row.no,
                    # 反馈用的稳定键：`scan_question(scan_id, no)` 本来就唯一，
                    # 同一页重复提交靠它覆盖而不是追加
                    "frame_key": f"a{row.no}",
                    # 表里没存"哪个策略切出来的"：卷面直接读到题号 → anchor_digit，
                    # 按跳号补出来的（页面上带 `?`）→ gap。不新增字段的诚实映射。
                    "source": "gap" if row.inferred_number else "anchor_digit",
                    "inferred": bool(row.inferred_number),
                    "preselected": bool(red_flags[index]),
                    "x0": bbox[0],
                    "y0": bbox[1],
                    "x1": bbox[2],
                    "y1": bbox[3],
                }
            )
        return render(
            request,
            "select",
            scan=scan,
            boxes=boxes,
            auto_count=len(boxes),
            page_width=page_width,
            page_height=page_height,
            image_sha256=image_sha256,
            recipe=scan.workflow_version_id if workflow_path else SCAN_RECIPE,
            params_hash=workflow_params_hash if workflow_path else _cut_params_hash(),
            error=error,
            workflow_mode=workflow_path is not None,
            collection_subject=collection_subject,
            ui_lang=ui_lang,
            image_url=(f"/api/scans/{scan.id}/workflow-image" if workflow_path else f"/scans/{scan.id}/upright"),
            date_text=_date_text(ui_lang),
            nav_active="capture",
        )

    @app.get("/scans/{scan_id}/select")
    def scan_select_page(request: Request, scan_id: int, lang: str = "zh"):
        """圈选入库页（Phase 3-a）：图上叠自动框，勾中的才入库，空白处自己圈新题。

        和标错页读同一批 `scan_question` 行（`_load_question_rows`：有行就不再跑
        OCR），所以两页看到的框永远一致。这页**不写库**：勾选与画框都由
        `select-questions.js` 收齐后分别发给 `/questions` 与 `/frame`。
        """
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                workflow_path = _workflow_selection_image(session, scan, request.app.state.db_url)
                path = workflow_path or scan_service.read_image_path(scan, request.app.state.db_url)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            rows = scan_service.list_questions(session, scan.id) if workflow_path else None
            workflow_params_hash = None
            workflow_preselect = False
            collection_subject = None
            if workflow_path:
                version = session.get(WorkflowVersion, scan.workflow_version_id)
                if version is None:
                    raise HTTPException(status_code=409, detail="工作流版本不存在")
                workflow_params_hash = hashlib.sha256(version.runtime_json.encode("utf-8")).hexdigest()
                run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
                runtime_steps = workflow_execution._runtime(session, run)
                workflow_preselect = runtime_steps[run.cursor]["params"].get("preselect") is True
                for step in runtime_steps[run.cursor + 1:]:
                    if step["node_id"] == "collection_append":
                        collection = session.get(Collection, step["params"].get("collection_id"))
                        if collection is None or collection.learner_id != scan.learner_id:
                            raise HTTPException(status_code=409, detail="目标题集不可用")
                        collection_subject = collection.subject
        if not path.is_file():
            raise HTTPException(status_code=404, detail="照片文件不在了")
        if rows is None:
            rows, error = _load_question_rows(request, scan, path)
        else:
            error = None
        return _render_select(
            request, scan, rows, image_sha256=_file_sha256(path), error=error,
            workflow_path=workflow_path, workflow_params_hash=workflow_params_hash,
            workflow_preselect=workflow_preselect, collection_subject=collection_subject,
            ui_lang="en" if workflow_path and lang == "en" else "zh",
        )

    def _render_frame(
        request: Request,
        scan,
        page,
        *,
        error: str | None = None,
        notice: str | None = None,
        warnings: tuple[str, ...] = (),
        subject: str = "数学",
        question_type: str = "填空",
        existing: int = 0,
    ):
        """手动框题页统一出口：整页图尺寸、旋转角、框的最小边都交给模板。"""
        return render(
            request,
            "scan_frame",
            scan=scan,
            page_width=page.width,
            page_height=page.height,
            angle=page.angle,
            min_side=scan_service.MANUAL_MIN_FRAME_SIDE,
            has_image=page.image is not None,
            existing=existing,
            error=error,
            notice=notice,
            warnings=list(warnings),
            subject=subject,
            question_type=question_type,
        )

    @app.get("/scans/{scan_id}/frame")
    def scan_frame_page(request: Request, scan_id: int):
        """家长自己框题：摆正后的整页图摆在上面，自己在图上拖框。

        这条入口**不看切题结果**：自动切题失败了（0 题）照样能用，方向角也只在
        没有切题行时才现判一次。
        """
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                workflow_path = _workflow_selection_image(session, scan, request.app.state.db_url)
                if workflow_path is not None:
                    return RedirectResponse(f"/scans/{scan.id}/select", status_code=303)
                page = scan_service.upright_page(
                    session, scan, db_url=request.app.state.db_url
                )
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            existing = len(scan_service.list_questions(session, scan.id))
        if not page.path.is_file():
            raise HTTPException(status_code=404, detail="照片文件不在了")
        return _render_frame(request, scan, page, existing=existing)

    @app.post("/scans/{scan_id}/frame")
    async def scan_frame_submit(request: Request, scan_id: int):
        """收家长框的 N 道题，一次全入或一条不入。

        框太小（任一边不到 30 像素）整批退回——半批入库比不收更让人糊涂。
        """
        pairs = await _read_form_pairs(request)
        form = dict(pairs)
        subject = (form.get("subject") or "").strip() or "数学"
        question_type = (form.get("question_type") or "").strip() or "填空"
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                workflow_path = _workflow_selection_image(session, scan, request.app.state.db_url)
                if workflow_path is not None:
                    raise HTTPException(status_code=409, detail="请使用工作流圈选提交")
                page = scan_service.upright_page(
                    session, scan, db_url=request.app.state.db_url, fixed_path=workflow_path
                )
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if not page.path.is_file():
                raise HTTPException(status_code=404, detail="照片文件不在了")
            frames = _parse_frames(
                _frame_fields(pairs), page.width, page.height
            )
            if frames is None:
                return _render_frame(
                    request,
                    scan,
                    page,
                    error="框没读出来，请在照片上重新框一次。",
                    subject=subject,
                    question_type=question_type,
                )
            if not frames:
                return _render_frame(
                    request,
                    scan,
                    page,
                    notice="一张都没框到：在照片上按住往下拖，框住一道题，松手就有了。",
                    subject=subject,
                    question_type=question_type,
                )
            min_side = scan_service.MANUAL_MIN_FRAME_SIDE
            small = [
                (index + 1, box)
                for index, box in enumerate(frames)
                if min(box[2] - box[0], box[3] - box[1]) < min_side
            ]
            if small:
                no, box = small[0]
                return _render_frame(
                    request,
                    scan,
                    page,
                    error=(
                        f"第 {no} 个框太小了（{box[2] - box[0]}×{box[3] - box[1]} 像素，"
                        f"任一边不到 {min_side} 像素），这次一道都没收。"
                        "把框拖大一点再提交。"
                    ),
                    subject=subject,
                    question_type=question_type,
                )
            try:
                result = scan_service.store_manual_frames(
                    session,
                    request.app.state.learner_id,
                    scan,
                    frames,
                    page_width=page.width,
                    page_height=page.height,
                    angle=page.angle,
                    page_image=page.image,
                    db_url=request.app.state.db_url,
                    subject=subject,
                    question_type=question_type,
                )
            except scan_service.FrameError as exc:
                return _render_frame(
                    request,
                    scan,
                    page,
                    error=str(exc),
                    subject=subject,
                    question_type=question_type,
                )
            existing = len(scan_service.list_questions(session, scan.id))
        if result.created_count:
            notice = f"已收好 {result.created_count} 题"
            if result.skipped:
                notice += (
                    f"；另有 {len(result.skipped)} 块之前已经在这张卷子里了，"
                    "没有重复建题"
                )
        else:
            notice = f"这 {len(result.skipped)} 块之前已经在这张卷子里了，没有重复建题"
        warnings = tuple(
            f"第 {no} 题没认出文字，先按原图收了（做题时看原图）"
            for no in result.blank_text
        )
        return _render_frame(
            request,
            scan,
            page,
            notice=notice,
            warnings=warnings,
            subject=subject,
            question_type=question_type,
            existing=existing,
        )

    @app.get("/scans/{scan_id}/upright")
    def scan_upright(request: Request, scan_id: int):
        """摆正后的整页图字节（手动框题的底图）。

        角度为 0 时**原样发原图**：不重采样、不重编码，字节与 `/image` 一致，
        免得给孩子看的那张卷子被悄悄糊一遍。
        """
        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(session, request.app.state.learner_id, scan_id)
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                page = scan_service.upright_page(
                    session, scan, db_url=request.app.state.db_url
                )
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not page.path.is_file():
            raise HTTPException(status_code=404, detail="照片文件不在了")
        if page.angle == 0:
            return FileResponse(page.path, media_type=scan_service.media_type_for(page.path))
        if page.image is None:
            raise HTTPException(status_code=404, detail="照片文件不在了")
        buffer = io.BytesIO()
        page.image.save(buffer, format="JPEG", quality=92)
        return Response(content=buffer.getvalue(), media_type="image/jpeg")

    def _question_and_crop_row(session: Session, learner_id: int, question_id: int):
        """本孩子的题 + 它对应的切题行（裁图记在那行上）；别人的题当不存在。"""
        question = session.get(Question, question_id)
        if question is None or question.learner_id != learner_id:
            return None, None
        row = session.scalars(
            select(ScanQuestion)
            .join(Scan, Scan.id == ScanQuestion.scan_id)
            .where(ScanQuestion.question_id == question_id, Scan.learner_id == learner_id)
            .order_by(ScanQuestion.id.asc())
        ).first()
        return question, row

    def _render_question_detail(
        request: Request,
        session: Session,
        question: Question,
        row: ScanQuestion | None,
        *,
        answer_error: str | None = None,
        answer_saved: bool = False,
        edit_error: str | None = None,
        status_code: int = 200,
    ) -> object:
        """详情页统一出口：GET 与「保存答案」共用同一份裁图/原图判定。"""
        scan = session.get(Scan, row.scan_id) if row is not None else None
        has_crop = False
        if row is not None:
            try:
                has_crop = scan_service.crop_image_path(
                    row, request.app.state.db_url
                ).is_file()
            except ValueError:
                has_crop = False
        return render(
            request,
            "question_detail",
            status_code=status_code,
            question=question,
            question_no=None if row is None else row.no,
            scan=scan,
            has_crop=has_crop,
            answer_error=answer_error,
            answer_saved=answer_saved,
            edit_error=edit_error,
            edited_at_text=_edited_at_text(question.edited_at),
            answer_max_chars=ANSWER_MAX_CHARS,
            date_text=_date_text(),
            nav_active="book",
        )

    @app.get("/questions")
    def questions_page(request: Request, q: str | None = None, subject: str | None = None):
        """错题本列表（v3 · E.10）：搜索框首条 + 科目筛选 + 每行「题干摘要 / 错过几次 /
        稳不稳」。稳不稳与「最近怎么样」同源谓词（wrong_count≥2 或学会过又掉回来 =
        还在反复错）。"""
        learner_id = request.app.state.learner_id
        keyword = (q or "").strip()
        with request.app.state.session_factory() as session:
            rows = []
            subjects: set[str] = set()
            items = session.scalars(
                select(ErrorItem).join(Question, Question.id == ErrorItem.question_id).where(
                                        ErrorItem.learner_id == learner_id, Question.learner_id == learner_id,
                                        workflow_review.ready_for_practice(ErrorItem.question_id))
            ).all()
            for item in items:
                question = session.get(Question, item.question_id)
                if question is None:
                    continue
                subjects.add(question.subject)
                if subject and question.subject != subject:
                    continue
                if keyword and keyword not in question.content:
                    continue
                repeated = item.wrong_count >= 2 or item.restored_at is not None
                rows.append(
                    {
                        "question_id": question.id,
                        "subject": question.subject,
                        "content": question.content,
                        "wrong_count": item.wrong_count,
                        "repeated": repeated,
                    }
                )
            rows.sort(key=lambda row: row["question_id"], reverse=True)
            total = len(items)
        return render(
            request,
            "questions",
            rows=rows,
            total=total,
            keyword=keyword,
            subject=subject or "",
            subjects=sorted(subjects),
            date_text=_date_text(),
            nav_active="book",
            badge_book=total or None,
        )

    def _delete_question_cascade(session: Session, question: Question) -> None:
        """删一道题的全部痕迹：复习记录 → 错题条 → 切题行 → 题本身（真删除，决策点 2）。"""
        from cairn.models.concept import question_concept
        from cairn.models.question import question_knowledge_link
        from cairn.models.tutor import TutorMessage, TutorSession
        from cairn.models.recap import RecapAttempt
        from cairn.models.collection import CollectionPracticeItem, CollectionVersionQuestion
        from cairn.collection_service import CollectionError, ensure_question_not_in_active_source, revoke_question_versions

        session.execute(update(Question).where(Question.id == question.id).values(
            content=Question.content).execution_options(synchronize_session=False))
        unfinished = session.scalar(select(WorkflowReviewItem.id).join(
            WorkflowRun, WorkflowRun.id == WorkflowReviewItem.run_id
        ).where(WorkflowReviewItem.question_id == question.id, WorkflowRun.status != "completed").limit(1))
        if unfinished is not None:
            raise HTTPException(status_code=409, detail="请先完成这道题所在卷子的处理")
        try:
            ensure_question_not_in_active_source(session, question.id)
        except CollectionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if session.scalar(select(CollectionPracticeItem.id).join(
                CollectionVersionQuestion, CollectionVersionQuestion.version_id == CollectionPracticeItem.version_id
        ).where(CollectionVersionQuestion.question_id == question.id).limit(1)) is not None:
            raise HTTPException(status_code=409, detail="这道题所属的题集版本仍有练习副本，请先处理关联的练习题")
        session.execute(delete(CollectionPracticeItem).where(CollectionPracticeItem.question_id == question.id))
        tutor_ids = select(TutorSession.id).where(TutorSession.question_id == question.id)
        session.execute(delete(TutorMessage).where(TutorMessage.session_id.in_(tutor_ids)))
        session.execute(delete(TutorSession).where(TutorSession.question_id == question.id))
        revoke_question_versions(session, question.id)
        session.execute(delete(WorkflowReviewItem).where(WorkflowReviewItem.question_id == question.id))
        session.execute(delete(question_concept).where(question_concept.c.question_id == question.id))
        session.execute(delete(question_knowledge_link).where(question_knowledge_link.c.question_id == question.id))
        session.execute(update(Question).where(Question.variant_of == question.id).values(variant_of=None))
        session.execute(update(Scan).where(Scan.question_id == question.id).values(question_id=None))
        item_ids = list(
            session.scalars(
                select(ErrorItem.id).where(ErrorItem.question_id == question.id)
            )
        )
        if item_ids:
            session.execute(delete(RecapAttempt).where(RecapAttempt.error_item_id.in_(item_ids)))
            session.execute(
                delete(Review).where(Review.error_item_id.in_(item_ids))
            )
            session.execute(delete(ErrorItem).where(ErrorItem.id.in_(item_ids)))
        session.execute(
            delete(ScanQuestion).where(ScanQuestion.question_id == question.id)
        )
        session.delete(question)

    @app.post("/questions/{question_id}/delete")
    def question_delete(request: Request, question_id: int):
        """删掉这道题（v3 · E.10）：详情页文字链「删掉」→ 确认 → 本端点，303 回列表。"""
        with request.app.state.session_factory() as session:
            question, _row = _question_and_crop_row(
                session, request.app.state.learner_id, question_id
            )
            if question is None:
                raise HTTPException(status_code=404, detail="没有这道题")
            _delete_question_cascade(session, question)
            session.commit()
        return RedirectResponse("/questions", status_code=303)

    @app.get("/questions/{question_id}")
    def question_detail(request: Request, question_id: int):
        """错题详情：题干 + 答案 + 这道题从卷子上裁下来的那块原图。

        没有裁图（老数据 / 裁图失败 / 文件被删）就显示"暂无原图"——页面永不 500。
        扫描题的标准答案可以在这一页补录（`POST /questions/{id}/answer`）。
        """
        with request.app.state.session_factory() as session:
            question, row = _question_and_crop_row(
                session, request.app.state.learner_id, question_id
            )
            if question is None:
                raise HTTPException(status_code=404, detail="没有这道题")
            return _render_question_detail(
                request,
                session,
                question,
                row,
                answer_saved=request.query_params.get("saved") == "1",
            )

    @app.post("/questions/{question_id}/answer")
    async def question_answer_save(request: Request, question_id: int):
        """家长补录 / 修改 / 清空标准答案。

        去首尾空格；空串 = 置 NULL（这道题回到「没有标准答案」，练习提交交人工
        两键，不判 again）。超过 `ANSWER_MAX_CHARS` 拒收并留在页面上重填——
        宁可让家长看见提示，也不写一条被截断的答案进去。
        """
        form = await _read_form(request)
        answer = (form.get("answer") or "").strip()
        with request.app.state.session_factory() as session:
            question, row = _question_and_crop_row(
                session, request.app.state.learner_id, question_id
            )
            if question is None:
                raise HTTPException(status_code=404, detail="没有这道题")
            if len(answer) > ANSWER_MAX_CHARS:
                return _render_question_detail(
                    request,
                    session,
                    question,
                    row,
                    answer_error=f"答案最多 {ANSWER_MAX_CHARS} 字，这条没有保存",
                    status_code=400,
                )
            question.answer = answer or None
            session.commit()
        return RedirectResponse(f"/questions/{question_id}?saved=1", status_code=303)

    def _workflow_review_return(session: Session, learner_id: int, question_id: int, back: str) -> str | None:
        match = re.fullmatch(r"workflow-([0-9]+)(-en)?", back)
        if match is None:
            return None
        scan = scan_service.get_scan(session, learner_id, int(match.group(1)))
        if scan is None:
            return None
        linked = session.scalar(
            select(WorkflowReviewItem.id)
            .join(WorkflowRun, WorkflowRun.id == WorkflowReviewItem.run_id)
            .where(WorkflowRun.scan_id == scan.id, WorkflowReviewItem.question_id == question_id)
        )
        if linked is None:
            return None
        return f"/scans/{scan.id}/workflow-review" + ("?lang=en" if match.group(2) else "")

    @app.get("/questions/{question_id}/edit")
    def question_edit_page(request: Request, question_id: int):
        """改一下这道题（v3 · E.7）：全流程唯一表单页，永不在必经路径上。

        `?back=queue` 时「不改了，返回」回待确认屏（确认流程进来的路）；
        默认回详情页。写路径仍是 POST 本路径（语义不动）。
        """
        with request.app.state.session_factory() as session:
            question, row = _question_and_crop_row(
                session, request.app.state.learner_id, question_id
            )
            if question is None:
                raise HTTPException(status_code=404, detail="没有这道题")
            has_crop = False
            if row is not None:
                try:
                    has_crop = scan_service.crop_image_path(
                        row, request.app.state.db_url
                    ).is_file()
                except ValueError:
                    has_crop = False
            back = request.query_params.get("back") or ""
            back_url = _workflow_review_return(session, request.app.state.learner_id, question_id, back)
        return render(
            request,
            "edit",
            question=question,
            has_crop=has_crop,
            back=back,
            back_url=back_url,
            ui_lang="en" if back_url and back.endswith("-en") else "zh",
            date_text=_date_text("en" if back_url and back.endswith("-en") else "zh"),
            nav_active="capture",
        )

    @app.post("/questions/{question_id}/edit")
    async def question_edit_save(request: Request, question_id: int):
        """人工改题干 / 答案 / 解析（T3-a），并留下"谁在什么时候改的"。

        题干是这道题唯一不能为空的字段：去掉首尾空白后为空 → 400，**原样回填
        库里的值**（家长手滑全选删了就点保存时，不该把题改成空白页）。答案与
        解析可空，空串 = NULL，与补答案那条路同一口径。

        三个字段都没变也照样写 `edited_at`：人确实点过保存，这个动作本身要留痕。
        写完 303 回详情页（不是 JSON，这条走表单）。
        """
        form = await _read_form(request)
        content = (form.get("content") or "").strip()
        answer = (form.get("answer") or "").strip()
        analysis = (form.get("analysis") or "").strip()
        # 可选 ?back=queue：从待确认屏的「改一下」进来，保存/返回都回那屏。
        # 不带 back 的老调用方行为一字不变（303 回详情页）。
        back = request.query_params.get("back") or ""
        with request.app.state.session_factory() as session:
            question, row = _question_and_crop_row(
                session, request.app.state.learner_id, question_id
            )
            if question is None:
                raise HTTPException(status_code=404, detail="没有这道题")
            back_url = _workflow_review_return(session, request.app.state.learner_id, question_id, back)
            if not content:
                if back == "queue" or back_url:
                    has_crop = False
                    if row is not None:
                        try:
                            has_crop = scan_service.crop_image_path(
                                row, request.app.state.db_url
                            ).is_file()
                        except ValueError:
                            has_crop = False
                    return render(
                        request,
                        "edit",
                        status_code=400,
                        question=question,
                        has_crop=has_crop,
                        back=back,
                        back_url=back_url,
                        edit_error=("Question text is required; nothing was saved."
                                    if back.endswith("-en") else "题干不能为空，这条没有保存"),
                        keep_form={"content": content, "answer": answer, "analysis": analysis},
                        ui_lang="en" if back_url and back.endswith("-en") else "zh",
                        date_text=_date_text("en" if back_url and back.endswith("-en") else "zh"),
                        nav_active="capture",
                    )
                return _render_question_detail(
                    request,
                    session,
                    question,
                    row,
                    edit_error="题干不能为空，这条没有保存",
                    status_code=400,
                )
            question.content = content
            question.answer = answer or None
            question.analysis = analysis or None
            question.edited_at = _edit_stamp_now()
            question.edited_by = "human"
            session.commit()
        if back == "queue":
            return RedirectResponse("/review-queue", status_code=303)
        if back_url:
            return RedirectResponse(back_url, status_code=303)
        return RedirectResponse(f"/questions/{question_id}", status_code=303)

    def _digitize_fields(question: Question) -> dict[str, str]:
        """复核页三个输入框的初值：就是库里现在的值（可能为空）。"""
        return {
            "content": question.content,
            "answer": question.answer or "",
            "analysis": question.analysis or "",
        }

    def _render_digitize(
        request: Request,
        question: Question,
        row: ScanQuestion | None,
        *,
        fields: dict[str, str] | None = None,
        provider: str = DIGITIZE_DEFAULT_PROVIDER,
        result: Digitized | None = None,
        model_error: str | None = None,
        crop_missing: bool = False,
        status_code: int = 200,
    ) -> object:
        """复核页统一出口：GET、跑模型、被拒的 provider 都从这一条出。

        页面上「读出来的三字段」只在这次请求里渲染，**一个字段都不写库**——
        保存仍走 `POST /questions/{id}/edit`（那条本来就会留痕）。
        """
        has_crop = False
        if row is not None:
            try:
                has_crop = scan_service.crop_image_path(
                    row, request.app.state.db_url
                ).is_file()
            except ValueError:
                has_crop = False
        model_text = None
        field_count = 0
        if result is not None:
            label = DIGITIZE_PROVIDER_LABELS.get(result.provider, result.provider)
            model_text = f"{label} {result.model} · {result.ms / 1000:.1f}s"
            field_count = sum(
                1 for value in (result.stem, result.answer, result.analysis) if value
            )
        return render(
            request,
            "digitize",
            status_code=status_code,
            question=question,
            question_no=None if row is None else row.no,
            has_crop=has_crop,
            fields=fields if fields is not None else _digitize_fields(question),
            provider=provider,
            result=result,
            model_error=model_error,
            crop_missing=crop_missing,
            model_text=model_text,
            field_count=field_count,
        )

    @app.get("/questions/{question_id}/digitize")
    def question_digitize(request: Request, question_id: int):
        """复核页：题块图 + 三个字段（先填库里的值）+ 模型二选一 + 读/保存。

        进来时不发任何请求，家长自己按「让机器读这题」才跑模型。
        """
        with request.app.state.session_factory() as session:
            question, row = _question_and_crop_row(
                session, request.app.state.learner_id, question_id
            )
            if question is None:
                raise HTTPException(status_code=404, detail="没有这道题")
            return _render_digitize(request, question, row)

    @app.post("/questions/{question_id}/digitize")
    async def question_digitize_run(request: Request, question_id: int):
        """跑一次模型，把读出来的三字段渲染回同一页；**不落库**。

        - `provider` 只认 `local` / `cloud`（缺省 local），别的值 400 留在页面上；
        - 模型失败（`DigitizeError`：无 key / 超时 / 返回不是 JSON）→ 200 同页，
          红条写中文原因，字段保持可编辑、可重试；
        - 成功也只是把值画进表单，家长点「保存这道题」才走 `/edit`。
        """
        form = await _read_form(request)
        provider = (form.get("provider") or DIGITIZE_DEFAULT_PROVIDER).strip()
        with request.app.state.session_factory() as session:
            question, row = _question_and_crop_row(
                session, request.app.state.learner_id, question_id
            )
            if question is None:
                raise HTTPException(status_code=404, detail="没有这道题")
            if provider not in DIGITIZE_PROVIDERS:
                return _render_digitize(
                    request,
                    question,
                    row,
                    provider=DIGITIZE_DEFAULT_PROVIDER,
                    model_error="模型只能选「本机」或「云端」，请重选再试。",
                    status_code=400,
                )
            path = None
            if row is not None:
                try:
                    path = scan_service.crop_image_path(
                        row, request.app.state.db_url
                    )
                except ValueError:
                    path = None
            if path is None:
                return _render_digitize(
                    request,
                    question,
                    row,
                    provider=provider,
                    crop_missing=True,
                    model_error="这道题没有原图，机器读不了；请手动填，或换一道有原图的题。",
                )
            try:
                result = describe(str(path), provider=provider)
            except DigitizeError as exc:
                return _render_digitize(
                    request,
                    question,
                    row,
                    provider=provider,
                    model_error=str(exc),
                )
            return _render_digitize(
                request,
                question,
                row,
                fields={
                    "content": result.stem,
                    "answer": result.answer,
                    "analysis": result.analysis,
                },
                provider=provider,
                result=result,
            )

    @app.get("/questions/{question_id}/crop")
    def question_crop(request: Request, question_id: int):
        """题块原图字节；没有裁图/文件不在了一律 404（详情页自己显示"暂无原图"）。"""
        with request.app.state.session_factory() as session:
            question, row = _question_and_crop_row(
                session, request.app.state.learner_id, question_id
            )
            if question is None:
                raise HTTPException(status_code=404, detail="没有这道题")
            if row is None:
                raise HTTPException(status_code=404, detail="这道题没有原图")
            try:
                path = scan_service.crop_image_path(row, request.app.state.db_url)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="题块图文件不在了")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/cut-feedback")
    async def cut_feedback_submit(request: Request):
        """Store one whole-page cut-correction snapshot (docs/CUT-FEEDBACK-SPEC.md).

        The page is committed as a snapshot, so resubmitting the same
        `(scan_id, frame_key)` overwrites the row instead of adding another one.
        Bboxes arrive in original-image pixels and are stored verbatim.
        """
        body = await _read_json(request)
        try:
            scan_id = int(body.get("scan_id"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="scan_id 必须是整数") from exc
        frames = body.get("frames")
        if not isinstance(frames, list):
            raise HTTPException(status_code=400, detail="frames 必须是数组")
        image_sha256 = str(body.get("image_sha256") or "").strip()
        recipe = str(body.get("recipe") or "").strip()
        params_hash = str(body.get("params_hash") or "").strip()
        if not (image_sha256 and recipe and params_hash):
            raise HTTPException(
                status_code=400, detail="image_sha256 / recipe / params_hash 都要填"
            )
        try:
            image_w = int(body.get("image_w"))
            image_h = int(body.get("image_h"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="image_w / image_h 必须是整数"
            ) from exc

        with request.app.state.session_factory() as session:
            scan = scan_service.get_scan(
                session, request.app.state.learner_id, scan_id
            )
            if scan is None:
                raise HTTPException(status_code=404, detail="没有这张照片")
            try:
                saved = scan_service.record_cut_feedback(
                    session,
                    scan,
                    frames,
                    image_sha256=image_sha256,
                    image_w=image_w,
                    image_h=image_h,
                    recipe=recipe,
                    params_hash=params_hash,
                )
            except scan_service.CutFeedbackError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"saved": saved}, status_code=201)

    @app.get("/api/cut-feedback/stats")
    def cut_feedback_stats_api(request: Request):
        """Four rates + mean IoU for a window; no rates below 20 automatic frames."""
        params = request.query_params
        since = _cut_feedback_bound(params.get("from"), end=False)
        until = _cut_feedback_bound(params.get("to"), end=True)
        recipe = (params.get("recipe") or "").strip() or None
        with request.app.state.session_factory() as session:
            stats = scan_service.cut_feedback_stats(
                session, since=since, until=until, recipe=recipe
            )
        return JSONResponse(stats)

    @app.get("/api/cut-feedback/export")
    def cut_feedback_export(request: Request):
        """JSONL export, one stored frame per line; images are opt-in.

        Default `include_image=0`: bbox + metadata + `image_sha256` only.
        `include_image=1` adds `scan.path` (the original image, never a copy) —
        the page may carry a child's name or school, so mask it before sharing.
        """
        params = request.query_params
        raw_flag = (params.get("include_image") or "0").strip().lower()
        include_image = raw_flag not in ("", "0", "false", "no")
        since = _cut_feedback_bound(params.get("since"), end=False)
        with request.app.state.session_factory() as session:
            records = scan_service.cut_feedback_records(
                session, since=since, include_image=include_image
            )
        lines = [json.dumps(record, ensure_ascii=False) for record in records]
        payload = "\n".join(lines) + ("\n" if lines else "")
        filename = f"cut-feedback-{datetime.now().astimezone():%Y%m%d}.jsonl"
        return Response(
            content=payload,
            media_type="application/x-ndjson; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    @app.get("/export")
    def export_download(request: Request):
        """家长导出备份：当前孩子的题库，一个 JSON 文件，中文原样写出。

        当前孩子 = `app.state.learner_id`（本卡不改身份切换 UI，下一卡再让家长切）。
        """
        now = datetime.now().astimezone()
        with request.app.state.session_factory() as session:
            try:
                document = export_service.build_export(
                    session, request.app.state.learner_id, now=now
                )
            except export_service.IncompleteWorkflowExport as exc:
                return render(request, "export_blocked", scan_id=exc.scan_id,
                              status_code=409, date_text=_date_text(), nav_active="capture")
            except export_service.InconsistentLearnerData:
                return render(request, "export_error", status_code=409,
                              date_text=_date_text(), nav_active="settings")
        return Response(
            content=export_service.dumps(document),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{export_service.export_filename(now)}"'
                )
            },
        )

    @app.get("/export/anki")
    def export_anki(request: Request):
        """家长导出 Anki 牌组：一个 .apkg，拖进 Anki 就能按学科复习。"""
        now = datetime.now().astimezone()
        with request.app.state.session_factory() as session:
            package = anki_export.build_anki_package(
                session, request.app.state.learner_id, now=now
            )
        return Response(
            content=package,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{anki_export.anki_filename(now)}"'
                )
            },
        )

    @app.get("/import")
    def import_form(request: Request):
        """换机恢复：把 /export 的 JSON 传回来。只接空库，风险写在页面上。"""
        return render(request, "import")

    @app.post("/import")
    async def import_upload(request: Request):
        # multipart 解析走 starlette 的 request.form()，依赖 mcp 已带的 python-multipart，
        # 不是新引入的依赖（pyproject.toml 未改）。
        form = await request.form()
        upload = form.get("backup")
        if upload is None or not getattr(upload, "filename", ""):
            return render(request, "import", error="请选择一个 .json 备份文件。")
        raw = await upload.read()
        try:
            document = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            return render(request, "import", error=f"备份文件不是 UTF-8 编码：{exc}")
        except json.JSONDecodeError as exc:
            return render(request, "import", error=f"备份文件不是有效的 JSON：{exc}")

        with request.app.state.session_factory() as session:
            try:
                report = import_service.load_backup(session, document)
            except import_service.BackupError as exc:
                return render(request, "import", error=str(exc))
            # 备份里的 learner id 不一定是启动时建的那条；不刷新，练习页会指向不存在的学习者。
            learner = session.scalars(
                select(Learner).order_by(Learner.id.asc()).limit(1)
            ).first()
            if learner is not None:
                request.app.state.learner_id = learner.id
        return render(request, "import", report=report)

    return app


def _int_field(form: dict[str, str], name: str) -> int | None:
    raw = form.get(name, "")
    return int(raw) if raw.isdigit() else None


def _today() -> date:
    """家长填的是日历日，所以「今天」按服务器本地时区切。"""
    return datetime.now().astimezone().date()


def _parse_exam_date(raw: str) -> date:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        raise ValidationError("考试日期请按 YYYY-MM-DD 填写") from None


def _exam_bar(request: Request, session: Session | None) -> dict | None:
    """练习页顶部的倒计时条；没有未过期考试时返回 None（模板整块不渲染）。"""
    if session is None:
        return None
    today = _today()
    exam = exam_service.active_exam(session, request.app.state.learner_id, today)
    if exam is None:
        return None
    return {
        "name": exam.name,
        "subject": exam.subject,
        "days_left": exam_service.days_left(exam, today),
        "in_sprint": exam_service.in_sprint(exam, today),
        "window_days": exam_service.SPRINT_DAYS,
    }


def _explicit_lang(raw: str | None) -> str | None:
    """Single-entry select: 'auto' (or missing) → None → service auto-detects;
    an explicit en/zh is passed through and overrides detection."""
    return raw if raw in ("en", "zh") else None


def _picked_word_ids(form: dict[str, str]) -> list[int]:
    """勾选的教材条目 id（复选框名 word_<i>，只勾了才会出现在表单里）。"""
    count = _int_field(form, "pick_count") or 0
    ids: list[int] = []
    for index in range(count):
        raw = form.get(f"word_{index}", "")
        if raw.isdigit():
            ids.append(int(raw))
    return ids


def _batch_payloads(form: dict[str, str]) -> list[EntryPayload]:
    """Collect the preview rows the parent kept (unchecked row == deleted)."""
    count = _int_field(form, "row_count") or 0
    payloads: list[EntryPayload] = []
    for index in range(count):
        if f"include_{index}" not in form:
            continue
        content = form.get(f"content_{index}", "").strip()
        answer = form.get(f"answer_{index}", "").strip()
        if not content or not answer:
            continue
        lang = "zh" if form.get(f"lang_{index}") == "zh" else "en"
        payloads.append(
            EntryPayload(
                # 学科用家长看的中文名，与单条录入保持一致（不写 english/chinese）
                subject="语文" if lang == "zh" else "英语",
                question_type="词语" if lang == "zh" else "单词",
                content=content,
                answer=answer,
                lang=lang,
                # 朗读 cue：预览里可改；清空则服务层按 audio_cue 重新推导
                audio_text=form.get(f"audio_text_{index}") or None,
            )
        )
    return payloads


def _queue_row(session: Session, item: ErrorItem, *, db_url: str | None = None) -> dict:
    """Flatten one pending-review item for the template (no logic, just joins)."""
    question = session.get(Question, item.question_id)
    has_crop = False
    if db_url is not None:
        crop_row = session.scalars(
            select(ScanQuestion)
            .join(Scan, Scan.id == ScanQuestion.scan_id)
            .where(ScanQuestion.question_id == question.id, Scan.learner_id == item.learner_id)
            .order_by(ScanQuestion.id.asc())
        ).first()
        if crop_row is not None:
            try:
                has_crop = scan_service.crop_image_path(crop_row, db_url).is_file()
            except ValueError:
                has_crop = False
    return {
        "item_id": item.id,
        # 详情页入口（/questions/{id}）：列表页每题一个「看原图」链接要用
        "question_id": question.id,
        "subject": question.subject,
        "question_type": question.question_type,
        "content": question.content,
        "confidence": item.confidence,
        # G2-4 确认页要的：讲解一段 + 有没有裁图（原图小图）
        "analysis": question.analysis,
        "has_crop": has_crop,
    }


app = create_app()
