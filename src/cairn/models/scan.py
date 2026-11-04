"""SCAN — 从摄像头/高拍仪拍到的一张图（0.2 卡11）。

只记录「拍了什么、图在哪」，不含任何识别结果：

- `device_id` 是后端的稳定标识（AVFoundation uniqueID / 假设备 id），不是列表下标；
- `path` 存**相对库目录**的路径（`scans/2026/10/<uuid>.jpg`），换机器搬库、改挂载点都
  还能定位；绝对路径写进库反而一搬家就失效；
- 图不做裁剪、不做二值化、不关联错题——切题与批改在 0.3，交外部模型（ADR-007）。
- `scan_question`（迁移 v16）才装切题结果：一页切成 N 块后每块一行，家长点选
  "哪几道错了"，只有被点选的块才建 `question` 并把 id 回填到 `question_id`。
  它**不判对错**，只记录"被标成错"这件事（见 `docs/WRONG-QUESTION-ENTRY.md`）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow


class Scan(Base):
    __tablename__ = "scan"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 谁拍的：一个家庭里每个孩子一套独立的照片与题库。可空只是因为 SQLite 给老库
    # ADD COLUMN 加不上 NOT NULL（迁移 v9 回填存量行）；写入路径一律必填。
    learner_id: Mapped[int | None] = mapped_column(
        ForeignKey("learner.id"), nullable=True, index=True
    )
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    # 相对库目录；同一文件不允许出现两条记录（唯一约束兜住重复入库）
    path: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 来源：device=摄像头/高拍仪抓拍，upload=浏览器上传（迁移 v15）。
    # server_default 兜住两条路：老库 ALTER TABLE 回填 'device'；老备份恢复时
    # 行里没有 source 也不算缺必填（import_service._required 认 server_default）。
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="device", server_default="device"
    )
    # 前端浏览器质量门报的指标 JSON（brightness/dark_ratio/…），可空 = 没报
    quality_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 人工确认入库后指向的 question；NULL = 还没确认（迁移 v15）
    question_id: Mapped[int | None] = mapped_column(
        ForeignKey("question.id"), nullable=True
    )
    # 自动预处理（找纸边 → 透视矫正 → 去阴影）另存的那张图（迁移 v19）：
    # `scans/yyyy/mm/<uuid>.processed.jpg`，与 `path`（原图）同目录。
    # NULL = 没处理（老行、或这张图找不到纸张边界，按约定回落原图）。
    # 原图**永远保留**：`path` 那一份一个字节都不改。
    processed_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Published workflow fixed when this scan is created. NULL is retained for legacy
    # rows and for development databases before the default workflow is initialized.
    workflow_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_version.version_id"), nullable=True, index=True
    )


class ScanQuestion(Base):
    """一页切出来的一道题（迁移 v16）：题号、题框、题干、是否被家长标错。

    这张表是「切题结果」与「错题入库」之间的桥：切题只写这里（`is_wrong`
    初始 False），家长点选后才把 `is_wrong` 置 True 并建 `question`，把新题
    id 回填进 `question_id`。因此：

    - `(scan_id, no)` 唯一 —— 同一张照片重复切题不重复插行（幂等）；
    - `question_id` 为 NULL = 还没入库；非 NULL = 已入库，重复提交不再建题；
    - `bbox_json` 存 `{"bbox": [x0,y0,x1,y1], "page": [w,h], "angle": a}`：
      bbox 落在**摆正后**的页面上，页面叠加时还要知道那页多大、转了多少度，
      否则侧拍的卷子框会整体错位；
    - `stem_text` 可以为空/残缺（OCR 没认出），入库前由服务层按长度挡掉，
      挡掉的行留在表里等人工补录，不删；
    - `inferred_number`（迁移 v18）= 这个题号不是卷面上认出来的，是按跳号推的。
      只给标错页提示家长「请核对」，不影响勾选与入库。
    """

    __tablename__ = "scan_question"
    __table_args__ = (
        UniqueConstraint("scan_id", "no", name="uq_scan_question_scan_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(
        ForeignKey("scan.id"), nullable=False, index=True
    )
    # 卷面原始题号（不是列表下标）：跨栏/跨页时题号不连续也不改
    no: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox_json: Mapped[str] = mapped_column(Text, nullable=False)
    stem_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 家长点选过"这道错了"= True；只是标记，不代表系统判过对错
    is_wrong: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # 题号是推测出来的（迁移 v18）：False = 卷面上直接认到；True = 按跳号 +
    # 正文行补出来的（`QuestionBlock.inferred_number`）。只是提示，不影响
    # 勾选与入库——家长看到「第 21 题」时要知道这个 21 是猜的。
    inferred_number: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    # 入库后关联的题目；NULL = 还没入库（含题干残缺被拒绝入库的行）
    question_id: Mapped[int | None] = mapped_column(
        ForeignKey("question.id"), nullable=True
    )
    # 题块裁图的相对库目录路径（迁移 v17）：
    # `scans/yyyy/mm/crops/<scan_id>-<no>.jpg`，与照片同级。
    # NULL = 还没裁（老行）或裁图失败——裁图失败不挡入库，只在日志留一行。
    crop_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
