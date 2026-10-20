"""摄像头服务。

职责：
- 枚举可用摄像头（0 ~ scan_max_index，逐个打开并真实读帧，能读帧才算可用）
- 选择 / 打开 / 释放摄像头
- 后台抓帧线程：持续产出原始帧供推理使用，并按需编码 JPEG 供预览使用
- 提供 MJPEG 分块生成器给 GET /api/video/feed

设计要点：
- 摄像头完全由后端 OpenCV 掌控，前端只发指令、只看流。
- 抓帧、编码都在独立线程里做，FastAPI 事件循环只做取最新帧（O(1) 引用交换），
  因此 /api/video/feed 与推理都不会阻塞接口。
- 只有存在 MJPEG 订阅者时才做 JPEG 编码，空闲时不浪费 CPU。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import cv2
import numpy as np

from .detector import Detection, draw_detections

# 必须在 import cv2 之后设置，抑制枚举不存在设备时的大量告警
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
try:  # pragma: no cover - 取决于 OpenCV 构建
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)  # type: ignore[attr-defined]
except Exception:
    pass

_LOGGER = logging.getLogger(__name__)

MJPEG_BOUNDARY = "frame"
_MJPEG_CHUNK = (
    b"--" + MJPEG_BOUNDARY.encode() + b"\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n"
)


class CameraError(RuntimeError):
    """摄像头操作失败。"""


def _cfg(config: Optional[dict], *keys: str, default: Any = None) -> Any:
    node: Any = config
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return default if node is None else node


def _backend_table() -> dict[str, int]:
    return {
        "any": int(getattr(cv2, "CAP_ANY", 0)),
        "dshow": int(getattr(cv2, "CAP_DSHOW", 700)),
        "msmf": int(getattr(cv2, "CAP_MSMF", 1400)),
        "v4l2": int(getattr(cv2, "CAP_V4L2", 200)),
        "avfoundation": int(getattr(cv2, "CAP_AVFOUNDATION", 1200)),
    }


def _backend_candidates(name: Optional[str]) -> list[tuple[str, int]]:
    """按平台给出采集后端尝试顺序。"""
    table = _backend_table()
    key = str(name or "auto").strip().lower()
    if key in table:
        return [(key, table[key])]
    if sys.platform.startswith("win"):
        order = ["dshow", "msmf", "any"]
    elif sys.platform == "darwin":
        order = ["avfoundation", "any"]
    else:
        order = ["v4l2", "any"]
    return [(item, table[item]) for item in order]


@dataclass(slots=True)
class CameraInfo:
    index: int
    width: int
    height: int
    backend: str
    fps: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "name": f"Camera {self.index}",
            "width": int(self.width),
            "height": int(self.height),
            "backend": self.backend,
            "fps": round(float(self.fps), 2),
        }


class CameraService:
    """摄像头枚举 / 选择 / 抓帧 / MJPEG 帧源。"""

    def __init__(self, config: Optional[dict] = None) -> None:
        # 枚举缓存：探测某些摄像头（虚拟设备 / 异常 dshow 设备）会阻塞几秒到几分钟，
        # 前端每次刷新都触发一次的话，界面会长时间卡在"正在刷新"。
        # 所以：结果缓存 TTL + 失败项负缓存（失败过的索引一段时间内不再探测）。
        self._enum_cache: Optional[tuple[float, list[CameraInfo]]] = None
        self._enum_cache_seconds = float(_cfg(config, "enumerate_cache_seconds", default=20.0))
        self._probe_failures: dict[int, float] = {}
        self._probe_failure_ttl = float(_cfg(config, "probe_failure_ttl_seconds", default=120.0))
        self._config = config or {}
        self._scan_max_index = int(_cfg(self._config, "scan_max_index", default=10))
        self._width = int(_cfg(self._config, "width", default=1280))
        self._height = int(_cfg(self._config, "height", default=720))
        self._fps = float(_cfg(self._config, "fps", default=30))
        self._buffer_size = int(_cfg(self._config, "buffer_size", default=1))
        self._warmup_frames = max(1, int(_cfg(self._config, "warmup_frames", default=5)))
        self._read_retries = max(1, int(_cfg(self._config, "read_retries", default=3)))
        self._reopen_after_failures = max(1, int(_cfg(self._config, "reopen_after_failures", default=30)))
        self._min_mean_brightness = float(_cfg(self._config, "min_mean_brightness", default=0.5))
        self._jpeg_quality = int(_cfg(self._config, "jpeg_quality", default=80))
        self._stream_fps = float(_cfg(self._config, "stream_fps", default=25))

        self._lock = threading.RLock()
        # 所有 OpenCV 设备调用（探测/打开/释放/读帧）必须串行。
        # Windows 上两个线程同时操作同一个 VideoCapture 会从 OpenCV 原生层
        # 直接崩掉整个进程（Python 的 try/except 拦不住），
        # 这正是"后端跑一会儿突然关闭"的根因。
        self._device_lock = threading.RLock()
        # 同一时刻只允许一次枚举，避免前端重复调用把设备搅乱
        self._enumerate_lock = threading.Lock()
        self._capture: Optional[cv2.VideoCapture] = None
        self._info: Optional[CameraInfo] = None

        self._raw_frame: Optional[np.ndarray] = None
        self._jpeg: Optional[bytes] = None
        self._frame_count = 0
        self._frame_ts = 0.0

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._stream_clients = 0
        self._encode_errors = 0
        self._consecutive_failures = 0
        self._restarts = 0
        self._last_restart_ts = 0.0
        self._grab_error: Optional[str] = None
        self._next_reopen_at = 0.0  # 重开失败后的退避时间点（time.monotonic）
        self._overlay_provider: Optional[Callable[[], list[Detection]]] = None
        self._placeholder: Optional[bytes] = None

    # ------------------------------------------------------------ 基础状态
    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._capture is not None and self._info is not None

    @property
    def current_index(self) -> Optional[int]:
        with self._lock:
            return self._info.index if self._info else None

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def status(self) -> dict[str, Any]:
        with self._lock:
            info = self._info.to_dict() if self._info else None
            frame_ts = self._frame_ts
            encoding = self._stream_clients > 0
            thread = self._thread
        return {
            "opened": info is not None,
            "camera": info,
            "frame_count": self._frame_count,
            "last_frame_ts": frame_ts or None,
            "frame_age": round(time.time() - frame_ts, 2) if frame_ts else None,
            "grab_thread_alive": bool(thread is not None and thread.is_alive()),
            "restarts": self._restarts,
            "last_restart_ts": self._last_restart_ts or None,
            "last_error": self._grab_error,
            "stream_clients": self._stream_clients,
            "encoding": encoding,
            "overlay_attached": self._overlay_provider is not None,
        }

    def set_overlay_provider(self, provider: Optional[Callable[[], list[Detection]]]) -> None:
        """注册检测框提供者，预览流即可叠加检测结果。"""
        self._overlay_provider = provider

    # ---------------------------------------------------------------- 枚举
    def _grab_valid(self, capture: cv2.VideoCapture) -> Optional[np.ndarray]:
        """读若干帧，返回第一张有效帧；读不到返回 None。"""
        frame: Optional[np.ndarray] = None
        attempts = self._warmup_frames + self._read_retries
        for _ in range(attempts):
            ok, candidate = capture.read()
            if ok and candidate is not None and getattr(candidate, "size", 0) > 0:
                frame = candidate
                break
            time.sleep(0.03)
        if frame is None:
            return None
        if self._min_mean_brightness > 0:
            try:
                if float(frame.mean()) <= self._min_mean_brightness:
                    return None
            except Exception:  # pragma: no cover
                pass
        return frame

    def _configure_capture(self, capture: cv2.VideoCapture) -> None:
        for prop, value in (
            (cv2.CAP_PROP_FRAME_WIDTH, self._width),
            (cv2.CAP_PROP_FRAME_HEIGHT, self._height),
            (cv2.CAP_PROP_FPS, self._fps),
            (cv2.CAP_PROP_BUFFERSIZE, self._buffer_size),
        ):
            try:
                capture.set(prop, value)
            except Exception:  # pragma: no cover - 部分后端不支持
                pass

    def _acquire_device(self, timeout: float = 5.0) -> bool:
        """拿设备锁；拿不到就跳过本轮（宁可不做，也不要并发碰设备导致崩溃）。"""
        if self._device_lock.acquire(timeout=timeout):
            return True
        _LOGGER.error("等待摄像头设备锁超时（%.1fs），本轮跳过错开/释放设备", timeout)
        return False

    def _probe(self, index: int) -> Optional[CameraInfo]:
        """尝试打开某个序号，成功（真的读到帧）则返回信息。

        全程持有设备锁：探测会真的打开/释放设备，绝不能和抓帧线程并发。
        """
        if not self._acquire_device():
            return None
        try:
            for backend_name, backend_id in _backend_candidates(_cfg(self._config, "backend", default="auto")):
                capture: Optional[cv2.VideoCapture] = None
                try:
                    capture = cv2.VideoCapture(index, backend_id)
                    if not capture or not capture.isOpened():
                        continue
                    self._configure_capture(capture)
                    frame = self._grab_valid(capture)
                    if frame is None:
                        continue
                    height, width = frame.shape[:2]
                    fps = 0.0
                    try:
                        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
                    except Exception:  # pragma: no cover
                        pass
                    return CameraInfo(index=index, width=width, height=height, backend=backend_name, fps=fps)
                except Exception as exc:  # pragma: no cover - 驱动异常
                    _LOGGER.debug("探测摄像头 %s (%s) 异常: %s", index, backend_name, exc)
                finally:
                    if capture is not None:
                        try:
                            capture.release()
                        except Exception:  # pragma: no cover
                            pass
            return None
        finally:
            self._device_lock.release()

    def list_cameras(self, cache_seconds: Optional[float] = None) -> list[CameraInfo]:
        """枚举 0 ~ scan_max_index，能读帧才返回。

        正在使用中的那台设备**不会去探测**（探测会 open/release 它，
        和抓帧线程并发时会把进程搞崩），直接用已知信息。
        """
        # 命中缓存直接返回：探测是重活（实测单次 3 秒起，虚拟设备可到几分钟）
        now = time.monotonic()
        cached = self._enum_cache
        ttl = self._enum_cache_seconds if cache_seconds is None else float(cache_seconds)
        if cached is not None and ttl > 0 and now - cached[0] < ttl:
            _LOGGER.info("摄像头枚举命中缓存（%.1f 秒前，共 %d 个）", now - cached[0], len(cached[1]))
            return list(cached[1])

        # 关键修复：自动路径绝不阻塞等锁。
        # 之前页面来回切换会并发发起多次 /api/cameras，每个都阻塞在 to_thread 的线程里
        # 等最多 30 秒，把线程池占满 → 所有走 to_thread 的接口（模型 / 串口 / 标注列表…）
        # 全部排队、界面表现为"所有列表一直在刷新但没结果"。
        if not self._enumerate_lock.acquire(blocking=False):
            if cached is not None:
                _LOGGER.info("摄像头枚举进行中，直接返回上次结果（共 %d 个）", len(cached[1]))
                return list(cached[1])
            _LOGGER.info("摄像头枚举进行中且暂无缓存，先返回空列表，前端稍后会自动重试")
            return []
        try:
            with self._lock:
                current_info = self._info
                in_use = current_info.index if current_info is not None else None

            found: list[CameraInfo] = []
            skipped = 0
            _LOGGER.info("开始枚举摄像头 0-%d ...（正在使用 %s，跳过探测）", self._scan_max_index, in_use)
            for index in range(0, self._scan_max_index + 1):
                if index == in_use and current_info is not None:
                    found.append(current_info)
                    continue
                failed_at = self._probe_failures.get(index)
                if failed_at is not None and now - failed_at < self._probe_failure_ttl:
                    skipped += 1
                    continue
                info = self._probe(index)
                if info is not None:
                    found.append(info)
                    self._probe_failures.pop(index, None)
                    _LOGGER.info(
                        "发现摄像头 index=%d %dx%d backend=%s",
                        info.index,
                        info.width,
                        info.height,
                        info.backend,
                    )
                else:
                    self._probe_failures[index] = time.monotonic()
            self._enum_cache = (time.monotonic(), list(found))
            _LOGGER.info(
                "摄像头枚举完成，共 %d 个（跳过最近失败的 %d 个索引，耗时 %.2f 秒）",
                len(found),
                skipped,
                time.monotonic() - now,
            )
            return list(found)
        finally:
            self._enumerate_lock.release()

    # ---------------------------------------------------------------- 开关
    def open(self, index: int) -> CameraInfo:
        """打开指定摄像头并启动抓帧线程。"""
        index = int(index)
        if index < 0:
            raise CameraError(f"非法的摄像头序号: {index}")

        # 先切换状态，避免旧设备占用
        self.close()

        if not self._acquire_device(timeout=15.0):
            raise CameraError("摄像头设备正忙（上一次设备操作还没结束），请稍后重试")

        try:
            capture: Optional[cv2.VideoCapture] = None
            selected: Optional[CameraInfo] = None
            last_error: Optional[str] = None
            for backend_name, backend_id in _backend_candidates(_cfg(self._config, "backend", default="auto")):
                try:
                    candidate = cv2.VideoCapture(index, backend_id)
                except Exception as exc:  # pragma: no cover
                    last_error = str(exc)
                    continue
                if not candidate or not candidate.isOpened():
                    if candidate is not None:
                        candidate.release()
                    continue
                self._configure_capture(candidate)
                frame = self._grab_valid(candidate)
                if frame is None:
                    candidate.release()
                    last_error = "打开成功但无法读取有效帧"
                    continue
                height, width = frame.shape[:2]
                fps = 0.0
                try:
                    fps = float(candidate.get(cv2.CAP_PROP_FPS) or 0.0)
                except Exception:  # pragma: no cover
                    pass
                capture = candidate
                selected = CameraInfo(index=index, width=width, height=height, backend=backend_name, fps=fps)
                break

            if capture is None or selected is None:
                raise CameraError(f"无法打开摄像头 {index}：{last_error or '设备不存在或被占用'}")

            with self._lock:
                self._capture = capture
                self._info = selected
                self._raw_frame = frame
                self._frame_count = 0
                self._frame_ts = time.time()
                self._consecutive_failures = 0
                self._stop.clear()
                self._thread = threading.Thread(target=self._grab_loop, name="camera-grab", daemon=True)
                self._thread.start()
        finally:
            self._device_lock.release()

        _LOGGER.info("摄像头 %d 已打开 (%dx%d, backend=%s)", index, selected.width, selected.height, selected.backend)
        return selected

    def select(self, index: int) -> CameraInfo:
        """open 的语义化别名，配合 POST /api/camera/select。"""
        return self.open(index)

    def close(self) -> None:
        """停止抓帧线程并释放设备（释放动作必须独占设备锁）。"""
        thread = self._thread
        self._stop.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            self._info = None
            self._thread = None
            self._raw_frame = None
            self._jpeg = None
            self._consecutive_failures = 0

        if self._acquire_device(timeout=8.0):
            try:
                with self._lock:
                    capture = self._capture
                    self._capture = None
                if capture is not None:
                    capture.release()
            except Exception:  # pragma: no cover
                pass
            finally:
                self._device_lock.release()
        else:
            _LOGGER.error("关闭摄像头时拿不到设备锁，本轮跳过释放（宁可泄露也不并发碰设备导致崩溃）")
        _LOGGER.info("摄像头已释放")

    # ------------------------------------------------------------ 抓帧线程
    def _grab_loop(self) -> None:
        """抓帧线程。整段兜底：这个线程绝不能死，否则画面和检测都会停住。"""
        interval = 1.0 / max(self._fps, 1.0)
        try:
            while not self._stop.is_set():
                started = time.perf_counter()
                # 读帧全程持有设备锁：绝不允许"枚举/重开/释放"和读帧并发，
                # 否则 Windows 上会从 OpenCV 原生层直接崩溃（整个进程退出）
                with self._device_lock:
                    capture = self._capture
                    if capture is None:
                        break
                    try:
                        ok, frame = capture.read()
                    except Exception as exc:  # pragma: no cover - 驱动异常
                        _LOGGER.warning("读帧异常: %s", exc)
                        ok, frame = False, None

                if not ok or frame is None or getattr(frame, "size", 0) == 0:
                    self._consecutive_failures += 1
                    # 退避：重开失败后不要每 50ms 猛敲一次设备
                    if (
                        self._consecutive_failures >= self._reopen_after_failures
                        and time.monotonic() >= self._next_reopen_at
                    ):
                        _LOGGER.warning("连续 %d 次读帧失败，尝试原地重开摄像头", self._consecutive_failures)
                        self._reopen()
                    time.sleep(0.05)
                    continue

                self._consecutive_failures = 0
                with self._lock:
                    self._raw_frame = frame
                    self._frame_ts = time.time()
                    self._frame_count += 1
                    need_jpeg = self._stream_clients > 0
                if need_jpeg:
                    jpeg = self._encode_jpeg(frame)
                    if jpeg is not None:
                        with self._lock:
                            self._jpeg = jpeg

                elapsed = time.perf_counter() - started
                if elapsed < interval:
                    time.sleep(interval - elapsed)
        except BaseException as exc:  # 兜底：任何异常都不允许打死抓帧线程
            self._grab_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.exception("抓帧线程异常退出，等看门狗重开摄像头: %s", exc)

    def ensure_healthy(self, stale_seconds: float = 6.0) -> str:
        """看门狗调用：线程死了、或长时间没有新帧（休眠/设备掉线）就整机重开。

        返回 "closed" / "busy" / "ok" / "backoff" / "restarted" / "failed"。
        """
        # 枚举期间读帧会被短暂阻塞，此时重开只会火上浇油
        if self._enumerate_lock.locked():
            return "busy"

        with self._lock:
            info = self._info
            thread = self._thread
            frame_ts = self._frame_ts
        if info is None:
            return "closed"

        now = time.time()
        thread_alive = thread is not None and thread.is_alive()
        stale = frame_ts > 0 and (now - frame_ts) > stale_seconds
        if thread_alive and not stale:
            return "ok"

        if time.monotonic() < self._next_reopen_at:
            return "backoff"

        reason = "抓帧线程已退出" if not thread_alive else f"{now - frame_ts:.1f}s 没有新帧"
        _LOGGER.warning("看门狗触发（%s），重开摄像头 %s", reason, info.index)
        self._restarts += 1
        self._last_restart_ts = now
        try:
            self.open(int(info.index))
        except Exception as exc:
            self._next_reopen_at = time.monotonic() + 5.0
            self._grab_error = str(exc)
            _LOGGER.error("看门狗重开摄像头失败（5s 后重试）: %s", exc)
            return "failed"
        self._grab_error = None
        _LOGGER.info("看门狗重开摄像头成功（累计 %d 次）", self._restarts)
        return "restarted"

    def _reopen(self) -> None:
        """在抓帧线程内原地重开当前设备（全程独占设备锁）。"""
        with self._lock:
            info = self._info
        if info is None:
            return
        if not self._acquire_device():
            return
        try:
            with self._lock:
                capture = self._capture
                self._capture = None
            try:
                if capture is not None:
                    capture.release()
            except Exception:  # pragma: no cover
                pass
            for backend_name, backend_id in _backend_candidates(_cfg(self._config, "backend", default="auto")):
                try:
                    candidate = cv2.VideoCapture(info.index, backend_id)
                except Exception:  # pragma: no cover
                    continue
                if candidate and candidate.isOpened():
                    self._configure_capture(candidate)
                    with self._lock:
                        self._capture = candidate
                        self._consecutive_failures = 0
                    _LOGGER.info("摄像头 %d 重开成功 (backend=%s)", info.index, backend_name)
                    return
                if candidate is not None:
                    candidate.release()
            self._next_reopen_at = time.monotonic() + 3.0
            self._grab_error = "重开失败（设备可能被占用或已掉线）"
            _LOGGER.error("摄像头 %d 重开失败，3s 后重试", info.index)
        finally:
            self._device_lock.release()

    def _encode_jpeg(self, frame: np.ndarray) -> Optional[bytes]:
        image = frame
        provider = self._overlay_provider
        if provider is not None:
            try:
                detections = provider() or []
            except Exception as exc:  # pragma: no cover - 叠加失败不影响预览
                _LOGGER.debug("获取检测框失败: %s", exc)
                detections = []
            if detections:
                try:
                    image = draw_detections(frame.copy(), detections)
                except Exception as exc:  # pragma: no cover
                    _LOGGER.debug("绘制检测框失败: %s", exc)
                    image = frame
        try:
            ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality])
        except Exception as exc:  # pragma: no cover
            self._encode_errors += 1
            _LOGGER.debug("JPEG 编码异常: %s", exc)
            return None
        if not ok:
            self._encode_errors += 1
            return None
        return buffer.tobytes()

    # ------------------------------------------------------------ 帧访问
    def get_frame(self, copy: bool = True) -> Optional[np.ndarray]:
        """获取最新原始帧（推理用，不含检测框）。"""
        with self._lock:
            frame = self._raw_frame
        if frame is None:
            return None
        return frame.copy() if copy else frame

    def get_jpeg(self) -> Optional[bytes]:
        """获取最新 JPEG 预览帧（含检测框，如果有订阅者）。"""
        with self._lock:
            return self._jpeg

    def save_snapshot(self, path: Path, quality: int = 95) -> bool:
        """把当前**原始帧**（不含检测框）存成图片文件，用于采集训练数据。

        返回 False 表示当前没有画面（摄像头没开或还没读到帧）。
        """
        frame = self.get_frame(copy=False)
        if frame is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return bool(cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]))
        except Exception as exc:  # pragma: no cover - 磁盘/权限问题
            _LOGGER.warning("保存抓拍失败 %s: %s", path, exc)
            return False

    def _no_signal_jpeg(self) -> bytes:
        if self._placeholder is None:
            frame = np.full((self._height, self._width, 3), 32, dtype=np.uint8)
            cv2.putText(
                frame,
                "NO SIGNAL - select a camera",
                (40, self._height // 2),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                (190, 190, 190),
                2,
                cv2.LINE_AA,
            )
            self._placeholder = self._encode_jpeg(frame) or b""
        return self._placeholder

    def frames(self) -> Iterator[bytes]:
        """MJPEG 分块生成器：供 GET /api/video/feed 使用。"""
        self._stream_clients += 1
        interval = 1.0 / max(self._stream_fps, 1.0)
        last_served = -1
        _LOGGER.debug("MJPEG 客户端接入，当前客户端数=%d", self._stream_clients)
        try:
            while True:
                with self._lock:
                    jpeg = self._jpeg
                    count = self._frame_count
                if jpeg is None or count == last_served:
                    if jpeg is None:
                        placeholder = self._no_signal_jpeg()
                        yield _MJPEG_CHUNK % (len(placeholder), placeholder)
                    time.sleep(interval)
                    continue
                last_served = count
                payload = bytes(jpeg)
                yield _MJPEG_CHUNK % (len(payload), payload)
                time.sleep(interval)
        except GeneratorExit:  # 客户端断开
            raise
        finally:
            self._stream_clients = max(0, self._stream_clients - 1)
            _LOGGER.debug("MJPEG 客户端断开，当前客户端数=%d", self._stream_clients)

    def __enter__(self) -> "CameraService":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
