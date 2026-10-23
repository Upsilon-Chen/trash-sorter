# -*- coding: utf-8 -*-
"""分拣记录采集器：把"一次下发"落成数据库一行 + 三张图。

为什么用独立线程 + 队列
----------------------
检测流水线在独立线程里以 10 fps 跑，**绝对不能**在里面做 JPEG 编码和数据库写入。
这里用 `queue.Queue` + 一个 daemon 线程：
    pipeline 线程：on_event() 只做入队（微秒级）
    writer  线程：取帧 → 编码 JPEG（原始/带框/缩略图）→ 写 MySQL

不侵入 pipeline
---------------
`Pipeline` 每次下发都会 `emit({"type": "sort", ...})`，我们把它包一层即可，
`pipeline.py` 一行都不用改（`EventBus.emit` 本身是线程安全的同步函数）。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

import cv2
import numpy as np

from .. import db
from ..db.models import SortImage, SortRecord

_LOGGER = logging.getLogger(__name__)


def _cfg(*keys: str, default: Any = None) -> Any:
    """读取 settings.local.yaml / settings.yaml 的 records 段。"""
    from . import serial_esp32  # noqa: F401  仅确保包已加载

    try:
        from ..db.session import _read_yaml, CONFIG_DIR  # type: ignore[attr-defined]

        merged: dict[str, Any] = {}
        for name in ("settings.yaml", "settings.local.yaml"):
            cfg = _read_yaml(CONFIG_DIR / name).get("records") or {}
            if isinstance(cfg, dict):
                merged.update(cfg)
        value: Any = merged
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value if value is not None else default
    except Exception:  # noqa: BLE001
        return default


class RecordRecorder:
    """分拣记录采集器（线程安全，失败不影响主流程）。"""

    def __init__(self, camera: Any, model_name_fn: Optional[Callable[[], Optional[str]]] = None) -> None:
        self._camera = camera
        self._model_name_fn = model_name_fn
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=int(_cfg("queue_size", default=128) or 128)
        )
        self._thread: Optional[threading.Thread] = None
        self._session_id = uuid.uuid4().hex[:12]
        # 最近一次检测结果：class_name -> {confidence, bbox}
        self._last_detections: dict[str, dict[str, Any]] = {}
        self._dropped = 0
        self._written = 0
        self._failed = 0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not db.session.available():
            _LOGGER.warning("数据库不可用，分拣记录功能关闭")
            return
        self._thread = threading.Thread(target=self._worker, name="record-writer", daemon=True)
        self._thread.start()
        _LOGGER.info("分拣记录采集已启动（会话 %s）", self._session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._thread is not None and self._thread.is_alive(),
            "session_id": self._session_id,
            "queued": self._queue.qsize(),
            "written": self._written,
            "failed": self._failed,
            "dropped": self._dropped,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------------ 事件入口
    def wrap(self, inner: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
        """包装 EventBus.emit：先喂给自己（绝不抛异常），再转发原实现。"""

        def emit(event: dict[str, Any]) -> None:
            try:
                self.on_event(event)
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                _LOGGER.debug("记录采集异常（已忽略）: %s", exc)
            return inner(event)

        return emit

    def on_event(self, event: dict[str, Any]) -> None:
        """由 pipeline 线程调用，必须极快。"""
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "detection":
            self._remember(event.get("data") or {})
        elif kind == "sort":
            self._enqueue(event.get("data") or {})

    def _remember(self, data: dict[str, Any]) -> None:
        detections = data.get("detections") or data.get("boxes") or []
        if not isinstance(detections, list):
            return
        cache: dict[str, dict[str, Any]] = {}
        for det in detections:
            if not isinstance(det, dict):
                continue
            name = det.get("class_name") or det.get("class") or det.get("name") or ""
            if not name:
                continue
            conf = det.get("confidence", det.get("conf"))
            box = det.get("bbox") or det.get("box")
            cache[str(name)] = {
                "confidence": float(conf) if conf is not None else None,
                "bbox": box if isinstance(box, (list, tuple, dict)) else None,
            }
        if cache:
            self._last_detections = cache

    def _enqueue(self, outcome: dict[str, Any]) -> None:
        item = {
            "outcome": outcome,
            "frame": self._cam_frame(),
            "detections": dict(self._last_detections),
            "model_name": self._model_name(),
            "camera_index": self._camera_index(),
            "created_at": datetime.now(),
        }
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._dropped += 1
            _LOGGER.warning("记录队列已满，丢弃一条（累计丢弃 %d）", self._dropped)

    def _cam_frame(self) -> Optional[np.ndarray]:
        try:
            frame = self._camera.get_frame(copy=True)
            return frame if isinstance(frame, np.ndarray) else None
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("取帧失败: %s", exc)
            return None

    def _model_name(self) -> Optional[str]:
        try:
            return self._model_name_fn() if self._model_name_fn else None
        except Exception:  # noqa: BLE001
            return None

    def _camera_index(self) -> Optional[int]:
        try:
            return self._camera.current_index
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ 写入线程
    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                self._save(item)
                self._written += 1
            except Exception as exc:  # noqa: BLE001
                self._failed += 1
                self._last_error = str(exc)
                _LOGGER.warning("写入分拣记录失败: %s", exc)
            finally:
                self._queue.task_done()

    def _save(self, item: dict[str, Any]) -> None:
        outcome: dict[str, Any] = item["outcome"]
        frame: Optional[np.ndarray] = item["frame"]
        class_name = str(outcome.get("class_name") or "")

        record = SortRecord(
            created_at=item["created_at"],
            ts=round(time.time(), 3),
            source="manual" if outcome.get("manual") else "auto",
            session_id=self._session_id,
            class_name=class_name,
            label_zh=str(outcome.get("label_zh") or ""),
            model_name=item.get("model_name"),
            camera_index=item.get("camera_index"),
            bucket_slot=int(outcome.get("slot") or 0),
            bucket_label=str(outcome.get("group_label_zh") or ""),
            angle=int(outcome.get("angle") or 0),
            angle_h=outcome.get("h"),
            angle_v=outcome.get("v"),
            ok=bool(outcome.get("ok")),
            serial_sent=bool(outcome.get("serial_sent")),
            reason=str(outcome.get("reason") or "")[:255],
            verified="pending",
            in_dataset=False,
        )

        det = (item.get("detections") or {}).get(class_name) or {}
        if det.get("confidence") is not None:
            record.confidence = min(0.9999, max(0.0, float(det["confidence"])))
        if det.get("bbox") is not None:
            record.bbox = _normalize_bbox(det["bbox"])

        image_row: Optional[SortImage] = None
        if frame is not None:
            image_row = self._encode_images(frame, item.get("detections") or {})
            record.has_image = image_row is not None

        with db.session.session() as session:
            session.add(record)
            session.flush()  # 拿到自增 id
            if image_row is not None:
                image_row.record_id = int(record.id)
                session.add(image_row)
            session.commit()

    def _encode_images(self, frame: np.ndarray, detections: dict[str, Any]) -> Optional[SortImage]:
        """编码三张图：原始 / 带框 / 缩略图。任何一步失败都降级，不抛异常。"""
        quality = int(_cfg("jpeg_quality", default=85) or 85)
        thumb_w = int(_cfg("thumb_width", default=320) or 320)
        thumb_q = int(_cfg("thumb_quality", default=70) or 70)

        try:
            height, width = frame.shape[:2]
            raw_jpeg = _encode_jpeg(frame, quality)
            if raw_jpeg is None:
                return None

            marked = frame
            try:
                marked = _draw_detections(frame, detections)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("绘制检测框失败（用原始帧替代）: %s", exc)
            marked_jpeg = _encode_jpeg(marked, quality) or raw_jpeg

            scale = min(1.0, thumb_w / float(width or 1))
            thumb_frame = cv2.resize(
                marked,
                (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
            thumb_jpeg = _encode_jpeg(thumb_frame, thumb_q) or marked_jpeg

            return SortImage(
                image_raw=raw_jpeg,
                image_marked=marked_jpeg,
                thumb=thumb_jpeg,
                width=int(width),
                height=int(height),
                raw_size=len(raw_jpeg),
                marked_size=len(marked_jpeg),
                thumb_size=len(thumb_jpeg),
                sha1=_sha1(raw_jpeg),
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("图片编码失败: %s", exc)
            return None


# ---------------------------------------------------------------------- 工具函数
def _encode_jpeg(frame: np.ndarray, quality: int) -> Optional[bytes]:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        return None
    return buf.tobytes()


def _sha1(data: bytes) -> str:
    import hashlib

    return hashlib.sha1(data).hexdigest()


def _normalize_bbox(box: Any) -> Optional[dict[str, int]]:
    try:
        if isinstance(box, dict):
            keys = ("x1", "y1", "x2", "y2")
            if all(k in box for k in keys):
                return {k: int(float(box[k])) for k in keys}
            return None
        if isinstance(box, (list, tuple)) and len(box) == 4:
            return {"x1": int(box[0]), "y1": int(box[1]), "x2": int(box[2]), "y2": int(box[3])}
    except Exception:  # noqa: BLE001
        return None
    return None


def _draw_detections(frame: np.ndarray, detections: dict[str, Any]) -> np.ndarray:
    """在副本上画检测框（原始帧保持干净，用于回流训练）。"""
    canvas = frame.copy()
    for name, det in (detections or {}).items():
        box = _normalize_bbox(det.get("bbox"))
        if not box:
            continue
        conf = det.get("confidence")
        label = f"{name} {conf:.2f}" if isinstance(conf, (int, float)) else str(name)
        cv2.rectangle(canvas, (box["x1"], box["y1"]), (box["x2"], box["y2"]), (60, 220, 140), 2)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(
            canvas,
            (box["x1"], max(0, box["y1"] - th - 8)),
            (box["x1"] + tw + 8, box["y1"]),
            (60, 220, 140),
            -1,
        )
        cv2.putText(
            canvas,
            label,
            (box["x1"] + 4, max(12, box["y1"] - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (10, 20, 14),
            2,
            cv2.LINE_AA,
        )
    return canvas
