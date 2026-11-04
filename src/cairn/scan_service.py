"""抓拍：取一帧 → 落盘 → 入库（0.2 卡11）；上传通道（迁移 v15）。

目录约定（`docs/DEVICE-BASE-RESEARCH.md` 第四节）：

    <db 同目录>/scans/yyyy/mm/<uuid>.jpg     # AVFoundation 交原始 JPEG
    <db 同目录>/scans/yyyy/mm/<uuid>.png     # 假设备交 PNG

库里存的是**相对库目录**的路径。写盘先于入库：commit 失败就把刚写的文件删掉，
不留没人认领的孤儿图；反过来「库里有行、盘上没图」才是更难查的坏状态。

上传通道：浏览器端质量门算完指标后把原图 + 指标 JSON 一起 POST 上来，
服务端做三类硬校验（是不是图、超没超 12MB、短边够不够）——指标判定在前端，
服务端不重复算。前两类只认文件头；第三类必须真读像素尺寸：聊天软件压缩过的
图（实测 549×1280）只能切出 7 题，同一张卷子的原图切出 18 题，静默漏题比
报错危险，所以短边不足的图在落盘前就拒。

图像库在两处按需导入 PIL：上传时量短边（`image_dimensions`）、切题落库时按 bbox
裁题块图（`crop_question_image`）——模块导入时不拖图像依赖；裁不出来只记
一行 warning——照片入库这条路不许被裁图挡住。

上传还会顺手把图"扫描化"（迁移 v19）：先 `_crop_page` 把纸从背景里裁出来
（`detect_paper` → 闸门 → 裁或原样 → 方向归正），结果**另存一份**
`<uuid>.crop.png`（裁完的图是我们产生的，一律无损）。上传的那份原图从头到尾只读不写，
字节与上传时完全一致——原图必须保留，这是不可逆操作的边界。再用 `_write_processed` 调
`ocr.preprocess` 找纸边 → 透视矫正 → 去阴影，最后调 `ocr.enhance` 把纸压白、把字
压黑（灰度），落成 `<uuid>.processed.jpg` 并记 `processed_path`。两条链各自独立：
裁边关掉增强照跑，增强关掉裁边照跑。
裁边的结论（裁了没裁、为什么）写在图旁边的 `<uuid>.crop.json`，配方里「裁切」
那一步照实念给家长看。读图链路（切题/OCR/展示/手动框）统一走 `read_image_path`：
先裁好的那张（新卷子是 `<uuid>.crop.png`，老卷子还在 `<uuid>.crop.jpg`）、其次
processed、最后原图——三者都是同一页、同一套坐标，`bbox_json.page` 才是对的尺寸。

手动框题（v19 卡）是第二条不依赖切题的入口：`upright_page` 给出摆正后的整页图，
`store_manual_frames` 把家长自己拖的框一次性落成 scan_question + 裁图 + question
+ error_item（一个事务）。它跟自动切题共用同一套坐标口径与裁图函数。
"""

from __future__ import annotations

import io
import json
import logging
import math
import mimetypes
import os
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cairn import entry_service, tools
from cairn.capture.base import CameraBackend, Capabilities, CaptureError, DeviceInfo, Frame
from cairn.models.cut_feedback import CutFeedback, now_iso
from cairn.models.learner import utcnow
from cairn.models.error_item import REVIEW_STATUS_NEW, ErrorItem
from cairn.models.question import Question
from cairn.models.scan import Scan, ScanQuestion
from cairn.workflow.service import active_version_id

if TYPE_CHECKING:  # 只为类型标注：不让 scan_service 在导入时拖上 OCR 依赖
    import numpy as np
    from PIL import Image

    from cairn.ocr.crop import PaperResult
    from cairn.ocr.cut import QuestionBlock

logger = logging.getLogger(__name__)

SCANS_DIRNAME = "scans"

# 上传硬上限：手机原图 JPEG 通常 2–8MB，12MB 覆盖 4800 万像素级原图。
MAX_UPLOAD_BYTES = 12 * 1024 * 1024

# 短边下限：549×1280 的压缩图只能切出 7 题，同一张卷子的原图切出 18 题。
# 1000 是 Lead 实测给出的下限（短边 1000 及以上的图切题数与原图一致），
# 环境变量 `CAIRN_MIN_IMAGE_SIDE` 可覆盖（测试用它验边界）。
MIN_IMAGE_SIDE = 1000
MIN_IMAGE_SIDE_ENV = "CAIRN_MIN_IMAGE_SIDE"

# 魔数白名单（便宜的第一道筛子：不解码，只认文件头）：JPEG / PNG / GIF / BMP /
# WEBP（RIFF 容器）/ HEIC·HEIF·AVIF（ISO BMFF 的 ftyp 容器，iPhone 原图）。
_IMAGE_CHECKS: tuple[tuple[int, bytes], ...] = (
    (0, b"\xff\xd8\xff"),  # JPEG
    (0, b"\x89PNG\r\n\x1a\n"),  # PNG
    (0, b"GIF87a"),  # GIF
    (0, b"GIF89a"),
    (0, b"BM"),  # BMP
    (0, b"RIFF"),  # WEBP（还需 8..12 == WEBP）
    (4, b"ftyp"),  # HEIC / HEIF / AVIF（ISO 容器，brand 不细究）
)


class UploadError(Exception):
    """上传被硬校验拒绝（非图片 / 超限）；web 层映射 400。"""


def looks_like_image(data: bytes) -> bool:
    """按魔数判断是不是图片；内容型校验到这里为止，解码交给浏览器端。"""
    for offset, magic in _IMAGE_CHECKS:
        if data[offset : offset + len(magic)] != magic:
            continue
        if magic == b"RIFF":
            return data[8:12] == b"WEBP"
        return True
    return False


def min_image_side() -> int:
    """当前生效的短边下限；环境变量缺失/不是正整数时回落到默认值。

    每次调用都读环境变量（不缓存），测试才能用 monkeypatch 改完立刻生效。
    """
    raw = os.environ.get(MIN_IMAGE_SIDE_ENV, "").strip()
    if not raw:
        return MIN_IMAGE_SIDE
    try:
        value = int(raw)
    except ValueError:
        return MIN_IMAGE_SIDE
    return value if value > 0 else MIN_IMAGE_SIDE


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """只读文件头拿 (宽, 高)，不解码整张图。

    返回 None 表示**尺寸未知**（PIL 认不出的格式、或坏到读不出头），
    不是"文件不合法"：是不是图由 `looks_like_image` 判，这里只负责量尺寸，
    量不出来就不能当成尺寸不足去拒——那会让既有入口（HEIC 等）回退。
    """
    from PIL import Image  # 按需导入：不量尺寸的调用方不拖图像依赖

    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception:  # noqa: BLE001 —— 解不开就当尺寸未知，交给后续流程
        return None
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def parse_quality(raw: str | None) -> str | None:
    """前端报的质量指标 JSON；解析失败/非对象一律忽略（返回 None），不许 500。"""
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return raw if isinstance(parsed, dict) else None


def _quality_dimensions(quality_json: str | None) -> tuple[int, int]:
    """从指标 JSON 里捎带的 width/height 取分辨率；取不到就 0（不阻塞入库）。"""
    if quality_json is None:
        return 0, 0
    try:
        parsed = json.loads(quality_json)
        width = int(parsed.get("width") or 0)
        height = int(parsed.get("height") or 0)
    except (ValueError, TypeError):
        return 0, 0
    return max(width, 0), max(height, 0)


def save_upload(
    session: Session,
    learner_id: int,
    data: bytes,
    *,
    db_url: str,
    quality: str | None = None,
    note: str | None = None,
    defer_processing: bool = False,
    workflow_version_id: str | None = None,
) -> Scan:
    """上传原图 → 落盘 → 入库（source='upload'）。拒绝时不出文件不出行。"""
    if not data:
        raise UploadError("空文件")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("图片超过 12MB 上限")
    if not looks_like_image(data):
        raise UploadError("不是可识别的图片文件")

    # 尺寸校验在落盘之前：小图会静默切残（漏题没人发现），宁可让家长换原图。
    limit = min_image_side()
    size = image_dimensions(data)
    if size is not None and min(size) < limit:
        width_px, height_px = size
        raise UploadError(
            f"这张图太小（{width_px}×{height_px}），切题会漏。"
            f"请用手机原图或文件方式重新发送（短边至少 {limit} 像素）。"
        )

    quality_json = parse_quality(quality)
    width, height = _quality_dimensions(quality_json)
    root = db_dir(db_url)
    # 与抓拍同一套目录约定；扩展名统一 .jpg（手机原图是 JPEG/HEIC，
    # 浏览器画布导出也是 JPEG），格式真身以文件头为准。
    stamp = datetime.now(timezone.utc).astimezone()
    folder = root / SCANS_DIRNAME / f"{stamp:%Y}" / f"{stamp:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{uuid.uuid4().hex}.jpg"
    target.write_bytes(data)

    if defer_processing:
        # Workflow execution owns image processing after the version is pinned.
        from cairn.workflow import execution

        scan = Scan(
            learner_id=learner_id,
            device_id="upload",
            device_name="浏览器上传",
            width=width,
            height=height,
            captured_at=utcnow(),
            path=target.relative_to(root).as_posix(),
            note=note,
            source="upload",
            quality_json=quality_json,
            workflow_version_id=workflow_version_id or active_version_id(session),
        )
        try:
            session.add(scan)
            session.flush()
            execution.start(session, scan)
            session.commit()
        except Exception:
            session.rollback()
            target.unlink(missing_ok=True)
            raise
        return scan

    # 原图落盘之后、增强之前，先把照片"裁到纸"（找纸 → 闸门 → 裁或原样 → 方向归正），
    # 结果**另存** `<uuid>.crop.png`：上传的那份原图只读不写，字节与上传时一致。
    # 裁好的那张只靠文件名认领（`crop_path_for_scan`），不改库、不加列。
    crop_record, crop_file = _crop_page(target)

    # 再试着把这张图"扫描化"（找纸边 → 透视矫正 → 去阴影 → 压白压黑），另存
    # `<uuid>.processed.jpg`。增强读的是裁好的那张（没有裁就读原图），两条链各自
    # 独立：裁边关掉，增强照跑；增强关掉，裁边照跑。
    # 任何异常也只记一行 warning——上传这条路不许被预处理挡住。
    processed_file = _write_processed(target, folder, source=crop_file)
    processed_path = (
        processed_file.relative_to(root).as_posix() if processed_file is not None else None
    )

    scan = Scan(
        learner_id=learner_id,
        device_id="upload",
        device_name="浏览器上传",
        width=width,
        height=height,
        captured_at=utcnow(),
        path=target.relative_to(root).as_posix(),
        note=note,
        source="upload",
        quality_json=quality_json,
        processed_path=processed_path,
        workflow_version_id=workflow_version_id or active_version_id(session),
    )
    session.add(scan)
    try:
        session.commit()
    except Exception:
        session.rollback()
        target.unlink(missing_ok=True)
        if crop_record is not None:
            crop_record.unlink(missing_ok=True)
        if crop_file is not None:
            crop_file.unlink(missing_ok=True)
        if processed_file is not None:
            processed_file.unlink(missing_ok=True)
        raise
    return scan


