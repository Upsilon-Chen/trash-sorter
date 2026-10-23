# -*- coding: utf-8 -*-
"""分拣记录相关接口（/api/db/*、/api/records/*）。

全部为同步 SQLAlchemy 调用，统一用 `asyncio.to_thread` 执行，
不占用事件循环（与应用内既有的阻塞调用风格一致）。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Integer, delete, func, select

from . import db
from .db.models import DatasetImage, SortImage, SortRecord

router = APIRouter(prefix="/api", tags=["records"])

ROOT_DIR = Path(__file__).resolve().parent.parent
CAPTURES_DIR = ROOT_DIR / "training" / "captures"


def _recorder(request: Request):
    return getattr(request.app.state, "recorder", None)


def _record_dict(row: SortRecord) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds") if row.created_at else None,
        "source": row.source,
        "session_id": row.session_id,
        "class_name": row.class_name,
        "label_zh": row.label_zh,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "bbox": row.bbox,
        "model_name": row.model_name,
        "camera_index": row.camera_index,
        "bucket_slot": int(row.bucket_slot or 0),
        "bucket_label": row.bucket_label,
        "angle": int(row.angle or 0),
        "angle_h": row.angle_h,
        "angle_v": row.angle_v,
        "ok": bool(row.ok),
        "serial_sent": bool(row.serial_sent),
        "reason": row.reason,
        "verified": row.verified,
        "true_class": row.true_class,
        "verified_at": row.verified_at.isoformat(sep=" ", timespec="seconds") if row.verified_at else None,
        "verified_by": row.verified_by,
        "in_dataset": bool(row.in_dataset),
        "dataset_image_id": row.dataset_image_id,
        "has_image": bool(row.has_image),
    }


# ---------------------------------------------------------------------- 状态
@router.get("/db/status")
async def db_status(request: Request) -> dict[str, Any]:
    """数据库与记录采集器状态（前端用来显示“记录功能是否可用”）。"""
    info = await asyncio.to_thread(db.session.status)
    rec = _recorder(request)
    info["recorder"] = rec.status() if rec is not None else {"enabled": False}
    return info


@router.post("/db/reconnect")
async def db_reconnect() -> dict[str, Any]:
    """数据库断开后手动重连。"""
    ok = await asyncio.to_thread(db.session.init, True)
    return {"ok": ok, **(await asyncio.to_thread(db.session.status))}


# ---------------------------------------------------------------------- 列表
@router.get("/records")
async def list_records(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    class_name: Optional[str] = None,
    bucket_slot: Optional[int] = None,
    source: Optional[str] = None,
    verified: Optional[str] = None,
    in_dataset: Optional[bool] = None,
    serial_sent: Optional[bool] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    q: Optional[str] = None,
    order: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接，记录功能不可用")

    def run() -> dict[str, Any]:
        conds = []
        if class_name:
            conds.append(SortRecord.class_name == class_name)
        if bucket_slot is not None:
            conds.append(SortRecord.bucket_slot == bucket_slot)
        if source:
            conds.append(SortRecord.source == source)
        if verified:
            conds.append(SortRecord.verified == verified)
        if in_dataset is not None:
            conds.append(SortRecord.in_dataset.is_(in_dataset))
        if serial_sent is not None:
            conds.append(SortRecord.serial_sent.is_(serial_sent))
        if date_from:
            conds.append(SortRecord.created_at >= datetime.combine(date_from, datetime.min.time()))
        if date_to:
            conds.append(SortRecord.created_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
        if q:
            like = f"%{q}%"
            conds.append(
                SortRecord.class_name.like(like)
                | SortRecord.label_zh.like(like)
                | SortRecord.bucket_label.like(like)
                | SortRecord.reason.like(like)
            )

        with db.session.session() as session:
            total = int(
                session.execute(select(func.count()).select_from(SortRecord).where(*conds)).scalar() or 0
            )
            order_by = SortRecord.id.desc() if order == "desc" else SortRecord.id.asc()
            rows = (
                session.execute(
                    select(SortRecord)
                    .where(*conds)
                    .order_by(order_by)
                    .limit(size)
                    .offset((page - 1) * size)
                )
                .scalars()
                .all()
            )
            return {
                "total": total,
                "page": page,
                "size": size,
                "pages": max(1, (total + size - 1) // size),
                "items": [_record_dict(r) for r in rows],
            }

    return await asyncio.to_thread(run)


@router.get("/records/stats")
async def records_stats(days: int = Query(7, ge=1, le=365)) -> dict[str, Any]:
    """按类别 / 桶位 / 天 / 判定状态聚合。"""
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接")

    since = datetime.now() - timedelta(days=days)

    def run() -> dict[str, Any]:
        with db.session.session() as session:
            by_class = session.execute(
                select(SortRecord.class_name, SortRecord.label_zh, func.count())
                .where(SortRecord.created_at >= since)
                .group_by(SortRecord.class_name, SortRecord.label_zh)
                .order_by(func.count().desc())
            ).all()
            by_bucket = session.execute(
                select(SortRecord.bucket_slot, SortRecord.bucket_label, func.count())
                .where(SortRecord.created_at >= since)
                .group_by(SortRecord.bucket_slot, SortRecord.bucket_label)
                .order_by(SortRecord.bucket_slot.asc())
            ).all()
            by_day = session.execute(
                select(func.date(SortRecord.created_at), func.count())
                .where(SortRecord.created_at >= since)
                .group_by(func.date(SortRecord.created_at))
                .order_by(func.date(SortRecord.created_at).asc())
            ).all()
            by_verified = session.execute(
                select(SortRecord.verified, func.count())
                .where(SortRecord.created_at >= since)
                .group_by(SortRecord.verified)
            ).all()
            failed = int(
                session.execute(
                    select(func.count())
                    .select_from(SortRecord)
                    .where(SortRecord.created_at >= since, SortRecord.serial_sent.is_(False))
                ).scalar()
                or 0
            )
            total = int(
                session.execute(
                    select(func.count()).select_from(SortRecord).where(SortRecord.created_at >= since)
                ).scalar()
                or 0
            )
            return {
                "days": days,
                "total": total,
                "serial_failed": failed,
                "by_class": [
                    {"class_name": c or "", "label_zh": zh or "", "count": int(n)} for c, zh, n in by_class
                ],
                "by_bucket": [
                    {"slot": int(s or 0), "label": label or "", "count": int(n)} for s, label, n in by_bucket
                ],
                "by_day": [{"day": str(d), "count": int(n)} for d, n in by_day],
                "by_verified": {str(k or "pending"): int(n) for k, n in by_verified},
            }

    return await asyncio.to_thread(run)


@router.get("/records/{rid}")
async def get_record(rid: int) -> dict[str, Any]:
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接")

    def run() -> dict[str, Any]:
        with db.session.session() as session:
            row = session.get(SortRecord, rid)
            if row is None:
                raise HTTPException(status_code=404, detail="记录不存在")
            data = _record_dict(row)
            img = session.get(SortImage, rid)
            if img is not None:
                data["image"] = {
                    "width": img.width,
                    "height": img.height,
                    "raw_size": img.raw_size,
                    "marked_size": img.marked_size,
                    "thumb_size": img.thumb_size,
                    "sha1": img.sha1,
                }
            return data

    return await asyncio.to_thread(run)


@router.get("/records/{rid}/image")
async def get_record_image(rid: int, kind: str = Query("thumb", pattern="^(thumb|raw|marked)$")):
    """取记录图片（thumb / marked / raw）。带长缓存头，列表滚动不再重复请求。"""
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接")

    def run() -> Optional[bytes]:
        with db.session.session() as session:
            img = session.get(SortImage, rid)
            if img is None:
                return None
            return {"thumb": img.thumb, "raw": img.image_raw, "marked": img.image_marked}.get(kind)

    blob = await asyncio.to_thread(run)
    if not blob:
        placeholder = _placeholder_jpeg()
        return Response(content=placeholder, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    return Response(
        content=blob,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------- 人工判定
class VerifyPayload(BaseModel):
    verified: str = Field(pattern="^(pending|correct|wrong)$")
    true_class: Optional[str] = Field(default=None, max_length=32)
    by: Optional[str] = Field(default=None, max_length=32)


@router.patch("/records/{rid}/verify")
async def verify_record(rid: int, payload: VerifyPayload) -> dict[str, Any]:
    """人工判定模型是否正确。判定为 wrong 时必须给 true_class。"""
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接")
    if payload.verified == "wrong" and not payload.true_class:
        raise HTTPException(status_code=422, detail="判定为“识别错误”时必须指定真实类别")

    def run() -> dict[str, Any]:
        with db.session.session() as session:
            row = session.get(SortRecord, rid)
            if row is None:
                raise HTTPException(status_code=404, detail="记录不存在")
            row.verified = payload.verified
            row.true_class = payload.true_class if payload.verified == "wrong" else None
            row.verified_at = None if payload.verified == "pending" else datetime.now()
            row.verified_by = payload.by
            session.commit()
            return _record_dict(row)

    return await asyncio.to_thread(run)


# ---------------------------------------------------------------------- 回流训练集
class PromotePayload(BaseModel):
    class_name: Optional[str] = Field(default=None, max_length=32)
    note: Optional[str] = Field(default=None, max_length=255)


@router.post("/records/{rid}/promote")
async def promote_record(rid: int, payload: PromotePayload) -> dict[str, Any]:
    """把记录的**原始无框帧**写成一张训练图并登记到 dataset_images。

    目录规则与现有抓拍一致：`training/captures/<类别>/<时间戳>_rec<id>.jpg`
    类别优先用 payload.class_name（人工纠正后的真实类别），否则用记录的类别。
    """
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接")

    def run() -> dict[str, Any]:
        with db.session.session() as session:
            row = session.get(SortRecord, rid)
            if row is None:
                raise HTTPException(status_code=404, detail="记录不存在")
            img = session.get(SortImage, rid)
            if img is None or not img.image_raw:
                raise HTTPException(status_code=409, detail="该记录没有原始图片，无法加入训练集")
            if row.in_dataset and row.dataset_image_id:
                return {"ok": True, "already": True, **_record_dict(row)}

            class_name = (payload.class_name or row.true_class or row.class_name or "").strip()
            if not class_name:
                raise HTTPException(status_code=422, detail="缺少类别，无法归档")

            target_dir = CAPTURES_DIR / class_name
            target_dir.mkdir(parents=True, exist_ok=True)
            stamp = (row.created_at or datetime.now()).strftime("%Y%m%d-%H%M%S")
            filename = f"{stamp}_rec{rid}.jpg"
            target = target_dir / filename
            target.write_bytes(bytes(img.image_raw))

            entry = DatasetImage(
                path=str(target.relative_to(ROOT_DIR)).replace("\\", "/"),
                class_name=class_name,
                split="unsplit",
                source="record",
                record_id=rid,
                width=img.width,
                height=img.height,
                sha1=img.sha1,
                labeled=False,
                note=(payload.note or f"来自记录 #{rid}（模型预测 {row.class_name}）")[:255],
            )
            session.add(entry)
            session.flush()
            row.in_dataset = True
            row.dataset_image_id = int(entry.id)
            if not row.true_class and class_name != row.class_name:
                row.true_class = class_name
            session.commit()
            data = _record_dict(row)
            data["saved_path"] = str(target)
            return {"ok": True, **data}

    return await asyncio.to_thread(run)


# ---------------------------------------------------------------------- 导出
@router.get("/records-export.csv")
async def export_records(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(5000, ge=1, le=50000),
) -> Response:
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接")

    def run() -> str:
        conds = []
        if date_from:
            conds.append(SortRecord.created_at >= datetime.combine(date_from, datetime.min.time()))
        if date_to:
            conds.append(SortRecord.created_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
        with db.session.session() as session:
            rows = (
                session.execute(select(SortRecord).where(*conds).order_by(SortRecord.id.desc()).limit(limit))
                .scalars()
                .all()
            )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["id", "时间", "来源", "预测类别", "中文名", "置信度", "桶位", "桶名", "角度",
             "串口下发", "原因", "人工判定", "真实类别", "已入训练集", "模型"]
        )
        for r in rows:
            writer.writerow([
                r.id, r.created_at, r.source, r.class_name, r.label_zh,
                float(r.confidence) if r.confidence is not None else "",
                r.bucket_slot, r.bucket_label, r.angle,
                "是" if r.serial_sent else "否", r.reason,
                r.verified, r.true_class or "", "是" if r.in_dataset else "否", r.model_name or "",
            ])
        return buf.getvalue()

    text = await asyncio.to_thread(run)
    body = "\ufeff" + text  # BOM：Excel 打开不乱码
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sort-records.csv"'},
    )


# ---------------------------------------------------------------------- 杂项
def _placeholder_jpeg() -> bytes:
    """没有图片时的占位图（深灰底 + 文字），避免前端出现裂图。"""
    import cv2
    import numpy as np

    canvas = np.full((360, 480, 3), 24, dtype=np.uint8)
    cv2.putText(canvas, "no image", (150, 190), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (90, 120, 105), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", canvas)
    return buf.tobytes() if ok else b""


# ---------------------------------------------------------------------------
# 数据集索引（P1）：汇总、回填、清理
# ---------------------------------------------------------------------------
class BackfillPayload(BaseModel):
    overwrite: bool = False
    limit: Optional[int] = Field(default=None, ge=1, le=200000)


@router.get("/dataset/summary")
async def dataset_summary() -> dict[str, Any]:
    """训练集索引汇总：按类别统计已标注 / 未标注 / 已增强。"""
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接")

    def run() -> dict[str, Any]:
        with db.session.session() as session:
            rows = session.execute(
                select(
                    DatasetImage.class_name,
                    func.count(),
                    func.sum(func.cast(DatasetImage.labeled, Integer)),
                ).group_by(DatasetImage.class_name)
            ).all()
            total = int(session.execute(select(func.count()).select_from(DatasetImage)).scalar() or 0)
            return {
                "total": total,
                "classes": [
                    {
                        "class_name": name or "",
                        "count": int(count),
                        "labeled": int(labeled or 0),
                        "unlabeled": int(count) - int(labeled or 0),
                    }
                    for name, count, labeled in rows
                ],
            }

    return await asyncio.to_thread(run)


@router.post("/dataset/backfill")
async def dataset_backfill(payload: BackfillPayload) -> dict[str, Any]:
    """把 training/captures 下已有的抓拍图导入 dataset_images（幂等）。

    路径已存在则跳过（overwrite=True 时更新元数据）。用于让记录中心/统计
    能看到历史上抓拍的 1900+ 张图。
    """
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接")

    def run() -> dict[str, Any]:
        import hashlib

        files: list[Path] = []
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.bmp"):
            files.extend(sorted(CAPTURES_DIR.rglob(pattern)) if CAPTURES_DIR.exists() else [])
        files = [f for f in files if f.is_file() and "_r90" not in f.stem and "_fh" not in f.stem]
        if payload.limit:
            files = files[: payload.limit]

        inserted = skipped = updated = 0
        with db.session.session() as session:
            existing = {
                str(p or "")
                for (p,) in session.execute(select(DatasetImage.path)).all()
            }
            for path in files:
                rel = str(path.relative_to(ROOT_DIR)).replace("\\", "/")
                label_candidates = [
                    path.with_suffix(".txt"),
                    CAPTURES_DIR / "labels" / f"{path.stem}.txt",
                ]
                label_path = next((str(c.relative_to(ROOT_DIR)).replace("\\", "/") for c in label_candidates if c.exists()), None)
                if rel in existing and not payload.overwrite:
                    skipped += 1
                    continue
                try:
                    data = path.read_bytes()
                    sha1 = hashlib.sha1(data).hexdigest()
                except Exception:  # noqa: BLE001
                    sha1 = None
                # 宽高：用 OpenCV 读头部（失败则为 0）
                width = height = 0
                try:
                    import cv2

                    img = cv2.imread(str(path))
                    if img is not None:
                        height, width = img.shape[:2]
                except Exception:  # noqa: BLE001
                    pass
                class_name = path.parent.name if path.parent != CAPTURES_DIR else ""
                if rel in existing:
                    row = session.execute(select(DatasetImage).where(DatasetImage.path == rel)).scalar_one_or_none()
                    if row is not None:
                        row.class_name, row.labeled, row.label_path = class_name, bool(label_path), label_path
                        row.width, row.height, row.sha1 = width, height, sha1
                        updated += 1
                        continue
                session.add(
                    DatasetImage(
                        path=rel,
                        class_name=class_name,
                        split="unsplit",
                        source="augment" if any(s in path.stem for s in ("_r180", "_r270", "_fv", "_br", "_dk", "_ns")) else "capture",
                        width=width,
                        height=height,
                        sha1=sha1,
                        labeled=bool(label_path),
                        label_path=label_path,
                    )
                )
                inserted += 1
                if inserted % 200 == 0:
                    session.flush()
            session.commit()
        return {
            "scanned": len(files),
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "total": inserted + updated + skipped,
        }

    return await asyncio.to_thread(run)


@router.post("/records-cleanup")
async def records_cleanup(days: int = Query(30, ge=1, le=3650), dry_run: bool = Query(False)) -> dict[str, Any]:
    """删除 N 天前的记录（图片在同一事务里一起删，释放 BLOB 空间）。"""
    if not db.session.available():
        raise HTTPException(status_code=503, detail="数据库未连接")

    cutoff = datetime.now() - timedelta(days=days)

    def run() -> dict[str, Any]:
        with db.session.session() as session:
            ids = [
                int(i)
                for (i,) in session.execute(
                    select(SortRecord.id).where(SortRecord.created_at < cutoff)
                ).all()
            ]
            freed = 0
            if ids:
                freed = int(
                    session.execute(
                        select(
                            func.coalesce(func.sum(SortImage.raw_size), 0)
                            + func.coalesce(func.sum(SortImage.marked_size), 0)
                            + func.coalesce(func.sum(SortImage.thumb_size), 0)
                        ).where(SortImage.record_id.in_(ids))
                    ).scalar()
                    or 0
                )
            if not dry_run and ids:
                session.execute(delete(SortImage).where(SortImage.record_id.in_(ids)))
                session.execute(delete(SortRecord).where(SortRecord.id.in_(ids)))
                session.commit()
            return {
                "cutoff": cutoff.isoformat(sep=" ", timespec="seconds"),
                "matched": len(ids),
                "deleted": 0 if dry_run else len(ids),
                "freed_bytes": freed if not dry_run else 0,
                "dry_run": dry_run,
            }

    return await asyncio.to_thread(run)