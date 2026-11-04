"""Scan workflow adapters using the existing image, OCR and learning services."""

from __future__ import annotations

from sqlalchemy import select

from cairn import recipe, scan_service, tools
from cairn.models.scan import ScanQuestion
from cairn.models.workflow_run import WorkflowRun
from cairn.workflow.execution import RunError
from cairn.workflow.images import select_image, select_oriented_image, selected_image
from cairn.workflow.learning import apply_strategy, append_confirmed_to_collection, enable_guided_tutor, enable_text_recap
from cairn.workflow import review as workflow_review
from cairn.workflow.execution import _runtime


def handlers(db_url: str) -> dict:
    """Build handlers for a database; their DB changes belong to the caller."""

    def run_for(session, scan):
        run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
        if run is None:
            raise RunError("照片没有工作流进度")
        return run

    def upright(session, scan, params):
        original = scan_service.image_path(scan, db_url)
        if not original.is_file():
            raise RunError("照片原图不存在")
        if not tools.is_enabled("preprocess"):
            select_image(session, run_for(session, scan), scan, db_url, "original")
            return
        record = scan_service.crop_record_for(scan, db_url)
        if record is None:
            record_path, _ = scan_service._crop_page(original)
            record = scan_service.crop_record_for(scan, db_url) if record_path else None
        source = "crop" if record is not None and scan_service.existing_crop_path(scan, db_url) else "original"
        select_image(session, run_for(session, scan), scan, db_url, source)

    def enhance(session, scan, params):
        tools.require("enhance")
        run = run_for(session, scan)
        source = selected_image(session, scan, db_url)
        if source is None:
            raise RunError("增强前没有选定图片")
        original = scan_service.image_path(scan, db_url)
        output = scan_service._write_processed(original, original.parent, source=source)
        if output is None:
            raise RunError("图片增强未完成")
        scan.processed_path = output.relative_to(scan_service.db_dir(db_url)).as_posix()
        session.flush()
        select_image(session, run, scan, db_url, "enhanced")

    def cut(session, scan, params):
        source = selected_image(session, scan, db_url)
        if source is None:
            raise RunError("切题前没有选定图片")
        result = recipe.run(
            recipe.Recipe("workflow-cut", "切题", (recipe.Step("切题", "cut"),)),
            {"path": source, "scan": scan, "db_url": db_url},
        )
        if result.statuses != [recipe.RAN]:
            raise RunError("自动切题未完成")
        page, blocks = result.data.get("page"), result.data.get("blocks")
        if page is None or blocks is None:
            raise RunError("自动切题没有返回结果")
        if page.angle:
            select_oriented_image(session, run_for(session, scan), scan, db_url, page.image)
        scan_service.store_questions(
            session, scan.id, blocks, page_width=page.width or scan.width,
            page_height=page.height or scan.height, angle=0, commit=False,
        )

    def recognize(session, scan, params):
        # Existing cutting and manual framing already perform OCR and store text.
        found = session.scalar(select(ScanQuestion.id).where(ScanQuestion.scan_id == scan.id).limit(1))
        if found is None:
            raise RunError("没有待确认的题目")
        run = run_for(session, scan)
        if any(step["node_id"] == "review" and step["type"] == "human"
               for step in _runtime(session, run)[run.cursor + 1:]):
            workflow_review.prepare(session, scan, run)

    return {"upright": upright, "enhance": enhance, "cut": cut,
            "recognize": recognize, "strategy": apply_strategy, "guided_tutor": enable_guided_tutor,
            "text_recap": enable_text_recap, "collection_append": append_confirmed_to_collection}
