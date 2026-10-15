"""YOLO 模型服务。

职责：
- 扫描 backend/models/*.pt
- 加载 / 热切换模型（线程安全，推理与切换互斥）
- 对单帧图像执行推理，输出带类别名、置信度、像素 bbox 的 Detection 列表
- 提供画框工具函数，供摄像头线程叠加到预览流上

设计要点：
- ultralytics 与 torch 均为惰性导入：即使没有安装推理依赖，后端仍能启动并
  正常提供摄像头枚举、串口扫描等接口，只有加载模型时才会报出明确错误。
- 模型放在目录中即被发现，新增/替换权重不需要改任何代码。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

_LOGGER = logging.getLogger(__name__)

# BGR 调色板，按 class_id 取模分配
_PALETTE: tuple[tuple[int, int, int], ...] = (
    (56, 142, 60),
    (47, 47, 211),
    (189, 119, 2),
    (0, 124, 245),
    (162, 31, 123),
    (123, 137, 0),
    (0, 160, 255),
    (255, 138, 68),
)


class DetectorError(RuntimeError):
    """模型加载或推理失败。"""


def _cfg(config: Optional[dict], *keys: str, default: Any = None) -> Any:
    node: Any = config
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return default if node is None else node


@dataclass(slots=True)
class Detection:
    """单个检测框。"""

    class_id: int
    class_name: str
    confidence: float
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2（像素）
    serial_sent: bool = False  # 该结果是否已下发串口

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": int(self.class_id),
            "class_name": self.class_name,
            "confidence": round(float(self.confidence), 4),
            "bbox": [int(v) for v in self.bbox],
            "serial_sent": bool(self.serial_sent),
        }


def draw_detections(
    frame: np.ndarray,
    detections: Optional[list[Detection]],
    show_label: bool = True,
) -> np.ndarray:
    """在图像上绘制检测框（原地修改并返回）。"""
    if frame is None or not detections:
        return frame
    height, width = frame.shape[:2]
    for det in detections:
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        x1 = max(0, min(x1, width - 1))
        x2 = max(0, min(x2, width - 1))
        y1 = max(0, min(y1, height - 1))
        y2 = max(0, min(y2, height - 1))
        color = _PALETTE[abs(int(det.class_id)) % len(_PALETTE)]
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        if not show_label:
            continue
        text = f"{det.class_name} {det.confidence:.2f}"
        if det.serial_sent:
            text += " >> SENT"
        (text_w, text_h), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        top = max(y1, text_h + 8)
        cv2.rectangle(frame, (x1, top - text_h - 8), (x1 + text_w + 10, top + baseline - 2), color, -1)
        cv2.putText(
            frame,
            text,
            (x1 + 5, top - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    return frame


class DetectorService:
    """模型扫描 / 加载 / 热切换 / 推理。"""

    def __init__(self, model_dir: Path | str, config: Optional[dict] = None) -> None:
        self._config = config or {}
        self._model_dir = Path(model_dir).expanduser()
        self._model_dir.mkdir(parents=True, exist_ok=True)

        self._conf = float(_cfg(self._config, "conf", default=0.35))
        self._iou = float(_cfg(self._config, "iou", default=0.45))
        self._imgsz = int(_cfg(self._config, "imgsz", default=640))
        self._max_det = int(_cfg(self._config, "max_det", default=20))
        self._half = bool(_cfg(self._config, "half", default=False))
        self._agnostic = bool(_cfg(self._config, "agnostic_nms", default=False))
        self._device_setting = str(_cfg(self._config, "device", default="auto"))
        self._warmup = bool(_cfg(self._config, "warmup", default=True))
        self._default_model = _cfg(self._config, "default", default=None)

        self._lock = threading.RLock()  # 保护模型引用与切换
        self._infer_lock = threading.Lock()  # 串行化推理，避免共享模型并发调用
        self._model: Any = None
        self._names: dict[int, str] = {}
        self._model_name: Optional[str] = None
        self._model_path: Optional[str] = None
        self._loaded_at: Optional[float] = None
        self._inference_count = 0
        self._last_inference_ms = 0.0

    # ------------------------------------------------------------------ 属性
    @property
    def model_dir(self) -> Path:
        return self._model_dir

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def current_model(self) -> Optional[str]:
        return self._model_name

    @property
    def class_names(self) -> dict[int, str]:
        with self._lock:
            return dict(self._names)

    @property
    def configured_default(self) -> Optional[str]:
        return str(self._default_model) if self._default_model else None

    @property
    def last_inference_ms(self) -> float:
        return self._last_inference_ms

    @property
    def inference_count(self) -> int:
        return self._inference_count

    # ------------------------------------------------------------ 模型扫描
    def resolve_model_path(self, name: str) -> Path:
        """把模型名解析为目录内的绝对路径，并阻止目录穿越。"""
        candidate = Path(str(name)).name
        if not candidate:
            raise DetectorError("模型名不能为空")
        path = (self._model_dir / candidate).resolve()
        base = self._model_dir.resolve()
        if base != path.parent:
            raise DetectorError(f"非法的模型路径: {name}")
        return path

    def list_models(self) -> list[dict[str, Any]]:
        """扫描目录下所有 *.pt，按修改时间倒序返回。"""
        models: list[dict[str, Any]] = []
        try:
            files = sorted(
                self._model_dir.glob("*.pt"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                reverse=True,
            )
        except OSError as exc:  # pragma: no cover - 目录不可读
            raise DetectorError(f"无法扫描模型目录 {self._model_dir}: {exc}") from exc

        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            models.append(
                {
                    "name": path.name,
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "modified_ts": stat.st_mtime,
                    "current": path.name == self._model_name,
                }
            )
        return models

    def pick_default_model(self) -> Optional[str]:
        """按配置的 default -> 最新的 *.pt 顺序挑选默认模型。"""
        wanted = self.configured_default
        if wanted:
            try:
                if self.resolve_model_path(wanted).is_file():
                    return Path(wanted).name
            except DetectorError:
                _LOGGER.warning("settings.yaml 中配置的默认模型非法: %s", wanted)
        models = self.list_models()
        return models[0]["name"] if models else None

    # ------------------------------------------------------------ 模型加载
    @staticmethod
    def _import_yolo() -> Any:
        try:
            from ultralytics import YOLO  # 惰性导入，避免无依赖时启动失败
        except Exception as exc:  # pragma: no cover - 依赖缺失
            raise DetectorError(
                f"无法导入 ultralytics，请先执行 pip install -r backend/requirements.txt（{exc}）"
            ) from exc
        return YOLO

    def _resolve_device(self) -> str:
        setting = (self._device_setting or "auto").strip().lower()
        if setting and setting != "auto":
            return setting
        try:
            import torch

            if torch.cuda.is_available():
                return "0"
        except Exception:  # pragma: no cover - torch 未安装
            pass
        return "cpu"

    def load_model(self, name: str) -> dict[str, Any]:
        """加载（或热切换）指定模型，返回模型信息。"""
        path = self.resolve_model_path(name)
        if not path.is_file():
            raise DetectorError(f"模型文件不存在: {path.name}")

        yolo_cls = self._import_yolo()
        device = self._resolve_device()
        _LOGGER.info("正在加载模型 %s (device=%s)", path.name, device)
        try:
            model = yolo_cls(str(path))
            try:  # 部分模型/后端不支持 to()，失败不影响 predict(device=...)
                model.to(device)
            except Exception as exc:
                _LOGGER.debug("model.to(%s) 跳过: %s", device, exc)
            raw_names = getattr(model, "names", None) or {}
            names = {int(key): str(value) for key, value in dict(raw_names).items()}
        except Exception as exc:
            raise DetectorError(f"加载模型 {path.name} 失败: {exc}") from exc

        with self._lock:
            previous = self._model
            self._model = model
            self._names = names
            self._model_name = path.name
            self._model_path = str(path)
            self._loaded_at = time.time()
        del previous  # 尽快释放旧模型占用的显存/内存

        _LOGGER.info("模型 %s 加载完成，类别数=%d，device=%s", path.name, len(names), device)
        if self._warmup:
            self._run_warmup()
        return self.model_info()

    def _run_warmup(self) -> None:
        size = max(int(self._imgsz), 32)
        blank = np.zeros((size, size, 3), dtype=np.uint8)
        try:
            self.infer(blank)
            _LOGGER.info("模型预热完成（%dx%d 空白图）", size, size)
        except Exception as exc:  # 预热失败不阻断切换，只告警
            _LOGGER.warning("模型预热失败（不影响接口可用性）: %s", exc)

    def model_info(self) -> dict[str, Any]:
        with self._lock:
            name = self._model_name
            names = dict(self._names)
            loaded_at = self._loaded_at
        info: dict[str, Any] = {
            "current": name,
            "loaded": name is not None,
            "class_count": len(names),
            "classes": [names[key] for key in sorted(names)],
            "device": self._resolve_device() if name else None,
            "loaded_at": (
                datetime.fromtimestamp(loaded_at).isoformat(timespec="seconds") if loaded_at else None
            ),
            "inference_count": self._inference_count,
            "last_inference_ms": round(self._last_inference_ms, 2),
        }
        return info

    # ---------------------------------------------------------------- 推理
    def infer(self, frame: np.ndarray, conf: Optional[float] = None) -> list[Detection]:
        """对单帧 BGR 图像执行推理。"""
        if frame is None or getattr(frame, "size", 0) == 0:
            return []
        with self._lock:
            model = self._model
            names = dict(self._names)
        if model is None:
            raise DetectorError("尚未加载任何模型")

        kwargs: dict[str, Any] = {
            "source": frame,
            "conf": float(self._conf if conf is None else conf),
            "iou": self._iou,
            "imgsz": self._imgsz,
            "max_det": self._max_det,
            "device": self._resolve_device(),
            "half": self._half,
            "agnostic_nms": self._agnostic,
            "verbose": False,
        }
        started = time.perf_counter()
        with self._infer_lock:
            try:
                results = model.predict(**kwargs)
            except Exception as exc:
                raise DetectorError(f"推理失败: {exc}") from exc
        self._last_inference_ms = (time.perf_counter() - started) * 1000.0
        self._inference_count += 1

        height, width = frame.shape[:2]
        detections: list[Detection] = []
        for result in results or []:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            try:
                xyxy_list = boxes.xyxy.tolist()
                cls_list = boxes.cls.tolist()
                conf_list = boxes.conf.tolist()
            except Exception as exc:  # pragma: no cover - 结果结构异常
                _LOGGER.warning("解析推理结果失败: %s", exc)
                continue
            for xyxy, cls_id, score in zip(xyxy_list, cls_list, conf_list):
                x1, y1, x2, y2 = (int(round(float(v))) for v in xyxy[:4])
                x1 = max(0, min(x1, width - 1))
                y1 = max(0, min(y1, height - 1))
                x2 = max(0, min(x2, width - 1))
                y2 = max(0, min(y2, height - 1))
                if x2 <= x1 or y2 <= y1:
                    continue
                class_id = int(cls_id)
                detections.append(
                    Detection(
                        class_id=class_id,
                        class_name=names.get(class_id, f"class_{class_id}"),
                        confidence=float(score),
                        bbox=(x1, y1, x2, y2),
                    )
                )
        detections.sort(key=lambda item: item.confidence, reverse=True)
        return detections
