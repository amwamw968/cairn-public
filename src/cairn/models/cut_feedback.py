"""CUT_FEEDBACK — one snapshot of a page: what the machine cut vs what the parent kept.

Spec: `docs/CUT-FEEDBACK-SPEC.md` (migration v20).

Each row is one frame (question block) after the parent finished editing. A page
is submitted as a whole snapshot, so `(scan_id, frame_key)` is unique: sending
the same page twice overwrites the previous snapshot instead of appending.

Coordinates (`auto_bbox` / `human_bbox`) are **original-image pixels**, stored as
the comma-separated text `x1,y1,x2,y2`. They are never scaled, clipped or
otherwise rewritten by this layer — a bbox that does not fit the image is stored
as given, because silently "fixing" it would destroy the very label we collect.
Images themselves are not copied here: the scan row already points at the
original (`scan.path`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base


def now_iso() -> str:
    """UTC ISO8601, second precision — the same text format used for filtering."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CutFeedback(Base):
    __tablename__ = "cut_feedback"
    __table_args__ = (
        # One snapshot per page: a resubmit updates, it never appends.
        UniqueConstraint("scan_id", "frame_key", name="uq_cut_feedback_scan_frame"),
        Index("ix_cut_feedback_scan_id", "scan_id"),
        Index("ix_cut_feedback_created_at", "created_at"),
        # Thresholds change over time; rate queries must be able to isolate the
        # runs that used one particular set of cutting parameters.
        Index("ix_cut_feedback_recipe_params_hash", "recipe", "params_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan.id"), nullable=False)
    # Front-end generated uuid; unique within one page.
    frame_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Which question the frame was judged to be; NULL when the machine had no idea.
    question_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # accept / delete / redraw / add / adjust
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    # Which strategy produced the automatic box:
    # anchor_digit / anchor_cn / gap / model:<ver> / manual
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # "x1,y1,x2,y2" in original-image pixels; NULL for a manual add.
    auto_bbox: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "x1,y1,x2,y2" in original-image pixels; NULL for a pure delete.
    human_bbox: Mapped[str | None] = mapped_column(Text, nullable=True)
    # IoU of auto vs human; only when both boxes exist.
    iou: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Whether the frame entered the question bank.
    selected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # Fingerprint of the original image, used to line up copies of the same page.
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Pixel size of the original image; bbox coordinates live in this space.
    image_w: Mapped[int] = mapped_column(Integer, nullable=False)
    image_h: Mapped[int] = mapped_column(Integer, nullable=False)
    # Recipe name at capture time, e.g. scan-entry.
    recipe: Mapped[str] = mapped_column(String(64), nullable=False)
    # Fingerprint of the cutting-parameter snapshot (so a threshold change shows).
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # ISO8601 UTC text: lexical ordering keeps the (created_at) index usable.
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=now_iso)