def _write_processed(target: Path, folder: Path, *, source: Path | None = None) -> Path | None:
    """原图 → 处理图（摆正 + 增强）→ 灰度 JPEG；没做成返回 None。

    三步，顺序固定，但**互不阻塞**：

    1. `preprocess.save_processed`：找纸边 → 透视矫正 → 去阴影。找到就用它的
       产物当增强的输入；找不到纸边（`changed=False`）就**直接增强原图**——
       增强只需要这一页的灰度分布，不需要四边形。本次实测的三张真卷子全都
       找不到纸边，旧的门槛（没纸边就不落处理图）会让它们在真实上传里一张都不被增强。
    2. `enhance.save_gray`：压白压黑，写成灰度。
    3. `enhance` 工具关掉时不留这张文件（`processed_path` 为空，读图回落原图）。

    输入是 `source`（上传时裁好的那张，没有就 `target` 原图）：增强要和切题看
    同一页，否则一页纸两种尺寸，框全错位。文件名仍按 `target` 起——
    `<uuid>.processed.jpg`，家长在 Finder 里三个文件同一个前缀。
    `target` 从头到尾只读不写。增强失败按原图入库，并把半成品删掉——
    库里指着一张没增强的图比没有图更难查。
    """
    try:
        # `enhance` 这个名字在包里是 preprocess 的去阴影函数，模块要单独取函数
        from cairn.ocr import preprocess, save_gray
    except Exception as exc:  # noqa: BLE001 —— 装不上 cv2 也不许挡上传
        logger.warning("预处理不可用，按原图入库：%s", exc)
        return None

    if not tools.is_enabled("enhance"):
        # 增强关掉：不落处理图（`processed_path` 留空 = 读图回落原图）
        logger.warning("增强已关闭，不落处理图（%s）", target.name)
        return None

    page = source if source is not None else target
    candidate = folder / f"{target.stem}.processed.jpg"
    enhanced_from = page
    try:
        if preprocess.save_processed(page, candidate):
            enhanced_from = candidate  # 摆正过的图更规整，拿它当增强的输入
        else:
            logger.warning(
                "预处理：这张图找不到纸张边界，直接增强原图（%s）", page.name
            )
    except Exception as exc:  # noqa: BLE001 —— 摆正失败绝不阻断上传
        logger.warning("预处理失败，直接增强原图（%s）：%s", page.name, exc)
        candidate.unlink(missing_ok=True)

    try:
        save_gray(enhanced_from, candidate)
    except Exception as exc:  # noqa: BLE001 —— 增强失败也只是退回原图
        logger.warning("增强失败，按原图入库（%s）：%s", page.name, exc)
        candidate.unlink(missing_ok=True)
        return None
    return candidate


# ---------------------------------------------------------------- 裁到纸（第二步 a）

# 上传时的裁切结论记在图旁边：`<uuid>.crop.json`。配方里的「裁切」那一步直接念
# 这句话——此时图已经裁过了，重算一次只会得到"纸已满幅"，家长看到的就是假话。
CROP_RECORD_SUFFIX = ".crop.json"

# 裁好的那张：与原图**同目录**，文件名多一段 `.crop`，扩展名固定 `.png`
# （`<uuid>.jpg` → `<uuid>.crop.png`）。我们自己产生的图一律无损；老卷子那份
# `<uuid>.crop.jpg` 还在盘上，读图时先认 PNG、再认它。上传的那份原图一个字节都不动。
CROP_FILE_MARK = ".crop"

# 裁后图的后缀（新写的）与老卷子的后缀（只读，不再产出）
CROP_IMAGE_SUFFIX = ".png"
CROP_LEGACY_SUFFIX = ".jpg"

# 裁完（或只转正）之后另存图片用的 JPEG 质量：题块要能看清手写小字，与题块裁图同档。
# 现在只给老路径兜底用——新图落 PNG，不经过这条。
CROP_JPEG_QUALITY = 92

# 家长自己拖的四角，及格线：面积占原图 ≥5%（别把一行字当纸）、
# 最短边 ≥ 原图长边的 10%（别退化成一条线）。
MIN_HUMAN_AREA_RATIO = 0.05
MIN_HUMAN_SIDE_RATIO = 0.10

# 闸门原因 → 给家长的一句话（任务卡里那八句，一字不改）。`not_found` 与
# `cannot_separate_background` 共用一句：都意味着"这张图分不出纸"，办法也一样。
_CROP_SKIP_NOTES = {
    "already_full_page": "跳过（纸已满幅）",
    "cannot_separate_background": "跳过（纸和背景分不开，请换深色背景或自己框）",
    "not_found": "跳过（纸和背景分不开，请换深色背景或自己框）",
    "paper_too_small": "跳过（纸太小，找不到整页）",
    "degenerate_mask": "跳过（检出的不是四角）",
    "paper_cut_by_frame": "跳过（纸被画面截断，请重拍或自己框）",
    "weak_edge": "跳过（纸边不清楚）",
}


def crop_note(
    result: PaperResult,
    from_size: tuple[int, int] | None = None,
    to_size: tuple[int, int] | None = None,
) -> str:
    """`PaperResult` → 给家长的一句话（不带步骤名，步骤名由配方那一步加）。

    裁得动才报"完成"，并带上纸占画面多少、裁前裁后多大；其余一律"跳过 + 原因"。
    """
    if result.can_auto_crop and from_size is not None and to_size is not None:
        return (
            f"完成（纸占 {round(result.area_ratio * 100)}%，"
            f"从 {int(from_size[0])}x{int(from_size[1])} "
            f"裁到 {int(to_size[0])}x{int(to_size[1])}）"
        )
    return _CROP_SKIP_NOTES.get(
        result.reason, _CROP_SKIP_NOTES["cannot_separate_background"]
    )


def _read_bgr(path: Path):
    """读图 → OpenCV 的 BGR 数组（不缩放、不转正）。"""
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        image.load()
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        return cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)


def _rotate(bgr, angle: int):
    """按 `ocr.detect_orientation` 的角度摆正（90° 的倍数走 transpose，不插值）。"""
    import cv2

    if angle == 90:
        return cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if angle == 180:
        return cv2.rotate(bgr, cv2.ROTATE_180)
    if angle == 270:
        return cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
    return bgr


def _detect_and_warp(bgr: "np.ndarray") -> tuple[PaperResult, "np.ndarray | None"]:
    """`detect_paper` → 闸门放行才 `warp_page`；返回 (Detection, 裁好的图或 None)。"""
    from cairn.ocr.crop import detect_paper, warp_page

    result = detect_paper(bgr)
    if not result.can_auto_crop or result.corners is None:
        return result, None
    return result, warp_page(bgr, result.corners)


def _encode_page(bgr, suffix: str) -> bytes:
    """数组 → 图片字节：`.png` 用无损 PNG，其余按 JPEG 92。

    先编码成功再落盘，任何一步失败都不留半张图。
    """
    import cv2

    if suffix.lower() == ".png":
        ok, buffer = cv2.imencode(".png", bgr)
        if not ok:
            raise ValueError("PNG 编码失败")
        return buffer.tobytes()
    ok, buffer = cv2.imencode(
        ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), CROP_JPEG_QUALITY]
    )
    if not ok:
        raise ValueError("JPEG 编码失败")
    return buffer.tobytes()


def _write_page(bgr, target: Path) -> None:
    """把数组写成 `target`（后缀说了算：`.png` 无损，其余 JPEG）。

    `target` 一定是**新文件**（`<uuid>.crop.png` 或临时文件）：上传的原图只读，
    任何情况下都不许写回去。
    """
    target.write_bytes(_encode_page(bgr, target.suffix))


def _crop_output_path(path: Path) -> Path:
    """图 → 它裁好的那一份（`<uuid>.jpg` → `<uuid>.crop.png`，固定 PNG）。"""
    return path.with_name(f"{path.stem}{CROP_FILE_MARK}{CROP_IMAGE_SUFFIX}")


def _legacy_crop_path(path: Path) -> Path:
    """老卷子那份裁后图（`<uuid>.jpg` → `<uuid>.crop.jpg`）：只读，不再产出。"""
    return path.with_name(f"{path.stem}{CROP_FILE_MARK}{CROP_LEGACY_SUFFIX}")


