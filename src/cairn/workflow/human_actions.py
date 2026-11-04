"""Reconcile completed reader actions with a scan workflow run."""

from sqlalchemy import select

from cairn import scan_service
from cairn.models.question import Question
from cairn.models.workflow_run import WorkflowRun
from cairn.workflow.execution import RunError
from cairn.workflow.images import select_image, selected_image


def crop_confirmed(db_url: str):
    """Snapshot a saved human crop decision; caller resumes the waiting run."""
    def action(session, scan, params):
        record = scan_service._read_crop_record(scan, db_url)
        human = record.get("human") if record else None
        if human not in {"adjusted", "skipped"}:
            raise RunError("尚未确认或跳过裁边")
        crop = scan_service.existing_crop_path(scan, db_url)
        if human == "adjusted" and crop is None:
            raise RunError("人工裁边结果文件不存在")
        run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
        if run is None:
            raise RunError("照片没有工作流进度")
        select_image(session, run, scan, db_url, "crop" if crop else "original")
    return action


def selection_confirmed(db_url: str, auto_numbers: object, manual_frames: object):
    """Require both selection submissions to have persisted before resuming."""
    if (not isinstance(auto_numbers, list) or not isinstance(manual_frames, list)
            or len(auto_numbers) + len(manual_frames) == 0
            or len(auto_numbers) + len(manual_frames) > 200
            or any(type(number) is not int or number < 1 for number in auto_numbers)):
        raise RunError("圈选题目列表无效")
    if any(not isinstance(frame, list) or len(frame) != 4
           or any(type(value) is not int for value in frame) for frame in manual_frames):
        raise RunError("手动画框坐标无效")

    def action(session, scan, params):
        image = selected_image(session, scan, db_url)
        size = scan_service.image_size(image) if image else None
        if size is None:
            raise RunError("流程图片不存在或无法读取")
        width, height = size
        rows = scan_service.list_questions(session, scan.id)
        question_ids = [row.question_id for row in rows if row.question_id is not None]
        owned = set(session.scalars(select(Question.id).where(
            Question.learner_id == scan.learner_id, Question.id.in_(question_ids)
        )))
        stored = [row for row in rows if row.question_id in owned and row.is_wrong
                  and scan_service.page_geometry([row]) == (width, height, 0)]
        stored_numbers = {row.no for row in stored}
        if not set(auto_numbers).issubset(stored_numbers):
            raise RunError("有圈选题目尚未成功入库，请核对题干后重试")
        stored_frames = {
            scan_service._frame_fingerprint(box, width, height)
            for row in stored if (box := scan_service.row_bbox(row)) is not None
        }
        for frame in manual_frames:
            clipped = scan_service.clip_frame(tuple(frame), width, height)
            if clipped is None or clipped not in stored_frames:
                raise RunError("有手动画框尚未成功入库，请重试")
    return action


def submit_selection(db_url: str, auto_numbers: object, manual_frames: object,
                     written_files: list, subject: str, question_type: str):
    """Persist both selection modes within the caller's locked run transaction."""
    verify = selection_confirmed(db_url, auto_numbers, manual_frames)

    def action(session, scan, params):
        from PIL import Image
        from cairn.models.collection import Collection
        from cairn.workflow.execution import _runtime

        run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
        if run is None:
            raise RunError("照片没有工作流进度")
        for step in _runtime(session, run)[run.cursor + 1:]:
            if step["node_id"] == "collection_append":
                collection = session.get(Collection, step["params"].get("collection_id"))
                if collection is None or collection.learner_id != scan.learner_id:
                    raise RunError("目标题集不可用")
                if subject != collection.subject:
                    raise RunError("科目与目标题集不一致，请修改科目")

        image_path = selected_image(session, scan, db_url)
        if image_path is None:
            raise RunError("流程尚未选择照片")
        try:
            with Image.open(image_path) as image:
                image.load()
                page = image.copy()
        except (OSError, ValueError) as exc:
            raise RunError("流程图片无法读取") from exc
        width, height = page.size
        boxes = []
        for frame in manual_frames:
            clipped = scan_service.clip_frame(tuple(frame), width, height)
            if (clipped is None or min(clipped[2] - clipped[0], clipped[3] - clipped[1])
                    < scan_service.MANUAL_MIN_FRAME_SIDE):
                raise RunError("手动画框太小或超出照片")
            boxes.append(clipped)
        result = scan_service.import_wrong_questions(
            session, scan.learner_id, scan, auto_numbers,
            subject=subject, question_type=question_type, commit=False,
        )
        if result.short:
            raise RunError("有题目文字不完整，请取消选择或补充题干")
        try:
            scan_service.store_manual_frames(
                session, scan.learner_id, scan, boxes,
                page_width=width, page_height=height, angle=0, page_image=page,
                db_url=db_url, subject=subject, question_type=question_type,
                commit=False, written_files=written_files,
            )
        except scan_service.FrameError as exc:
            raise RunError(str(exc)) from exc
        verify(session, scan, params)
        # Mark questions as pending before committing the selection, even if a
        # later automatic step fails before the review page is reached.
        from cairn.workflow import execution, review as workflow_review
        run = session.scalar(select(WorkflowRun).where(WorkflowRun.scan_id == scan.id))
        if run is None:
            raise RunError("照片没有工作流进度")
        if any(step["node_id"] == "review" for step in execution._runtime(session, run)[run.cursor + 1:]):
            workflow_review.prepare(session, scan, run)

    return action
