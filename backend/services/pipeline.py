"""检测流水线。

职责（把摄像头 / 模型 / 串口串成业务流程）：
    取帧 -> YOLO 推理 -> 查 classes.json -> 冷却判定 -> pyserial 下发 -> WS 推送

设计要点：
- 检测跑在独立 daemon 线程里，FastAPI 事件循环只通过 emit 回调收到事件，
  因此推理再慢也不会阻塞接口。
- 同一类别有冷却时间（settings.yaml 的 detection.cooldown_seconds，
  可被 classes.json 中该类的 cooldown 覆盖），避免连续重复倾倒。
- 类别 -> 槽位/角度映射全部来自 classes.json，新增类别不需要改动本文件。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from .camera import CameraService
from .detector import Detection, DetectorService
from .serial_esp32 import SerialService

_LOGGER = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """流水线状态或参数错误。"""


def _cfg(config: Optional[dict], *keys: str, default: Any = None) -> Any:
    node: Any = config
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return default if node is None else node


@dataclass(slots=True)
class SortOutcome:
    """一次分类下发的结果。"""

    ok: bool
    class_name: str
    slot: int
    angle: int
    label_zh: str
    group_label_zh: str
    serial_sent: bool
    reason: str
    manual: bool
    ts: float
    # 手动覆盖的水平/竖直角度（None 表示用 slot 默认值）
    h: Optional[int] = None
    v: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "class_name": self.class_name,
            "slot": int(self.slot),
            "angle": int(self.angle),
            "h": self.h,
            "v": self.v,
            "label_zh": self.label_zh,
            "group_label_zh": self.group_label_zh,
            "serial_sent": bool(self.serial_sent),
            "reason": self.reason,
            "manual": bool(self.manual),
            "ts": self.ts,
            "time": datetime.fromtimestamp(self.ts).isoformat(timespec="seconds"),
        }


class Pipeline:
    """检测 + 分类下发的后台流水线。"""

    # 多目标（一帧里出现多个不同类别的垃圾）的处理策略
    VALID_MULTI_POLICIES = ("priority", "largest", "confidence", "hold")

    def __init__(
        self,
        camera: CameraService,
        detector: DetectorService,
        serial_service: SerialService,
        settings: Optional[dict] = None,
        classes: Optional[dict] = None,
        emit: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self._camera = camera
        self._detector = detector
        self._serial = serial_service
        self._settings = settings or {}
        self._classes = classes or {}
        self._emit = emit or (lambda event: None)

        self._target_fps = max(0.5, float(_cfg(self._settings, "detection", "target_fps", default=10)))
        self._default_cooldown = float(_cfg(self._settings, "detection", "cooldown_seconds", default=3.0))
        self._min_confidence = float(_cfg(self._settings, "detection", "min_confidence", default=0.35))
        self._push_interval = float(_cfg(self._settings, "detection", "push_interval", default=0.2))
        self._push_empty = bool(_cfg(self._settings, "detection", "push_empty", default=False))
        self._sort_enabled = bool(_cfg(self._settings, "detection", "sort_enabled", default=True))
        # 大于该比例（占整幅画面）的检测框直接丢弃：托盘/桌面被整块误识别时靠它兜底，0 表示关闭
        self._max_box_area = float(_cfg(self._settings, "detection", "max_box_area", default=0.0))
        # 一帧里同时出现多个不同类别的垃圾时怎么办：
        #   priority   按物理桶优先级倒（classes.json groups[].priority，有害垃圾最优先）
        #   largest    倒画面里最大的那个
        #   confidence 倒置信度最高的那个（最接近旧行为）
        #   hold       不下发，提示"一次只能放一个"（零错分，但需要人工介入）
        self._multi_policy = str(_cfg(self._settings, "detection", "multi_object", default="priority")).strip().lower()
        if self._multi_policy not in self.VALID_MULTI_POLICIES:
            _LOGGER.warning("未知的 detection.multi_object=%r，已回退到 priority", self._multi_policy)
            self._multi_policy = "priority"
        # 参与"多目标"判定的最小面积占比，挡住小噪点误触发
        self._multi_min_area = float(_cfg(self._settings, "detection", "multi_min_area", default=0.01))
        # 连续多少帧选中同一个目标才真正下发（1 = 关闭确认，和旧行为一致）
        self._stable_frames = max(1, int(_cfg(self._settings, "detection", "stable_frames", default=3)))
        self._candidate_key: Optional[tuple[str, int]] = None
        self._candidate_hits = 0
        self._last_multi: Optional[dict[str, Any]] = None

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 冷却按【物理槽位】记账：同一个桶的不同类别（如 Paper 与 Cardboard）共用冷却
        self._last_sent: dict[int, float] = {}
        self._overlay: list[Detection] = []
        self._last_results: list[dict[str, Any]] = []
        self._last_outcome: Optional[dict[str, Any]] = None
        self._counts: dict[str, int] = {}  # 识别类别 -> 下发次数
        self._group_counts: dict[str, int] = {}  # 物理槽位 -> 下发次数
        self._frames = 0
        self._fps = 0.0
        self._last_push = 0.0
        self._started_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._desired_running = False  # 用户是否要求检测处于运行状态（看门狗据此重启）
        self._detection_restarts = 0
        self._filtered_large = 0

    # ------------------------------------------------------------ 类别映射
    def class_table(self) -> dict[str, Any]:
        table = self._classes.get("classes") if isinstance(self._classes, dict) else None
        return table if isinstance(table, dict) else {}

    def default_entry(self) -> dict[str, Any]:
        entry = self._classes.get("default") if isinstance(self._classes, dict) else None
        return entry if isinstance(entry, dict) else {}

    def group_table(self) -> dict[str, Any]:
        """物理槽位（桶）表：{"0": {slot, label_zh, cooldown}, ...}。"""
        table = self._classes.get("groups") if isinstance(self._classes, dict) else None
        return table if isinstance(table, dict) else {}

    def resolve_group(self, slot: int) -> dict[str, Any]:
        """把槽位号解析成物理桶信息（未定义时走 default）。"""
        slot_value = int(slot)
        entry = self.group_table().get(str(slot_value))
        fallback = self.default_entry()
        source = entry if isinstance(entry, dict) else {}
        label = source.get("label_zh") or fallback.get("label_zh") or f"slot {slot_value}"
        cooldown = source.get("cooldown", None)
        if cooldown is None:
            cooldown = self._default_cooldown
        try:
            # 多目标时用：数字越小越优先；没配就退化成 slot 号
            priority = int(source.get("priority", slot_value))
        except (TypeError, ValueError):
            priority = slot_value
        return {
            "slot": slot_value,
            "label_zh": str(label),
            "cooldown": float(cooldown),
            "priority": priority,
            "defined": isinstance(entry, dict),
        }

    def resolve_class(self, class_name: str) -> dict[str, Any]:
        """把识别类别解析成 槽位 / 角度 / 所属物理桶 / 冷却时间（未知类别走 default）。

        两层映射：data.yaml 里的 6 个识别类别 -> classes.json 的 slot -> groups 里的物理桶。
        多个类别可以指向同一个 slot（例如 Paper 与 Cardboard 都进"纸类"桶）。
        """
        entry = self.class_table().get(class_name)
        fallback = self.default_entry()
        source = entry if isinstance(entry, dict) else fallback

        slot = source.get("slot", fallback.get("slot", 0))
        angle = source.get("angle", fallback.get("angle", 90))
        label = source.get("label_zh") or fallback.get("label_zh") or class_name

        try:
            slot_value = int(slot)
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"classes.json 中 {class_name} 的 slot 非法: {slot!r}") from exc
        try:
            angle_value = int(angle)
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"classes.json 中 {class_name} 的 angle 非法: {angle!r}") from exc

        group = self.resolve_group(slot_value)
        # 冷却优先取类别自己的，没写则用所属物理桶的，再退回全局默认
        cooldown = source.get("cooldown", None)
        cooldown = group["cooldown"] if cooldown is None else float(cooldown)

        return {
            "class_name": class_name,
            "slot": slot_value,
            "angle": angle_value,
            "label_zh": str(label),
            "group_label_zh": group["label_zh"],
            "priority": int(group["priority"]),
            "cooldown": cooldown,
            "mapped": isinstance(entry, dict),
        }

    def cooldown_remaining(self, class_name: str, now: Optional[float] = None) -> float:
        """返回该类别所属物理桶的剩余冷却秒数（0 表示可下发）。"""
        config = self.resolve_class(class_name)
        return self.cooldown_remaining_for_slot(int(config["slot"]), now)

    def cooldown_remaining_for_slot(self, slot: int, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        last = self._last_sent.get(int(slot))
        if last is None:
            return 0.0
        return max(0.0, float(self.resolve_group(int(slot))["cooldown"]) - (now - last))

    # ---------------------------------------------------------------- 状态
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()

    def get_overlay(self) -> list[Detection]:
        """供摄像头线程叠加到预览流上的最新检测框。"""
        with self._lock:
            return list(self._overlay)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            cooldowns = {
                str(slot): round(max(0.0, float(self.resolve_group(slot)["cooldown"]) - (now - ts)), 2)
                for slot, ts in self._last_sent.items()
            }
            return {
                "running": self.running,
                "frames": self._frames,
                "fps": round(self._fps, 2),
                "started_at": (
                    datetime.fromtimestamp(self._started_at).isoformat(timespec="seconds")
                    if self._started_at
                    else None
                ),
                "results": list(self._last_results),
                "counts": dict(self._counts),
                "group_counts": dict(self._group_counts),
                "cooldowns": cooldowns,
                "last_outcome": self._last_outcome,
                "last_error": self._last_error,
                "sort_enabled": self._sort_enabled,
                "target_fps": self._target_fps,
                "min_confidence": self._min_confidence,
                "max_box_area": self._max_box_area,
                "filtered_large": self._filtered_large,
                "multi_object": self._multi_policy,
                "multi_min_area": self._multi_min_area,
                "stable_frames": self._stable_frames,
                "last_multi": self._last_multi,
                "desired_running": self._desired_running,
                "detection_restarts": self._detection_restarts,
                "classes": self.class_table(),
                "groups": self.group_table(),
                "camera": self._camera.status(),
                "model": self._detector.model_info(),
                "serial": self._serial.status(),
            }

    # ---------------------------------------------------------------- 控制
    def start(self, reset_stats: bool = False, conf: Optional[float] = None) -> dict[str, Any]:
        """启动检测线程。

        conf 不为空时作为本次检测的置信度阈值（同时用于推理与结果过滤），
        覆盖 settings.yaml 的 detection.min_confidence；不传则沿用当前阈值
        （启动时为配置值，之后为上一次传入的值）。
        """
        with self._lock:
            if self.running:
                return self.snapshot()
            if not self._camera.is_open:
                raise PipelineError("尚未选择摄像头，无法开始检测")
            if not self._detector.ready:
                raise PipelineError("尚未加载模型，无法开始检测")
            if conf is not None:
                try:
                    self._min_confidence = max(0.01, min(float(conf), 0.99))
                except (TypeError, ValueError) as exc:
                    raise PipelineError(f"非法的置信度阈值: {conf!r}") from exc
            if reset_stats:
                self._counts = {}
                self._group_counts = {}
                self._last_sent = {}
                self._frames = 0
                self._last_outcome = None
            self._last_error = None
            self._desired_running = True
            self._stop.clear()
            self._started_at = time.time()
            self._thread = threading.Thread(target=self._loop, name="detection-loop", daemon=True)
            self._thread.start()
        _LOGGER.info("检测流水线已启动（目标 %s fps，阈值 %.2f）", self._target_fps, self._min_confidence)
        snapshot = self.snapshot()
        self._emit({"type": "detection_state", "data": snapshot})
        return snapshot

    def stop(self) -> dict[str, Any]:
        """停止检测线程并清空叠加框。"""
        self._desired_running = False
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        with self._lock:
            self._thread = None
            self._overlay = []
            self._last_results = []
            self._fps = 0.0
        _LOGGER.info("检测流水线已停止")
        snapshot = self.snapshot()
        self._emit({"type": "detection_state", "data": snapshot})
        return snapshot

    def ensure_running(self) -> str:
        """看门狗调用：用户开着检测但线程意外退出时自动重启。

        返回 "idle"（本来就没开）/ "ok" / "restarted" / "failed"。
        """
        with self._lock:
            if not self._desired_running:
                return "idle"
            if self.running:
                return "ok"
            self._thread = None
        _LOGGER.warning("检测线程已意外退出，看门狗正在重启")
        self._detection_restarts += 1
        try:
            self.start()
        except Exception as exc:
            _LOGGER.error("重启检测线程失败: %s", exc)
            return "failed"
        return "restarted"

    def manual_sort(
        self,
        class_name: Optional[str] = None,
        slot: Optional[int] = None,
        angle: Optional[int] = None,
        force: bool = True,
        h: Optional[int] = None,
        v: Optional[int] = None,
    ) -> dict[str, Any]:
        """手动下发一次分类指令（force=False 时同样受冷却限制）。

        注意：槽位（slot）决定落到哪个物理桶、也就决定了固件的水平/竖直角度；
        `angle` 只是随协议带过去的记录值，固件不使用。要临时改角度请用 h/v 覆盖。
        """
        if not class_name and slot is None and angle is None:
            raise PipelineError("必须提供 class_name 或 slot/angle 之一")
        name = str(class_name) if class_name else "manual"
        config = self.resolve_class(name)
        if slot is not None:
            config["slot"] = int(slot)
        if angle is not None:
            config["angle"] = int(angle)
        if h is not None:
            config["h"] = int(h)
        if v is not None:
            config["v"] = int(v)

        now = time.monotonic()
        outcome = self._dispatch(config, now=now, manual=True, force=force)
        self._emit({"type": "sort", "data": outcome.to_dict()})
        if not outcome.ok:
            raise PipelineError(outcome.reason)
        return outcome.to_dict()

    # ------------------------------------------------------------ 下发逻辑
    def _dispatch(
        self,
        config: dict[str, Any],
        now: float,
        manual: bool = False,
        force: bool = False,
    ) -> SortOutcome:
        """执行冷却判定 + 串口下发，返回结果对象。"""
        class_name = str(config["class_name"])
        slot = int(config["slot"])
        angle = int(config["angle"])
        # h/v 是"显式覆盖"，固件优先使用它们；angle 字段固件按设计忽略
        override_h = config.get("h")
        override_v = config.get("v")
        override_h = int(override_h) if override_h is not None else None
        override_v = int(override_v) if override_v is not None else None
        label = str(config.get("label_zh") or class_name)
        group_label = str(config.get("group_label_zh") or self.resolve_group(slot)["label_zh"])
        cooldown = float(config.get("cooldown") or self.resolve_group(slot)["cooldown"])

        # 冷却按物理桶（slot）判定：同桶的不同类别不会连续倒两次
        if not force and cooldown > 0:
            last = self._last_sent.get(slot)
            if last is not None and (now - last) < cooldown:
                remaining = cooldown - (now - last)
                return SortOutcome(
                    ok=False,
                    class_name=class_name,
                    slot=slot,
                    angle=angle,
                    label_zh=label,
                    group_label_zh=group_label,
                    serial_sent=False,
                    reason=f"{group_label} 冷却中，剩余 {remaining:.1f}s",
                    manual=manual,
                    ts=time.time(),
                    h=override_h,
                    v=override_v,
                )

        result = self._serial.send_sort(class_name, slot, angle, override_h, override_v)
        if not result.get("ok"):
            return SortOutcome(
                ok=False,
                class_name=class_name,
                slot=slot,
                angle=angle,
                label_zh=label,
                group_label_zh=group_label,
                serial_sent=False,
                reason=str(result.get("error") or "串口下发失败"),
                manual=manual,
                ts=time.time(),
                h=override_h,
                v=override_v,
            )

        with self._lock:
            self._last_sent[slot] = now
            self._counts[class_name] = self._counts.get(class_name, 0) + 1
            self._group_counts[str(slot)] = self._group_counts.get(str(slot), 0) + 1
        outcome = SortOutcome(
            ok=True,
            class_name=class_name,
            slot=slot,
            angle=angle,
            label_zh=label,
            group_label_zh=group_label,
            serial_sent=True,
            reason=f"已下发串口（{group_label}）",
            manual=manual,
            ts=time.time(),
            h=override_h,
            v=override_v,
        )
        self._last_outcome = outcome.to_dict()
        return outcome

    # ------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        interval = 1.0 / self._target_fps
        fps_ema = 0.0
        _LOGGER.debug("检测线程启动")
        while not self._stop.is_set():
            cycle_start = time.perf_counter()
            try:
                self._process_once()
            except Exception as exc:  # 任何异常都不允许打死检测线程
                self._last_error = str(exc)
                _LOGGER.exception("检测循环异常: %s", exc)
                self._emit({"type": "error", "data": {"scope": "detection", "message": str(exc)}})
                if self._stop.wait(0.5):
                    break

            elapsed = time.perf_counter() - cycle_start
            instant_fps = 1.0 / elapsed if elapsed > 0 else 0.0
            fps_ema = instant_fps if fps_ema == 0 else (fps_ema * 0.8 + instant_fps * 0.2)
            with self._lock:
                self._fps = fps_ema
                self._frames += 1
            remaining = interval - elapsed
            if remaining > 0 and self._stop.wait(remaining):
                break
        _LOGGER.debug("检测线程退出")

    # ------------------------------------------------------------ 多目标决策
    def set_multi_policy(self, policy: str) -> dict[str, Any]:
        """运行时切换"一帧多个不同类别"的处理策略，立即生效并广播新快照。"""
        value = str(policy or "").strip().lower()
        if value not in self.VALID_MULTI_POLICIES:
            raise PipelineError(
                f"未知的多目标策略 {policy!r}，可选：{'、'.join(self.VALID_MULTI_POLICIES)}"
            )
        with self._lock:
            self._multi_policy = value
            # 策略变了，之前累计的"连续帧确认"作废
            self._candidate_key = None
            self._candidate_hits = 0
            self._last_multi = None
        _LOGGER.info("多目标策略已切换为 %s", value)
        snapshot = self.snapshot()
        self._emit({"type": "detection_state", "data": snapshot})
        return snapshot

    def _select_target(
        self, detections: list[Detection], frame_shape: tuple[int, ...]
    ) -> tuple[Optional[Detection], Optional[dict[str, Any]]]:
        """一帧里可能同时有多个垃圾：按配置的策略挑一个来倒。

        物理约束（很重要）：托盘只有"往一个方向倾斜"这一个动作，一次会把托盘上的东西
        **全部**倒进同一个桶。所以两个不同桶的东西同时出现时，必然有一个会被分错，
        策略只能决定"牺牲哪一个"，做不到分别投递。

        返回 (选中的目标, 多目标信息)；没有多目标时第二项为 None。
        """
        if not detections:
            return None, None

        frame_area = float(frame_shape[0] * frame_shape[1]) or 1.0
        enriched: list[dict[str, Any]] = []
        for det in detections:
            config = self.resolve_class(det.class_name)
            x1, y1, x2, y2 = det.bbox
            enriched.append(
                {
                    "det": det,
                    "config": config,
                    "area": ((x2 - x1) * (y2 - y1)) / frame_area,
                }
            )

        # 只有"够大"的目标才参与多目标判定，避免一个很小的噪点把结果带偏
        significant = [item for item in enriched if item["area"] >= self._multi_min_area] or enriched

        if len({int(item["config"]["slot"]) for item in significant}) < 2:
            # 同一桶（或只有一个目标）：取置信度最高的即可，结果一样
            chosen = max(significant, key=lambda item: item["det"].confidence)
            return chosen["det"], None  # type: ignore[return-value]

        info: dict[str, Any] = {
            "count": len(significant),
            "policy": self._multi_policy,
            "objects": [
                {
                    "class_name": item["det"].class_name,
                    "label_zh": item["config"]["label_zh"],
                    "slot": int(item["config"]["slot"]),
                    "group_label_zh": item["config"]["group_label_zh"],
                    "confidence": round(float(item["det"].confidence), 3),
                    "area": round(float(item["area"]), 4),
                }
                for item in sorted(significant, key=lambda item: item["area"], reverse=True)
            ],
        }

        if self._multi_policy == "hold":
            info.update(
                {
                    "held": True,
                    "chosen": None,
                    "reason": "画面里有多个不同类别的垃圾，按当前策略不下发（一次只能放一件）",
                }
            )
            return None, info

        if self._multi_policy == "largest":
            key = lambda item: item["area"]  # noqa: E731
        elif self._multi_policy == "confidence":
            key = lambda item: item["det"].confidence  # noqa: E731
        else:  # priority：优先级数字最小者胜，平手看置信度
            key = lambda item: (  # noqa: E731
                -int(item["config"].get("priority", item["config"]["slot"])),
                item["det"].confidence,
            )

        chosen = max(significant, key=key)
        info.update(
            {
                "held": False,
                "chosen": chosen["det"].class_name,
                "chosen_group_zh": chosen["config"]["group_label_zh"],
            }
        )
        return chosen["det"], info

    def _process_once(self) -> None:
        frame = self._camera.get_frame()
        if frame is None:
            time.sleep(0.02)
            return

        detections = self._detector.infer(frame, conf=self._min_confidence)
        detections = [det for det in detections if det.confidence >= self._min_confidence]

        # 丢掉"几乎占满画面"的框：托盘/桌面被整块误识别成某一类时，面积最大的就是它。
        # 真正的垃圾只占画面一小部分，所以这个阈值能有效挡住"把托盘当纸张"。
        if self._max_box_area > 0 and detections:
            height, width = frame.shape[:2]
            frame_area = float(height * width) or 1.0
            kept: list[Detection] = []
            for det in detections:
                x1, y1, x2, y2 = det.bbox
                if ((x2 - x1) * (y2 - y1)) / frame_area <= self._max_box_area:
                    kept.append(det)
            if len(kept) != len(detections):
                self._filtered_large += len(detections) - len(kept)
                _LOGGER.debug(
                    "过滤掉 %d 个过大检测框（阈值 %.2f，累计 %d）",
                    len(detections) - len(kept),
                    self._max_box_area,
                    self._filtered_large,
                )
            detections = kept

        now = time.monotonic()
        for det in detections:
            det.serial_sent = False

        multi_info: Optional[dict[str, Any]] = None
        if detections:
            target, multi_info = self._select_target(detections, frame.shape)
            if target is None:
                # hold 策略：画面里有多个不同类别的垃圾，不下发，只提示
                if multi_info is not None and multi_info.get("held"):
                    _LOGGER.info(
                        "多目标暂不下发（策略 %s）：%s",
                        multi_info.get("policy"),
                        "、".join(str(item["label_zh"]) for item in multi_info.get("objects", [])),
                    )
                self._candidate_key = None
                self._candidate_hits = 0
            elif self._sort_enabled:
                config = self.resolve_class(target.class_name)
                candidate = (target.class_name, int(config["slot"]))
                if candidate == self._candidate_key:
                    self._candidate_hits += 1
                else:
                    self._candidate_key = candidate
                    self._candidate_hits = 1
                # 连续 stable_frames 帧都选中同一个目标才真正下发，避免某一帧抖动就倒错
                if self._candidate_hits >= self._stable_frames:
                    outcome = self._dispatch(config, now=now, manual=False, force=False)
                    if outcome.serial_sent:
                        target.serial_sent = True
                        self._candidate_hits = 0  # 倒完重新计数，冷却结束后再重新确认
                        # 计数在 _dispatch 里统一累加（自动与手动一致）
                        self._emit({"type": "sort", "data": outcome.to_dict()})
                    elif "冷却中" not in outcome.reason:
                        # 串口未连接/写入失败等，降频告警即可
                        _LOGGER.debug("自动分类未下发: %s", outcome.reason)
        else:
            self._candidate_key = None
            self._candidate_hits = 0

        self._last_multi = multi_info
        results = [det.to_dict() for det in detections]
        with self._lock:
            self._overlay = list(detections)
            self._last_results = results

        if results or self._push_empty:
            if now - self._last_push >= self._push_interval:
                self._last_push = now
                payload: dict[str, Any] = {
                    "results": results,
                    "count": len(results),
                    "fps": round(self._fps, 2),
                    "inference_ms": round(self._detector.last_inference_ms, 2),
                    "model": self._detector.current_model,
                    "ts": time.time(),
                }
                if multi_info:
                    payload["multi"] = multi_info
                self._emit({"type": "detection", "data": payload})

    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()