def _crop_record_path(path: Path) -> Path:
    """图 → 它旁边那条裁切结论（`<uuid>.jpg` → `<uuid>.crop.json`）。"""
    return path.with_suffix(CROP_RECORD_SUFFIX)


def _corners_json(result: PaperResult) -> list[list[int]] | None:
    """`PaperResult.corners` → 可下发给前端的四点（**原图坐标**，整数）。

    检测出来的四角本来就在整张原图的像素坐标里（`detect_paper` 不缩放），这里只做
    取整；不是 `warp_page` 之后那张裁好图的坐标——前端拿它去叠框，用的是原图。
    没检出纸（`not_found`）时是 null。
    """
    if result.corners is None:
        return None
    return [
        [int(round(float(point[0]))), int(round(float(point[1])))]
        for point in result.corners
    ]


def _write_record_json(path: Path, payload: dict) -> Path:
    """把旁挂 json 原子地写下去：先写同目录临时文件，再 `os.replace` 换名。

    读的人（配方那一步、`crop-info` 接口）永远看不到半条 JSON；写一半失败也不会
    把原来那条毁掉。
    """
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f"{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def _write_crop_record(
    target: Path, result: PaperResult, note: str, size: tuple[int, int]
) -> Path:
    """把这条裁切结论写进 `<uuid>.crop.json`（原图旁边，一个 uuid 一条）。

    `size` 是**原图**的 (宽, 高)：前端要按原图尺寸铺四角，`crop.png` 是裁完再摆正的
    另一套尺寸，拿它算会整体错位。

    四角写在两处：`corners`（P2-a 的旧键，不动）与 `detected_corners`（新名字），
    兼容期同值。家长自己拖过的结论由 `apply_human_corners` 往同一条记录上追加
    `human` / `human_corners`，这里不写——没调过那个接口的卷子没有这两个键。

    三个亮度字段（D2b 的数据条）：`paper_level` / `edge_level` 取整成 int，
    `background_delta` **用取整后的两个数相减**，不是把 float 差值再取整——
    卡上要的是 `delta == paper_level - edge_level` 这条相等断言，分开取整
    会让它在 .5 附近差 1。
    """
    width, height = size
    corners = _corners_json(result)
    paper_level = int(round(float(result.paper_level)))
    edge_level = int(round(float(result.edge_level)))
    return _write_record_json(
        _crop_record_path(target),
        {
            "note": note,
            "reason": result.reason,
            "area_ratio": round(float(result.area_ratio), 4),
            "corners": corners,
            "width": int(width),
            "height": int(height),
            "detected_corners": corners,
            "paper_level": paper_level,
            "edge_level": edge_level,
            "background_delta": paper_level - edge_level,
        },
    )


def _crop_page(target: Path) -> tuple[Path | None, Path | None]:
    """原图 → 裁到纸：detect_paper → 闸门 → 裁或原样 → 方向归正，结论记在图旁边。

    **用原图裁、往新文件写**：增强图是后面 `_write_processed` 才产出的，这里读的
    就是刚落盘的原图；`target` 一个字节不改。裁得动就 `warp_page` 写到
    `<uuid>.crop.png`；裁不动但方向不正要转，也只把转正结果写到那张新文件上；
    既不裁也不转，就不出这份文件（读图回落原图/增强图）。

    判定放在**转正之前**：第一步八条闸门的阈值是在原图上标定的（`hi-redbg.jpg`
    原图 43% 判 ok，转成横图后缩到 700x300 反而判成"不是四角"）。裁完再转 90°，
    和第一步"裁完的图给 `read_page` 判方向"是同一条路，得到的页图一模一样。

    工具关掉、cv2 装不上、任何异常都只记一行 warning 并按原样入库——裁边是锦上添花，
    不许把上传拖下水。返回 (结论文件, 裁好的图)，没写成的那项是 None；
    入库失败时调用方按这两个返回值一起删。
    """
    if not tools.is_enabled("crop"):
        logger.info("裁边已关闭，整张原样保留（%s）", target.name)
        return None, None
    try:
        angle = detect_page_angle(target)
        bgr = _read_bgr(target)
        height, width = bgr.shape[:2]
        result, page = _detect_and_warp(bgr)
        crop_file: Path | None = None
        if page is None:
            note = crop_note(result)
            if angle:  # 方向归正要落盘，即使这一页不裁
                crop_file = _crop_output_path(target)
                _write_page(_rotate(bgr, angle), crop_file)
        else:
            stored = _rotate(page, angle)
            crop_file = _crop_output_path(target)
            _write_page(stored, crop_file)
            note = crop_note(
                result, (width, height), (stored.shape[1], stored.shape[0])
            )
        logger.info("裁边：%s（%s）", note, target.name)
        return _write_crop_record(target, result, note, (width, height)), crop_file
    except Exception as exc:  # noqa: BLE001 —— 裁边失败绝不挡上传
        logger.warning("裁边失败，按原样入库（%s）：%s", target.name, exc)
        return None, None


def _read_crop_record(scan: Scan, db_url: str) -> dict | None:
    """图旁边那条裁切结论（原始 JSON）；没有记录（老照片）或读不动都是 None。"""
    try:
        record = _crop_record_path(image_path(scan, db_url))
    except ValueError:
        return None
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def crop_note_for(scan: Scan, db_url: str) -> str | None:
    """这张照片上传时记下的那句话；没有记录（老照片）返回 None。"""
    payload = _read_crop_record(scan, db_url)
    note = payload.get("note") if payload is not None else None
    return note if isinstance(note, str) and note else None


def crop_record_for(scan: Scan, db_url: str) -> dict | None:
    """这张照片上传时记下的整条裁切结论，给只读接口用；老照片没有就是 None。

    **只念已经落盘的事实，不重跑检测**：四角记的是上传时那张原图的坐标，现在再判
    一次只会得到别的东西（裁好的那张再判必然是"纸已满幅"），前端叠框会整体错位。
    老卷子的 sidecar 只有三字段，缺的 corners/width/height 按"不知道"给 None。
    """
    payload = _read_crop_record(scan, db_url)
    if payload is None:
        return None
    corners = payload.get("corners")
    area_ratio = payload.get("area_ratio")
    width, height = payload.get("width"), payload.get("height")
    return {
        "reason": payload.get("reason") if isinstance(payload.get("reason"), str) else None,
        "note": payload.get("note") if isinstance(payload.get("note"), str) else None,
        "area_ratio": (
            float(area_ratio)
            if isinstance(area_ratio, (int, float)) and not isinstance(area_ratio, bool)
            else None
        ),
        "corners": (
            corners
            if isinstance(corners, list)
            and len(corners) == 4
            and all(isinstance(point, list) and len(point) == 2 for point in corners)
            else None
        ),
        "width": width if isinstance(width, int) else None,
        "height": height if isinstance(height, int) else None,
        "paper_level": _int_or_none(payload.get("paper_level")),
        "edge_level": _int_or_none(payload.get("edge_level")),
        "background_delta": _int_or_none(payload.get("background_delta")),
    }


