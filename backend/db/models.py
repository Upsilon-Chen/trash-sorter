# -*- coding: utf-8 -*-
"""MySQL / SQLite 表结构（SQLAlchemy 2.0 声明式）。

设计要点
--------
1. **一次下发 = 一行**（`sort_records`）：检测帧率 10 fps，只有真正决定倾倒时才落库。
2. **图片拆到独立表**（`sort_images`）：列表查询永远不触碰大字段，
   缩略图走单独接口。图片按用户要求存进 MySQL（LONGBLOB）。
3. **原始无框帧 + 带框帧都存**：原始帧用于回流训练，带框帧用于记录页展示证据。
4. 人工判定与训练集回流字段一次建好（P1 直接可用，不用迁移）：
   `verified` / `true_class` / `in_dataset` / `dataset_image_id`。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# MySQL 需要 LONGBLOB（普通 BLOB 只有 64 KB，放不下 720p JPEG）；SQLite 用 BLOB
ImageBlob = LargeBinary().with_variant(LONGBLOB(), "mysql")


class Base(DeclarativeBase):
    pass


class SortRecord(Base):
    """一次分拣下发（自动或手动）的完整记录。"""

    __tablename__ = "sort_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # ---- 时间 ----
    created_at: Mapped[datetime] = mapped_column(
        DateTime(3), nullable=False, server_default=func.now()
    )
    ts: Mapped[float] = mapped_column(Numeric(16, 3), default=0)  # 原始 time.time()

    # ---- 是谁触发的 ----
    source: Mapped[str] = mapped_column(String(16), default="auto")  # auto / manual
    session_id: Mapped[Optional[str]] = mapped_column(String(36), default=None)

    # ---- 模型输出 ----
    class_name: Mapped[str] = mapped_column(String(32), default="")
    label_zh: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(5, 4), default=None)
    bbox: Mapped[Optional[dict]] = mapped_column(JSON, default=None)  # {x1,y1,x2,y2}
    model_name: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    camera_index: Mapped[Optional[int]] = mapped_column(Integer, default=None)

    # ---- 动作 ----
    bucket_slot: Mapped[int] = mapped_column(Integer, default=0)
    bucket_label: Mapped[str] = mapped_column(String(32), default="")
    angle: Mapped[int] = mapped_column(Integer, default=0)
    angle_h: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    angle_v: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    serial_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(255), default="")

    # ---- 人工判定（P1）----
    verified: Mapped[str] = mapped_column(String(16), default="pending")  # pending/correct/wrong
    true_class: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(3), default=None)
    verified_by: Mapped[Optional[str]] = mapped_column(String(32), default=None)

    # ---- 训练集回流（P1）----
    in_dataset: Mapped[bool] = mapped_column(Boolean, default=False)
    dataset_image_id: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)

    # ---- 图片是否存在（避免列表接口 JOIN 大字段）----
    has_image: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("idx_records_time", "created_at"),
        Index("idx_records_class_time", "class_name", "created_at"),
        Index("idx_records_verified", "verified"),
        Index("idx_records_dataset", "in_dataset"),
        Index("idx_records_session", "session_id"),
    )


class SortImage(Base):
    """记录对应的图片（1:1，单独表避免拖慢列表查询）。"""

    __tablename__ = "sort_images"

    record_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True  # 与 sort_records.id 一一对应
    )
    image_raw = mapped_column(ImageBlob, default=None)      # 原始无框帧（回流训练用）
    image_marked = mapped_column(ImageBlob, default=None)   # 带检测框（证据展示用）
    thumb = mapped_column(ImageBlob, default=None)          # 320px 缩略图

    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    raw_size: Mapped[int] = mapped_column(Integer, default=0)
    marked_size: Mapped[int] = mapped_column(Integer, default=0)
    thumb_size: Mapped[int] = mapped_column(Integer, default=0)
    sha1: Mapped[Optional[str]] = mapped_column(String(40), default=None)


class DatasetImage(Base):
    """训练图片索引（文件仍以磁盘为准，这里只做索引与元数据，P1 使用）。"""

    __tablename__ = "dataset_images"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(255), unique=True)
    class_name: Mapped[str] = mapped_column(String(32), default="")
    split: Mapped[str] = mapped_column(String(16), default="unsplit")  # train/val/test/unsplit
    source: Mapped[str] = mapped_column(String(16), default="capture")  # capture/record/augment
    record_id: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    sha1: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    labeled: Mapped[bool] = mapped_column(Boolean, default=False)
    label_path: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(3), server_default=func.now())
    note: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    __table_args__ = (Index("idx_dataset_class", "class_name", "split"),)
