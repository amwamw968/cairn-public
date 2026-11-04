"""Stable image selection for workflow runs, independent of mutable sidecars."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn import scan_service
from cairn.models.scan import Scan
from cairn.models.workflow_run import WorkflowRun
from cairn.workflow.execution import RunError


def selected_image(session: Session, scan: Scan, db_url: str) -> Path | None:
    """Resolve the current run's verified snapshot, if one has been selected."""
    run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
    if run is None or run.image_path is None:
        return None
    if run.version_id != scan.workflow_version_id or not isinstance(run.image_sha256, str):
        raise RunError("照片快照的工作流版本或摘要无效")
    if not re.fullmatch(r"[0-9a-f]{64}", run.image_sha256):
        raise RunError("照片快照摘要无效")
    root = scan_service.db_dir(db_url).resolve()
    scans_root = scan_service.scans_root(db_url).resolve()
    original = scan_service.image_path(scan, db_url).resolve()
    target = (root / run.image_path).resolve()
    if (not target.is_relative_to(scans_root) or target.parent != original.parent
            or target.name != f"{original.stem}.workflow-{run.image_sha256}{target.suffix}"):
        raise RunError("照片快照路径无效")
    if not target.is_file():
        raise RunError("照片快照不存在")
    with target.open("rb") as file:
        actual_hash = hashlib.file_digest(file, "sha256").hexdigest()
    if actual_hash != run.image_sha256:
        raise RunError("照片快照校验失败")
    return target


def select_image(session: Session, run: WorkflowRun, scan: Scan, db_url: str, source: str) -> Path:
    """Copy an owned scan image into a content-addressed run snapshot.

    The caller commits the DB reference. An interrupted transaction may leave an
    unreferenced snapshot; retry reuses the same bytes instead of replacing them.
    """
    if run.scan_id != scan.id or run.version_id != scan.workflow_version_id:
        raise RunError("照片与固定的工作流版本不一致")
    original = scan_service.image_path(scan, db_url)
    choices = {
        "original": original,
        "crop": scan_service.existing_crop_path(scan, db_url),
        "enhanced": scan_service.processed_image_path(scan, db_url),
    }
    selected = choices.get(source) if isinstance(source, str) else None
    if selected is None or not selected.is_file():
        raise RunError("工作流选择的图片不存在")
    root = scan_service.db_dir(db_url).resolve()
    scans_root = scan_service.scans_root(db_url).resolve()
    selected = selected.resolve()
    if not selected.is_relative_to(scans_root) or selected.parent != original.resolve().parent:
        raise RunError("图片不在照片库内")
    return _persist_snapshot(session, run, root, original, selected, selected.suffix)


def select_oriented_image(session: Session, run: WorkflowRun, scan: Scan, db_url: str, image) -> Path:
    """Freeze the OCR-oriented page so stored boxes match what readers see."""
    from PIL import Image

    if run.scan_id != scan.id or run.version_id != scan.workflow_version_id:
        raise RunError("照片与固定的工作流版本不一致")
    if not isinstance(image, Image.Image):
        raise RunError("无法保存转正后的照片")
    root = scan_service.db_dir(db_url).resolve()
    scans_root = scan_service.scans_root(db_url).resolve()
    original = scan_service.image_path(scan, db_url).resolve()
    if not original.is_relative_to(scans_root):
        raise RunError("图片不在照片库内")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=original.parent, prefix=".oriented-", suffix=".png", delete=False) as temporary:
            temp_path = Path(temporary.name)
            image.save(temporary, format="PNG")
        return _persist_snapshot(session, run, root, original, temp_path, ".png")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _persist_snapshot(session: Session, run: WorkflowRun, root: Path,
                      original: Path, selected: Path, suffix: str) -> Path:
    digest = hashlib.sha256()
    temp_path: Path | None = None
    try:
        with selected.open("rb") as source_file, tempfile.NamedTemporaryFile(
            dir=selected.parent, prefix=".workflow-", suffix=".tmp", delete=False
        ) as temporary:
            temp_path = Path(temporary.name)
            while chunk := source_file.read(1024 * 1024):
                digest.update(chunk)
                temporary.write(chunk)
        image_hash = digest.hexdigest()
        target = original.parent / f"{original.stem}.workflow-{image_hash}{suffix}"
        try:
            os.link(temp_path, target)
        except FileExistsError:
            with target.open("rb") as existing:
                existing_hash = hashlib.file_digest(existing, "sha256").hexdigest()
            if existing_hash != image_hash:
                raise RunError("已保存图片快照校验失败")
        run.image_path = target.relative_to(root).as_posix()
        run.image_sha256 = image_hash
        session.flush()
        return target
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