def _int_or_none(value: object) -> int | None:
    """sidecar 里的 int 字段：不是 int（含 bool、字符串、缺失）一律 None。

    老卷子（D2b 之前写的 sidecar）没有这三个键，页面上的数据条按"不知道"显示，
    不拿 0 冒充真实读数。
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ------------------------------------------------- 家长改四角 → 从原图重裁


class CropInputError(ValueError):
    """家长给的四角不合格：接口回 400，**一个文件都不许动**。

    单独一个异常类型，是因为 web 那边要按 400（家长能改）和 404（这张照片根本
    不在）分开说话；只靠 ValueError 分不出来。
    """


def _parse_corners(raw: object) -> list[list[float]]:
    """请求体里的四角 → 四个 [x, y]；形状不对就 400（不猜、不补）。"""
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        raise CropInputError("四角必须是 4 个点")
    points: list[list[float]] = []
    for point in raw:
        if not isinstance(point, list | tuple) or len(point) != 2:
            raise CropInputError("每个点必须是两个数字")
        values: list[float] = []
        for value in point:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise CropInputError("每个点必须是两个数字")
            number = float(value)
            if not math.isfinite(number):
                raise CropInputError("每个点必须是两个数字")
            values.append(number)
        points.append(values)
    return points


def _quad_area(quad) -> float:
    """四个点的面积（鞋带公式；顶点顺序由 `order_quad` 保证，取绝对值更省心）。"""
    xs, ys = quad[:, 0], quad[:, 1]
    return 0.5 * abs(
        sum(
            float(xs[i] * ys[(i + 1) % 4] - xs[(i + 1) % 4] * ys[i])
            for i in range(4)
        )
    )


def _check_quad(points: list[list[float]], size: tuple[int, int]):
    """四角 → 摆正顺序的四边形；不合格抛 `CropInputError`。

    五条，全在写盘之前：4 点（`_parse_corners` 已经管了）、每个点在图内（越界
    **不夹**——夹到边界等于替家长做了一个他没做的决定）、凸、面积 ≥5%、最短边
    ≥ 原图长边的 10%（后两条防"把一行字当纸"和"退化成一条线"）。
    """
    import numpy as np

    from cairn.ocr.crop import order_quad  # 与 warp_page 内部同一套排序

    width, height = size
    for x, y in points:
        if not (0 <= x <= width) or not (0 <= y <= height):
            raise CropInputError("四角必须落在原图范围内")
    quad = order_quad(np.asarray(points, dtype=np.float32))
    turns = []
    for index in range(4):
        edge = quad[index] - quad[(index + 1) % 4]
        following = quad[(index + 1) % 4] - quad[(index + 2) % 4]
        turns.append(float(edge[0] * following[1] - edge[1] * following[0]))
    if any(turn == 0 for turn in turns) or not (
        all(turn > 0 for turn in turns) or all(turn < 0 for turn in turns)
    ):
        raise CropInputError("四角必须围成一个凸四边形")
    area = _quad_area(quad)
    if area < MIN_HUMAN_AREA_RATIO * width * height:
        raise CropInputError(
            f"框太小了（不足原图的 {round(MIN_HUMAN_AREA_RATIO * 100)}%）"
        )
    sides = [float(np.linalg.norm(quad[i] - quad[(i + 1) % 4])) for i in range(4)]
    if min(sides) < MIN_HUMAN_SIDE_RATIO * max(width, height):
        raise CropInputError(
            f"框太窄了（最短边不到原图长边的 {round(MIN_HUMAN_SIDE_RATIO * 100)}%）"
        )
    return quad


def apply_human_corners(scan: Scan, db_url: str, raw: object) -> dict:
    """家长拖过的四角 → **从原图**重裁一张，写 `<uuid>.crop.png`，结论追加进 sidecar。

    永远读原图（`scan.path` 那一份），不读上一次的裁后图：从裁过的图再裁一次，
    边缘会被重采样两遍，而且"重裁"的语义就是按原图像素重新框。原图只读，字节与
    上传时一致（AC3 有断言）。

    顺序是"全部算完再落盘"：校验、warp、PNG 编码都在内存里做完，任何一步失败都
    不会留下半张图或半条 json（AC4 用 mtime + sha256 断）。
    """
    from cairn.ocr.crop import warp_page

    original = image_path(scan, db_url)  # 越出库目录 → ValueError（web 转 404）
    points = _parse_corners(raw)
    bgr = _read_bgr(original)
    height, width = bgr.shape[:2]
    quad = _check_quad(points, (width, height))
    page = warp_page(bgr, quad)
    claim = _encode_page(page, CROP_IMAGE_SUFFIX)
    crop_file = _crop_output_path(original)
    crop_file.write_bytes(claim)
    payload = _read_crop_record(scan, db_url) or {}
    payload.setdefault("detected_corners", payload.get("corners"))
    payload["human"] = "adjusted"
    payload["human_corners"] = points
    _write_record_json(_crop_record_path(original), payload)
    area = _quad_area(quad)
    logger.info(
        "按家长的四角重裁：%s（%dx%d，纸占 %d%%）",
        original.name,
        page.shape[1],
        page.shape[0],
        round(area / (width * height) * 100),
    )
    return {
        "human": "adjusted",
        "crop_file": crop_file,
        "width": int(page.shape[1]),
        "height": int(page.shape[0]),
        "area_ratio": round(area / (width * height), 4),
    }


def mark_crop_skipped(scan: Scan, db_url: str) -> dict:
    """家长说"就这样"：不重裁、不动图，只把 sidecar 标成 `human="skipped"`。

    老卷子可能根本没有 sidecar：这时候只落新字段（`human` / `human_corners` /
    `detected_corners`），不编造 note / reason——那三个旧字段是上传时的事实，
    没有就是没有。
    """
    original = image_path(scan, db_url)
    payload = _read_crop_record(scan, db_url) or {}
    payload.setdefault("detected_corners", payload.get("corners"))
    payload["human"] = "skipped"
    payload["human_corners"] = None
    _write_record_json(_crop_record_path(original), payload)
    current = _existing_crop(original)
    size = image_size(current or original) or (0, 0)
    return {
        "human": "skipped",
        "crop_file": current,
        "width": int(size[0]),
        "height": int(size[1]),
        "area_ratio": payload.get("area_ratio"),
    }


def crop_page_now(path: str | Path) -> tuple[str, Path | None]:
    """没有上传记录的图（老照片 / 直接喂一张样本）：现判一次，能裁就裁。

    返回 (给家长的一句话, 裁好的临时文件或 None)。临时文件放系统临时目录：
    这类图没有 scan 行，裁完的图没有正式归宿，也不该往仓库的样本目录里写东西。
    失败一律按"跳过 + 纸和背景分不开"处理，不许把配方这一步抛崩。
    """
    source = Path(path)
    try:
        angle = detect_page_angle(source)
        bgr = _read_bgr(source)
        height, width = bgr.shape[:2]
        result, page = _detect_and_warp(bgr)
    except Exception as exc:  # noqa: BLE001 —— 判不了就照实说"分不开"
        logger.warning("裁边：这张图现判失败，按不裁处理（%s）：%s", source.name, exc)
        return _CROP_SKIP_NOTES["cannot_separate_background"], None
    if page is None:
        return crop_note(result), None
    stored = _rotate(page, angle)
    handle = tempfile.NamedTemporaryFile(
        prefix="cairn-crop-", suffix=".jpg", delete=False
    )
    handle.close()
    cropped = Path(handle.name)
    _write_page(stored, cropped)
    return (
        crop_note(result, (width, height), (stored.shape[1], stored.shape[0])),
        cropped,
    )


def confirm_scan(
    session: Session,
    learner_id: int,
    scan: Scan,
    *,
    subject: str,
    question_type: str,
    content: str,
    answer: str,
) -> tuple[Question, bool]:
    """人工确认：一张图只建一次题。

    走 entry_service.create_error_item 的既有创建路径（校验 + 词典补全 +
    Question/ErrorItem 落库），不自写 SQL。scan.question_id 已指向有效题时
    直接复用——同一张图确认两次不重复建题；第二次提交的字段被忽略。
    返回 (题目, 是否新建)。
    """
    if scan.question_id is not None:
        existing = session.get(Question, scan.question_id)
        if existing is not None:
            return existing, False
    payload = entry_service.EntryPayload(
        subject=subject,
        question_type=question_type,
        content=content,
        answer=answer,
    )
    item = entry_service.create_error_item(session, learner_id, payload)
    scan.question_id = item.question_id
    session.commit()
    return session.get(Question, item.question_id), True


def db_dir(db_url: str) -> Path:
    """库文件所在目录；内存库/非 SQLite 回落当前工作目录。"""
    url = make_url(db_url)
    database = url.database
    if url.drivername.startswith("sqlite") and database and database != ":memory:":
        return Path(database).expanduser().resolve().parent
    return Path.cwd().resolve()


def scans_root(db_url: str) -> Path:
    return db_dir(db_url) / SCANS_DIRNAME


@dataclass(frozen=True)
class DeviceView:
    """一台设备 + 它的能力；单台读取失败只影响这一行，不拖垮整页。"""

    device: DeviceInfo
    capabilities: Capabilities | None = None
    error: str | None = None


def build_device_views(backend: CameraBackend) -> list[DeviceView]:
    """逐台 open → capabilities → close（V4L2 的「先问再设」）。"""
    views: list[DeviceView] = []
    for device in backend.enumerate_devices():
        try:
            backend.open(device.stable_id)
            capabilities = backend.capabilities()
        except CaptureError as exc:
            views.append(DeviceView(device=device, error=str(exc)))
        else:
            views.append(DeviceView(device=device, capabilities=capabilities))
        finally:
            backend.close()
    return views


def grab_frame(backend: CameraBackend, device_id: str) -> Frame:
    """Capture before opening a database write transaction."""
    backend.open(device_id)
    try:
        return backend.grab()
    finally:
        backend.close()


def capture_snapshot(
    backend: CameraBackend,
    device_id: str,
    session: Session,
    learner_id: int,
    *,
    db_url: str,
    note: str | None = None,
    start_workflow: bool = False,
    workflow_version_id: str | None = None,
) -> Scan:
    """抓一帧并存盘入库。抓帧失败不会留下文件或行。"""
    frame = grab_frame(backend, device_id)
    return store_frame(session, frame, learner_id, db_url=db_url, note=note,
                       start_workflow=start_workflow, workflow_version_id=workflow_version_id)


def store_frame(
    session: Session,
    frame: Frame,
    learner_id: int,
    *,
    db_url: str,
    note: str | None = None,
    start_workflow: bool = False,
    workflow_version_id: str | None = None,
) -> Scan:
    """落盘 + 入库，照片记在当前孩子名下（写入必填，不允许无主照片）。"""
    root = db_dir(db_url)
    # 目录按本地时间分，方便家长在 Finder 里按年月翻；库里的 captured_at 仍是 UTC。
    stamp = frame.captured_at.astimezone()
    folder = root / SCANS_DIRNAME / f"{stamp:%Y}" / f"{stamp:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{uuid.uuid4().hex}{frame.suffix}"
    target.write_bytes(frame.data)

    scan = Scan(
        learner_id=learner_id,
        device_id=frame.device.stable_id,
        device_name=frame.device.name,
        width=frame.width,
        height=frame.height,
        captured_at=frame.captured_at,
        path=target.relative_to(root).as_posix(),
        note=note if note is not None else frame.note,
        workflow_version_id=workflow_version_id or active_version_id(session),
    )
    session.add(scan)
    try:
        if start_workflow:
            from cairn.workflow import execution

            session.flush()
            execution.start(session, scan)
        session.commit()
    except Exception:
        session.rollback()
        target.unlink(missing_ok=True)
        raise
    return scan


def list_scans(session: Session, learner_id: int) -> list[Scan]:
    """这个孩子的照片，新的在前；同一秒内按 id 兜底，保证顺序稳定。"""
    return list(
        session.scalars(
            select(Scan)
            .where(Scan.learner_id == learner_id)
            .order_by(Scan.captured_at.desc(), Scan.id.desc())
        ).all()
    )


def get_scan(session: Session, learner_id: int, scan_id: int) -> Scan | None:
    """本孩子的单张照片；别的孩子的 id 一律当作不存在。"""
    scan = session.get(Scan, scan_id)
    if scan is None or scan.learner_id != learner_id:
        return None
    return scan


# ------------------------------------------------- 这张卷子走到哪一步（卡 W1）

# 一张卷子在整条路上只有三个状态。页面只读结果，不自己判：
# 判据散在模板里就会长出第三套口径，家长看到的按钮和后端以为的状态迟早对不上。
STAGE_CROP = "crop"
STAGE_SELECT = "select"
STAGE_READY = "ready"
STAGE_REVIEW = "review"
STAGE_PROCESSING = "processing"
STAGE_FAILED = "failed"


def stored_question_count(session: Session, scan: Scan) -> int:
    """这张卷子已经入库的题数（按 question.id 去重）。

    入库有两条路，都要数：圈选页勾选的行记在 `scan_question.question_id`，
    详情页那个"人工确认入库"表单只回填 `scan.question_id`（没有切题行）。
    两条路合并去重——同一道题走两条路也只算一道；只数前一条的话，从详情页
    确认过的卷子会一直显示"待选题"。
    """
    linked = set(
        session.scalars(
            select(ScanQuestion.question_id).where(
                ScanQuestion.scan_id == scan.id,
                ScanQuestion.question_id.is_not(None),
            )
        ).all()
    )
    if scan.question_id is not None:
        linked.add(scan.question_id)
    return len(linked)


def stage_for(session: Session, scan: Scan, db_url: str) -> str:
    """这张卷子（已经在库里）走到哪一步；三个值之一，没有第四种。

    - `crop`：旁挂 json 不存在，或 `reason != "ok"` 且 `human` 不是 `adjusted`。
      家长拖过四角（`human == "adjusted"`）就算过，**即使机器那次判的是"没裁成"**：
      人工调整过就往下走，不再回头裁（`human == "skipped"` 不算，只是"就这样"）。
    - `ready`：已经入库 ≥1 道题。
    - `select`：裁好了但一道都没入库。

    顺序有意义：先看裁切，再看题数。反过来会让"没裁就入库"的卷子跳过裁边。
    """
    from cairn.models.workflow_run import WorkflowRun, WorkflowRunStep

    run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
    if run is not None:
        if run.status == "completed":
            return STAGE_READY
        if run.status == "failed":
            return STAGE_FAILED
        if run.status == "waiting":
            node = session.scalar(select(WorkflowRunStep.node_id).where(
                WorkflowRunStep.run_id == run.id, WorkflowRunStep.step_index == run.cursor
            ))
            return {"crop": STAGE_CROP, "select": STAGE_SELECT,
                    "review": STAGE_REVIEW}.get(node, STAGE_PROCESSING)
        return STAGE_PROCESSING
    record = _read_crop_record(scan, db_url)
    reason = record.get("reason") if record is not None else None
    human = record.get("human") if record is not None else None
    if record is None or (reason != "ok" and human != "adjusted"):
        return STAGE_CROP
    return STAGE_READY if stored_question_count(session, scan) else STAGE_SELECT


def stage(
    session: Session, learner_id: int, scan_id: int, db_url: str
) -> str | None:
    """`scan_id` 这张卷子走到哪一步；卷子不存在（或不是这个孩子的）返回 `None`。

    卡面写的是 `stage(scan_id)`，但判定要读 sidecar（要 `db_url`）和题数（要
    `session`），所以这四样一起进来。**不拿 `"crop"` 兜底**：调用方拿到 `None`
    就该给 404，编一个状态只会让页面替一张不存在的卷子出主意。
    """
    scan = get_scan(session, learner_id, scan_id)
    if scan is None:
        return None
    return stage_for(session, scan, db_url)


def image_path(scan: Scan, db_url: str) -> Path:
    """把相对路径解析回绝对路径；越出库目录一律拒绝（库里被改脏也不放飞）。"""
    root = db_dir(db_url)
    candidate = (root / scan.path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"scan#{scan.id} 的路径越出库目录：{scan.path}")
    return candidate


def processed_image_path(scan: Scan, db_url: str) -> Path | None:
    """预处理图的绝对路径；没记、文件不在、或越出库目录都是 None。

    越界不抛异常（与 `image_path` 不同）：调用方是"优先用 processed"的读图
    链路，这里坏掉只该悄悄退回原图，不该让页面 500。
    """
    if not scan.processed_path:
        return None
    root = db_dir(db_url)
    candidate = (root / scan.processed_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _existing_crop(original: Path) -> Path | None:
    """原图旁边那张裁好的图；没有（没裁、工具关了、老照片）就是 None。

    先认新卷子的 `<uuid>.crop.png`，再认老卷子的 `<uuid>.crop.jpg`：老卷子那份
    不会再被改写，但也不能因为换了后缀就假装它没有图。
    """
    for crop in (_crop_output_path(original), _legacy_crop_path(original)):
        if crop.is_file():
            return crop
    return None


def existing_crop_path(scan: Scan, db_url: str) -> Path | None:
    """这张照片裁好的那张（没有就是 None）；只读，**不重裁**。"""
    try:
        original = image_path(scan, db_url)
    except ValueError:
        return None
    return _existing_crop(original)


def crop_path_for_scan(scan: Scan, db_url: str) -> Path:
    """这张照片**裁好的那一张**的绝对路径；没裁过就回落原图。

    裁好的图不记库：名字由原图推出来（`<uuid>.jpg` → `<uuid>.crop.png`，老卷子
    是 `.crop.jpg`），文件在就算有。上传那份原图永远不动，`scan.path` 始终指向它。
    """
    original = image_path(scan, db_url)
    return _existing_crop(original) or original


def read_image_path(scan: Scan, db_url: str) -> Path:
    """切题/OCR/展示统一用这张图：**裁好的 → 预处理图 → 原图**。

    为什么统一：`bbox_json.page` 记的是切题时那张图的宽高。切题、叠框展示必须
    是同一张图，否则框整体错位；这三张都是"更好认"的同一页，尺寸各不相同，所以
    不能一半用这个一半用那个。裁好的那张优先：它已经去掉了桌面，纸占满画面；
    processed 是从它（没有裁过就从原图）增强出来的灰度版，只在前者缺失时顶上。
    """
    original = image_path(scan, db_url)
    return _existing_crop(original) or processed_image_path(scan, db_url) or original


def image_size(path: str | Path) -> tuple[int, int] | None:
    """只读文件头拿 (宽, 高)；读不出来返回 None（老图/坏图不该让页面 500）。"""
    from PIL import Image  # 按需导入：不展示的路径不拖图像依赖

    try:
        with Image.open(path) as img:
            width, height = img.size
    except Exception:  # noqa: BLE001 —— 量不出来就交给调用方用库里的尺寸兜
        return None
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def local_time(value: datetime) -> datetime:
    """SQLite 取回的 datetime 会丢时区；按 UTC 解释再转本地（写入时就是 UTC）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone()


def media_type_for(path: str | Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


# ---------------------------------------------------------------- 切题 → 标错入库（v16）

# 题干少于这么多字（去掉空白后）就不许入库：OCR 只认出"3."、"（ ）"这种残片时，
# 建出来的是一道空题，家长在练习里根本认不出是哪道。宁可让他人工补录。
MIN_STEM_CHARS = 4

# `entry_service` 要求 answer 非空（必填校验），而扫描入库时系统**没有**答案。
# 用一个明显的占位串过校验，落库后立刻把 answer 清成 NULL——占位串绝不能
# 留在库里被当标准答案判分（判分层对空答案返回"无法自动判定"）。
_PENDING_ANSWER = "（待补录）"


@dataclass(frozen=True)
class ImportResult:
    """一次标错提交的结果：新建了哪些、已入库哪些、题干残缺被拒哪些。"""

    created: tuple[int, ...]  # 本次新建 question 的题号
    existing: tuple[int, ...]  # 之前已入库、本次跳过的题号
    short: tuple[int, ...]  # 题干太短、拒绝入库的题号
    unselected: tuple[int, ...]  # 这张卷子里未被勾选、因此不入库的题号

    @property
    def created_count(self) -> int:
        return len(self.created)


def _bbox_payload(
    bbox: tuple[int, int, int, int], page_width: int, page_height: int, angle: int
) -> str:
    """bbox 落在摆正后的页面上；页面尺寸与旋转角一起存，叠加才不会错位。"""
    return json.dumps(
        {
            "bbox": [int(v) for v in bbox],
            "page": [int(page_width), int(page_height)],
            "angle": int(angle),
        },
        ensure_ascii=False,
    )


def store_questions(
    session: Session,
    scan_id: int,
    blocks: Sequence[QuestionBlock],
    *,
    page_width: int,
    page_height: int,
    angle: int = 0,
    scan: Scan | None = None,
    page_image: Image.Image | None = None,
    db_url: str | None = None,
    commit: bool = True,
) -> list[ScanQuestion]:
    """把一次切题结果落成 `scan_question` 行（幂等：同一 scan 重复切题不重复插）。

    幂等靠两条：读一次已有题号、批内也按题号去重；表上还有
    `(scan_id, no)` 唯一约束兜底。已存在的行**原样保留**——重切一次若 OCR 结果
    有细微差异，不覆盖家长已经标过/入过库的那一行。
    返回该 scan 当前的全部题行（按插入顺序，即卷面阅读顺序）。

    给了 `scan` + `db_url` + `page_image`（**摆正后**的那张页图）时，顺手把每行
    的题块图裁出来（迁移 v17 的 `crop_path`）：已存在的文件跳过、失败只记
    warning，绝不挡落库。三者缺一就不裁——老调用方（测试/CLI）签名不变。
    """
    if not commit and scan is not None and db_url is not None:
        raise ValueError("transactional question storage cannot write crop files")
    if not blocks:
        return list_questions(session, scan_id)
    existing = set(
        session.scalars(
            select(ScanQuestion.no).where(ScanQuestion.scan_id == scan_id)
        ).all()
    )
    created = False
    for block in blocks:
        if block.no in existing:
            continue
        existing.add(block.no)
        session.add(
            ScanQuestion(
                scan_id=scan_id,
                no=block.no,
                bbox_json=_bbox_payload(
                    block.bbox, page_width, page_height, angle
                ),
                stem_text=block.stem_text or "",
                is_wrong=False,
                # 题号是不是推测出来的（迁移 v18）：只做提示，不挡勾选/入库
                inferred_number=bool(block.inferred_number),
            )
        )
        created = True
    if created:
        if commit:
            session.commit()
        else:
            session.flush()
    rows = list_questions(session, scan_id)
    if scan is not None and db_url is not None:
        crop_question_blocks(session, scan, rows, page_image, db_url=db_url)
    return rows


def list_questions(session: Session, scan_id: int) -> list[ScanQuestion]:
    """一张照片切出的所有题，按插入顺序（= 卷面阅读顺序，跨栏也正确）。"""
    return list(
        session.scalars(
            select(ScanQuestion)
            .where(ScanQuestion.scan_id == scan_id)
            .order_by(ScanQuestion.id.asc())
        ).all()
    )


def page_geometry(rows: list[ScanQuestion]) -> tuple[int, int, int] | None:
    """从任意一行的 bbox_json 取 (page_width, page_height, angle)；取不到返回 None。

    老库里只有 bbox 没有页面信息时返回 None，模板退化成不叠加框。
    """
    for row in rows:
        try:
            payload = json.loads(row.bbox_json or "{}")
            width, height = payload["page"]
            return int(width), int(height), int(payload.get("angle") or 0)
        except (ValueError, TypeError, KeyError):
            continue
    return None


def row_bbox(row: ScanQuestion) -> tuple[int, int, int, int] | None:
    """一行存下来的题框；解析不了返回 None（坏数据不炸整页）。"""
    try:
        x0, y0, x1, y1 = json.loads(row.bbox_json or "{}")["bbox"]
    except (ValueError, TypeError, KeyError):
        return None
    return int(x0), int(y0), int(x1), int(y1)


# ---------------------------------------------------------------- 题块裁图（v17）

# 与照片同级的子目录：`scans/yyyy/mm/crops/<scan_id>-<no>.jpg`。
# 放在照片旁边而不是另起一棵树，家长在 Finder 里按年月找照片时，题块图就在边上。
CROPS_DIRNAME = "crops"

# 裁出来的 JPEG 质量：题块要能看清手写和印刷小字，90 足够且体积可控
CROP_JPEG_QUALITY = 90


def crops_dir(scan: Scan, db_url: str) -> Path:
    """题块图目录 = 照片所在目录 + crops/。"""
    return image_path(scan, db_url).parent / CROPS_DIRNAME


def crop_path_for(scan: Scan, no: int, db_url: str) -> Path:
    """第 `no` 题的裁图文件路径（还没写盘也算）。"""
    return crops_dir(scan, db_url) / f"{scan.id}-{no}.jpg"


def crop_image_path(row: ScanQuestion, db_url: str) -> Path:
    """把 `crop_path` 解析回绝对路径；空值/越出库目录一律 ValueError。

    与 `image_path` 同一条护栏：库里被改脏也不让读盘路径跑出库目录。
    """
    if not row.crop_path:
        raise ValueError(f"scan_question#{row.id} 没有裁图")
    root = db_dir(db_url)
    candidate = (root / row.crop_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"scan_question#{row.id} 的裁图路径越出库目录：{row.crop_path}")
    return candidate


def clip_frame(
    bbox: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int] | None:
    """把题框摆正（左上/右下）并夹进页面范围；夹完什么都不剩返回 None。

    自动切题的框和家长手拖的框共用这套夹取：家长在缩放后的画面上拖，坐标
    偶尔会差出一两像素到页外，夹一下比整批退回好。
    """
    x0, y0, x1, y1 = (int(value) for value in bbox)
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    left, right = max(0, left), min(width, right)
    top, bottom = max(0, top), min(height, bottom)
    if right - left <= 0 or bottom - top <= 0:
        return None
    return left, top, right, bottom


def crop_question_image(
    page_image: Image.Image,
    bbox: tuple[int, int, int, int],
    target: Path,
) -> tuple[int, int]:
    """从**摆正后**的页图上按 bbox 裁一块，写成 JPEG，返回 (宽, 高)。

    越界坐标先夹进页面（OCR 框偶尔会比图多出几像素）；整块落在页外则抛
    ValueError——这属于坐标坏掉，由调用方记 warning 跳过。
    """
    from PIL import Image  # 按需导入：不裁图的路径（拍照入库/上传）不拖图像依赖

    width, height = page_image.size
    box = clip_frame(bbox, width, height)
    if box is None:
        raise ValueError(f"题框 {list(bbox)} 整个落在页面 {width}×{height} 之外")
    target.parent.mkdir(parents=True, exist_ok=True)
    crop = page_image.crop(box)
    if crop.mode not in ("RGB", "L"):
        crop = crop.convert("RGB")
    crop.save(target, format="JPEG", quality=CROP_JPEG_QUALITY)
    return crop.size


def crop_question_blocks(
    session: Session,
    scan: Scan,
    rows: Sequence[ScanQuestion],
    page_image: Image.Image | None,
    *,
    db_url: str,
) -> int:
    """把每行的题块图裁出来落盘，并把相对路径回填 `crop_path`；返回写盘个数。

    - **幂等**：目标文件已存在就跳过（不重裁、不覆盖），只补 `crop_path`；
    - **失败不挡入库**：照片不在、坐标越界、PIL 抛异常都只记一行 warning；
    - `page_image` 为 None（图解不开）时不裁，行照旧留着。
    """
    written = 0
    dirty = False
    for row in rows:
        try:
            target = crop_path_for(scan, row.no, db_url)
            relative = target.relative_to(db_dir(db_url))
        except (ValueError, OSError) as exc:
            logger.warning("裁图跳过 scan#%s 第 %s 题：%s", scan.id, row.no, exc)
            continue
        relative_path = relative.as_posix()
        if target.is_file():
            if row.crop_path != relative_path:
                row.crop_path = relative_path
                dirty = True
            continue
        bbox = row_bbox(row)
        try:
            if bbox is None:
                raise ValueError("题框没读出来")
            if page_image is None:
                raise ValueError("摆正后的页图不可用")
            crop_question_image(page_image, bbox, target)
        except Exception as exc:  # noqa: BLE001 —— 裁图失败绝不挡入库
            logger.warning("裁图失败 scan#%s 第 %s 题：%s", scan.id, row.no, exc)
            continue
        row.crop_path = relative_path
        dirty = True
        written += 1
    if dirty:
        session.commit()
    return written


def import_wrong_questions(
    session: Session,
    learner_id: int,
    scan: Scan,
    nos: Sequence[int],
    *,
    subject: str,
    question_type: str,
    commit: bool = True,
) -> ImportResult:
    """把家长勾选的题入库成 `question`（走 `entry_service`，一次一条）。

    规则（与任务卡 AC4–AC6 一一对应）：

    - **只入勾选的**：没勾的行不入库，`is_wrong` 也不动；
    - **已入库不重复建**：`question_id` 非空直接跳过（幂等，重复提交不增题），
      并把它算进 `existing`；
    - **题干残缺不入库**：去掉空白后少于 `MIN_STEM_CHARS` 字就拒，行留在表里
      等人工补录，服务层把题号交给页面提示；
    - 勾选的题无论入没入库，`is_wrong` 都置 True —— 它记的是"家长说这题错了"。

    没有答案：`entry_service` 必填校验需要非空串，落库后把 `answer` 清 NULL、
    `source` 记 'scan'，判分层对空答案会走"无法自动判定"而不是拿占位串判错。
    Each question and its scan row link commit together. This function commits
    each selected question separately, so a retry can identify completed rows.
    """
    rows = {row.no: row for row in list_questions(session, scan.id)}
    selected = {int(no) for no in nos}
    created: list[int] = []
    existing: list[int] = []
    short: list[int] = []
    for no in sorted(selected):
        row = rows.get(no)
        if row is None:
            continue  # 不属于这张卷子的题号：忽略，不凭空建题
        row.is_wrong = True
        if row.question_id is not None:
            existing.append(no)
            continue
        stem = (row.stem_text or "").strip()
        if len(stem) < MIN_STEM_CHARS:
            short.append(no)
            continue
        item = entry_service.create_error_item(
            session,
            learner_id,
            entry_service.EntryPayload(
                subject=subject,
                question_type=question_type,
                content=stem,
                answer=_PENDING_ANSWER,
            ),
            commit=False,
        )
        question = session.get(Question, item.question_id)
        question.answer = None
        question.source = "scan"
        row.question_id = item.question_id
        if commit:
            session.commit()
        else:
            session.flush()
        created.append(no)
    # 收口一次：一条都没新建时也要把 is_wrong 的改动落盘（可能全是 already 或全被拒）
    if commit:
        session.commit()
    else:
        session.flush()
    unselected = tuple(sorted(no for no in rows if no not in selected))
    return ImportResult(
        created=tuple(created), existing=tuple(existing), short=tuple(short),
        unselected=unselected,
    )


# ---------------------------------------------------------------- 家长自己框题（v19 卡）

# 框的短边下限：手一抖就是一个几像素的框，裁出来只有噪点，入到错题本里就是一道
# 看不见的题。30px 在一张 2560px 宽的整页图上约等于一行小字的高度——比它小的框
# 一定框不住一道题。
MANUAL_MIN_FRAME_SIDE = 30


class FrameError(Exception):
    """手动框题没法入库（页图读不出来等）；web 层把它变成页面上的一句人话。"""


@dataclass(frozen=True)
class ManualFrameResult:
    """一次手动框题的结果。"""

    created: tuple[int, ...]  # 本次新建的题号（接在已有最大号之后，连续）
    skipped: tuple[int, ...]  # 判为"这批框之前提交过"的框在提交顺序里的位置（1 起，不是题号）
    blank_text: tuple[int, ...]  # 框内没认出文字、题干存了空串的题号

    @property
    def created_count(self) -> int:
        return len(self.created)


@dataclass(frozen=True)
class UprightPage:
    """摆正后的一页：旋转角、像素尺寸、可直接裁的图、原图路径。

    `image` 为 None 表示图解不开（损坏/非常见格式）：页面照常展示，只是不能裁。
    """

    angle: int
    width: int
    height: int
    image: Image.Image | None
    path: Path


def detect_page_angle(path: str | Path) -> int:
    """现判整页方向；引擎不可用/图读不了就按 0°（不许因为它挡住手动框）。"""
    from cairn import ocr

    try:
        angle, _score = ocr.detect_orientation(str(path))
    except Exception as exc:  # noqa: BLE001 —— 判不了方向只影响"转不转"，不影响能用
        logger.warning("手动框题：这张照片判不了方向，按不转处理（%s）", exc)
        return 0
    return int(angle) if int(angle) in (0, 90, 180, 270) else 0


def _open_upright_image(path: Path, angle: int) -> Image.Image:
    """把原图按方向转过来；angle=0 时返回原图对象本身（不重采样、不重编码）。"""
    from PIL import Image  # 按需导入：不框题的路径不拖图像依赖

    image = Image.open(path)
    image.load()
    if not angle:
        return image
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image.rotate(angle, expand=True, fillcolor="white")


def upright_page(session: Session, scan: Scan, *, db_url: str,
                 fixed_path: Path | None = None) -> UprightPage:
    """取这张照片**摆正后**的一页。

    角度优先读已有切题行记下的方向（零 OCR，与自动切题同一口径）；一行都没有
    ——自动切题失败、正是手动框要救的场子——才现判一次方向。角度为 0 时图就是
    原图对象：`/upright` 端点据此把原始字节原样发出去，不经二次编码。

    底图走 `read_image_path`（裁好的 → 预处理图 → 原图）：手动框与自动切题
    必须切同一张图，不然框坐标和裁图会错位。
    """
    path = fixed_path if fixed_path is not None else read_image_path(scan, db_url)
    geometry = page_geometry(list_questions(session, scan.id))
    angle = 0 if fixed_path is not None else (geometry[2] if geometry is not None else detect_page_angle(path))
    try:
        image: Image.Image | None = _open_upright_image(path, angle)
    except Exception as exc:  # noqa: BLE001 —— 图解不开也要能打开页面（只显示原图）
        logger.warning("手动框题：scan#%s 的图读不出来：%s", scan.id, exc)
        image = None
    if image is not None:
        width, height = image.size
    elif geometry is not None:
        width, height = geometry[0], geometry[1]
    else:
        width, height = scan.width, scan.height
    return UprightPage(
        angle=angle, width=width, height=height, image=image, path=path
    )


def frame_text(path: str | Path) -> str:
    """只识别框内这一小块（绝不重跑整页）；失败或没认出字一律空串。

    拼行沿用整页那条路（先切栏再按 y 聚行、行内按 x 排），否则选项那一行
    会被识别器的返回顺序打乱成 "D C A B"。题干是这道题的"文字版"，做题页
    靠原图看题——识别不出来只意味着少一段文字，不意味着这道题不该入库。
    """
    from cairn import ocr

    try:
        lines = ocr.recognize(str(path))
    except Exception as exc:  # noqa: BLE001 —— 识别失败绝不挡入库
        logger.warning("框内识别失败 %s：%s", path, exc)
        return ""
    return ocr.to_text(ocr.group_rows(lines)).strip()


def _frame_fingerprint(
    bbox: tuple[int, int, int, int], page_width: int, page_height: int
) -> tuple[int, int, int, int]:
    """一块框的坐标指纹：夹进页面、左上/右下摆正后的四个整数。

    判"这批框之前提交过"全靠它——同一块框无论提交几次、在表单里排第几，
    算出来的四元组都一样。**编号不参与指纹**：编号是每次提交时新分配的。
    """
    return clip_frame(bbox, page_width, page_height) or tuple(int(v) for v in bbox)


def store_manual_frames(
    session: Session,
    learner_id: int,
    scan: Scan,
    frames: Sequence[tuple[int, int, int, int]],
    *,
    page_width: int,
    page_height: int,
    angle: int,
    page_image: Image.Image | None,
    db_url: str,
    subject: str = "数学",
    question_type: str = "填空",
    commit: bool = True,
    written_files: list[Path] | None = None,
) -> ManualFrameResult:
    """把家长自己框的 N 块题一次性入库（scan_question + 裁图 + question + error_item）。

    规则：

    - **接着已有行往下编号**：从这张页现有 `scan_question` 的最大 `no` + 1 起，
      给本批新框连续分配。自动切题先落了 `no=1..K`，家长框的就是 `K+1, K+2…`；
      绝不因为「1 号已经存在」就把整批按已有行跳过（那等于静默零插入）。
    - **幂等判据是框坐标指纹，不是编号**：一块框夹进页面后的四元组若与这张页
      已有任何一行（自动切题或之前手框）`bbox` **夹进页面后**的四元组完全相同，
      就认成"这块框之前提交过"，本次不再建题；同一批里重复框两遍也只收一次。
      判据写在这里是因为
      「`no` 已存在」只说明号被占了，分不清是同一块框还是另一块框——拿它当幂等
      键正是零插入的来源。
    - 编号撞车（并发提交抢先占了同一个号）**不静默跳过**：整批回滚并抛
      `FrameError`，页面明说"和另一批框撞上了"，与"之前提交过"区分开。
    - 每题三件套一起落：`scan_question`（`is_wrong=1`、`crop_path`）、`question`
      （`source='scan'`、`answer` 空）、`error_item`；
    - 题干只做**框内局部 OCR**；认不出就存空串，不阻塞入库；
    - **整批一个事务**：任何一步抛异常就回滚，并把本次写下的裁图删掉——不留
      "有行没图"或"有题没行"的半截状态（`entry_service` 每条自己 commit，
      走不了这条，所以这里直接建行）；
    - 页图读不出来直接抛 `FrameError`：裁图是这模式唯一的凭据，没有它不能入。
    """
    if not commit and written_files is None:
        raise ValueError("transactional frame storage requires file cleanup tracking")
    if page_image is None:
        raise FrameError("这张照片读不出来，暂时没法自己框，换张清楚的照片试试")

    rows = list_questions(session, scan.id)
    existing_nos = {row.no for row in rows}
    # 两侧必须用同一把尺子：已存在的框按**原始** bbox 存（可能越界几像素），
    # 新框按 clip 后的指纹算。只拿原始 bbox 比，越界框首交与重交永远对不上，
    # 会被判成新框、重复建题——所以 taken 也走同一套 clip 后四元组。
    taken: set[tuple[int, int, int, int]] = {
        _frame_fingerprint(box, page_width, page_height)
        for row in rows
        if (box := row_bbox(row)) is not None
    }
    next_no = max(existing_nos, default=0) + 1

    planned: list[tuple[int, tuple[int, int, int, int]]] = []
    skipped: list[int] = []
    for position, bbox in enumerate(frames, start=1):
        fingerprint = _frame_fingerprint(bbox, page_width, page_height)
        if fingerprint in taken:  # 这块框之前就提交过（或本批里重了）
            skipped.append(position)
            continue
        taken.add(fingerprint)
        planned.append((next_no, bbox))
        next_no += 1

    created: list[int] = []
    blank: list[int] = []
    written: list[Path] = []
    try:
        for no, bbox in planned:
            target = crop_path_for(scan, no, db_url)
            relative = target.relative_to(db_dir(db_url)).as_posix()
            row = ScanQuestion(
                scan_id=scan.id,
                no=no,
                bbox_json=_bbox_payload(bbox, page_width, page_height, angle),
                stem_text="",
                is_wrong=True,
                crop_path=relative,
            )
            # 先占号再动盘：并发撞号会在这一行 flush 时被唯一约束拦下，
            # 此时还没写任何裁图，也就不会覆盖/删掉别人已有的那张。
            session.add(row)
            session.flush()
            written.append(target)
            crop_question_image(page_image, bbox, target)
            text = frame_text(target)
            row.stem_text = text
            if not text:
                blank.append(no)
            question = Question(
                learner_id=learner_id,
                subject=subject,
                grade_level="primary",
                question_type=question_type,
                content=text,
                answer=None,
                source="scan",
            )
            session.add(question)
            session.flush()
            session.add(
                ErrorItem(
                    learner_id=learner_id,
                    question_id=question.id,
                    wrong_count=1,
                    review_status=REVIEW_STATUS_NEW,
                    last_wrong_at=utcnow(),
                )
            )
            session.flush()
            row.question_id = question.id
            created.append(no)
        if created:
            if commit:
                session.commit()
            else:
                session.flush()
        if not commit and written_files is not None:
            written_files.extend(written)
    except IntegrityError as exc:
        # 唯一约束（scan_id, no）兜住的并发撞号：另一批框在本次读数之后抢先
        # 占了同一个号。这不是"这批框之前提交过"，必须说清楚并整批退回。
        if commit:
            session.rollback()
        for path in written:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # 删不掉也不能盖住原始异常
                pass
        raise FrameError(
            "这次框的题号和另一批刚提交的框撞上了（不是这批框之前提交过），"
            "这次一道都没收。刷新页面、看清卷面再框一次。"
        ) from exc
    except Exception:
        if commit:
            session.rollback()
        for path in written:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # 删不掉也不能盖住原始异常
                pass
        raise
    return ManualFrameResult(
        created=tuple(created), skipped=tuple(skipped), blank_text=tuple(blank)
    )


# ---------------------------------------------------------------- cut feedback (v20)

# Actions the frame editor can report. `adjust` (box moved but not accepted yet)
# is a valid label and counts toward auto_frames, but toward none of the rates.
CUT_FEEDBACK_ACTIONS = frozenset({"accept", "delete", "redraw", "add", "adjust"})

# Below this many automatic frames the rates say nothing (three pages would
# easily read as "100% correct"), so stats reports `insufficient` instead.
MIN_CUT_FEEDBACK_FRAMES = 20


class CutFeedbackError(Exception):
    """Rejected cut-feedback payload (bad action / bbox / frame key); web maps to 400."""


def _number_text(value: float) -> str:
    """Format a coordinate without adding meaningless decimals (10.0 -> "10")."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _bbox_values(raw: object) -> list[float] | None:
    """Validate a JSON bbox without touching its numbers.

    Deliberately no clipping and no aspect-ratio guessing: coordinates that do
    not fit the image are still the label we were given, and rewriting them
    would silently invent data.
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise CutFeedbackError("bbox must be a list of 4 numbers [x1, y1, x2, y2]")
    values: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CutFeedbackError("bbox coordinates must be numbers")
        values.append(float(value))
    return values


def _bbox_text(values: list[float] | None) -> str | None:
    if values is None:
        return None
    return ",".join(_number_text(value) for value in values)


def bbox_from_text(text: str | None) -> list[float] | None:
    """Parse a stored ``x1,y1,x2,y2`` string back into four numbers."""
    if not text:
        return None
    try:
        return [float(part) for part in text.split(",")]
    except ValueError:  # a hand-edited row must not break the whole export
        return None


def bbox_iou(first: list[float], second: list[float]) -> float:
    """IoU of two boxes; corner order does not matter, coordinates are not clipped."""
    ax1, ay1, ax2, ay2 = first
    bx1, by1, bx2, by2 = second
    inter_w = max(0.0, min(max(ax1, ax2), max(bx1, bx2)) - max(min(ax1, ax2), min(bx1, bx2)))
    inter_h = max(0.0, min(max(ay1, ay2), max(by1, by2)) - max(min(ay1, ay2), min(by1, by2)))
    intersection = inter_w * inter_h
    area_a = abs((ax2 - ax1) * (ay2 - ay1))
    area_b = abs((bx2 - bx1) * (by2 - by1))
    union = area_a + area_b - intersection
    if union <= 0:
        return 0.0
    return intersection / union


def _frame_payload(frame: object) -> dict[str, object]:
    """Turn one JSON frame into column values (key check, bbox parsing, IoU)."""
    if not isinstance(frame, dict):
        raise CutFeedbackError("each frame must be a JSON object")
    frame_key = str(frame.get("frame_key") or "").strip()
    if not frame_key:
        raise CutFeedbackError("frame_key is required")
    action = str(frame.get("action") or "").strip()
    if action not in CUT_FEEDBACK_ACTIONS:
        raise CutFeedbackError(f"unknown action: {action or '(empty)'}")
    source = str(frame.get("source") or "").strip() or "manual"
    auto_values = _bbox_values(frame.get("auto_bbox"))
    human_values = _bbox_values(frame.get("human_bbox"))
    raw_no = frame.get("no")
    try:
        question_no = None if raw_no is None else int(raw_no)
    except (TypeError, ValueError) as exc:
        raise CutFeedbackError("no must be an integer or null") from exc
    iou = (
        bbox_iou(auto_values, human_values)
        if auto_values is not None and human_values is not None
        else None
    )
    return {
        "frame_key": frame_key,
        "question_no": question_no,
        "action": action,
        "source": source,
        "auto_bbox": _bbox_text(auto_values),
        "human_bbox": _bbox_text(human_values),
        "iou": iou,
        "selected": bool(frame.get("selected")),
    }


def record_cut_feedback(
    session: Session,
    scan: Scan,
    frames: Sequence[dict],
    *,
    image_sha256: str,
    image_w: int,
    image_h: int,
    recipe: str,
    params_hash: str,
) -> int:
    """Store one whole-page snapshot; returns how many frames were written.

    The page is submitted as a snapshot, so the same ``(scan_id, frame_key)``
    updates the existing row instead of appending another one. Frames repeated
    inside one payload collapse to the last occurrence for the same reason.

    Bboxes are stored verbatim in original-image pixels: no scaling, no clipping,
    no aspect-ratio correction. ``iou`` is the only derived value, and only when
    both the automatic and the human box exist.
    """
    if isinstance(frames, (str, bytes)) or not isinstance(frames, Sequence):
        raise CutFeedbackError("frames must be a list")

    payloads: dict[str, dict[str, object]] = {}
    for frame in frames:
        payload = _frame_payload(frame)
        payloads[str(payload["frame_key"])] = payload

    existing: dict[str, CutFeedback] = {}
    if payloads:
        existing = {
            row.frame_key: row
            for row in session.scalars(
                select(CutFeedback).where(
                    CutFeedback.scan_id == scan.id,
                    CutFeedback.frame_key.in_(list(payloads)),
                )
            )
        }

    timestamp = now_iso()
    for frame_key, payload in payloads.items():
        row = existing.get(frame_key)
        if row is None:
            row = CutFeedback(scan_id=scan.id, frame_key=frame_key)
            session.add(row)
        row.question_no = payload["question_no"]
        row.action = payload["action"]
        row.source = payload["source"]
        row.auto_bbox = payload["auto_bbox"]
        row.human_bbox = payload["human_bbox"]
        row.iou = payload["iou"]
        row.selected = payload["selected"]
        row.image_sha256 = image_sha256
        row.image_w = int(image_w)
        row.image_h = int(image_h)
        row.recipe = recipe
        row.params_hash = params_hash
        row.created_at = timestamp
    session.commit()
    return len(payloads)


def cut_feedback_stats(
    session: Session,
    *,
    since: str | None = None,
    until: str | None = None,
    recipe: str | None = None,
) -> dict[str, object]:
    """Counts + four rates for one time window (see docs/CUT-FEEDBACK-SPEC.md).

    Denominators are fixed by the spec: accept/delete/redraw divide by the number
    of automatic frames, while add divides by ``auto_frames + added`` so it really
    measures "how much the machine missed". With fewer than
    ``MIN_CUT_FEEDBACK_FRAMES`` automatic frames no rate is produced at all.
    """
    stmt = select(CutFeedback)
    if recipe:
        stmt = stmt.where(CutFeedback.recipe == recipe)
    if since:
        stmt = stmt.where(CutFeedback.created_at >= since)
    if until:
        stmt = stmt.where(CutFeedback.created_at < until)
    rows = session.scalars(stmt).all()

    pages = len({row.scan_id for row in rows})
    auto_frames = sum(1 for row in rows if row.auto_bbox)
    accepted = sum(1 for row in rows if row.action == "accept")
    deleted = sum(1 for row in rows if row.action == "delete")
    redrawn = sum(1 for row in rows if row.action == "redraw")
    added = sum(1 for row in rows if row.action == "add")
    ious = [row.iou for row in rows if row.iou is not None]

    stats: dict[str, object] = {
        "pages": pages,
        "auto_frames": auto_frames,
        "accepted": accepted,
        "deleted": deleted,
        "redrawn": redrawn,
        "added": added,
        "mean_iou": sum(ious) / len(ious) if ious else None,
    }
    if auto_frames < MIN_CUT_FEEDBACK_FRAMES:
        stats["insufficient"] = True
        return stats
    stats["accept_rate"] = accepted / auto_frames
    stats["delete_rate"] = deleted / auto_frames
    stats["redraw_rate"] = redrawn / auto_frames
    stats["add_rate"] = added / (auto_frames + added)
    return stats


def _export_bbox(text: str | None) -> list[int | float] | None:
    values = bbox_from_text(text)
    if values is None:
        return None
    return [int(value) if value.is_integer() else value for value in values]


def cut_feedback_records(
    session: Session,
    *,
    since: str | None = None,
    include_image: bool = False,
) -> list[dict[str, object]]:
    """One dict per stored frame, ready to be dumped as JSONL.

    ``include_image`` only adds the original image path (`scan.path`); no image
    bytes are embedded and no copy is made. Whoever exports with images must
    remember that a child's name or school may be printed on the page.
    """
    stmt = (
        select(CutFeedback, Scan.path)
        .join(Scan, Scan.id == CutFeedback.scan_id)
        .order_by(CutFeedback.id)
    )
    if since:
        stmt = stmt.where(CutFeedback.created_at >= since)

    records: list[dict[str, object]] = []
    for row, scan_path in session.execute(stmt):
        record: dict[str, object] = {
            "id": row.id,
            "scan_id": row.scan_id,
            "frame_key": row.frame_key,
            "question_no": row.question_no,
            "action": row.action,
            "source": row.source,
            "auto_bbox": _export_bbox(row.auto_bbox),
            "human_bbox": _export_bbox(row.human_bbox),
            "iou": row.iou,
            "selected": bool(row.selected),
            "image_sha256": row.image_sha256,
            "image_w": row.image_w,
            "image_h": row.image_h,
            "recipe": row.recipe,
            "params_hash": row.params_hash,
            "created_at": row.created_at,
        }
        if include_image:
            record["image_path"] = scan_path
        records.append(record)
    return records
