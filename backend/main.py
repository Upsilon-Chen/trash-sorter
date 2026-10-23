"""YOLO 垃圾自动识别分类系统 - 后端入口。

启动：
    # 在仓库根目录（trash-sorter/）执行
    python -m backend.main
    # 或
    uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

接口（与总约定一致）：
    GET  /api/cameras            枚举摄像头
    POST /api/camera/select      选择摄像头
    GET  /api/models             列出 backend/models/*.pt
    POST /api/models/select      热切换模型
    POST /api/models/upload      上传 .pt 模型
    GET  /api/serial/ports       扫描串口（CP210x/CH340/ESP32 优先）
    POST /api/serial/connect     连接串口
    POST /api/serial/disconnect  断开串口
    POST /api/detection/start    开始检测
    POST /api/detection/stop     停止检测
    POST /api/sort/manual        手动下发一次分类
    GET  /api/video/feed         MJPEG 预览流
    WS   /ws                     状态 / 检测结果推送

架构说明：
- 推理跑在 pipeline 的后台线程，串口读写在串口服务线程，摄像头抓帧在抓帧线程，
  FastAPI 事件循环只做参数校验、取最新帧与事件转发，因此永远不会被推理阻塞。
- 线程 -> 事件循环的事件通过 EventBus（call_soon_threadsafe + asyncio.Queue）投递，
  再由后台任务广播给所有 WebSocket 连接。
"""
from __future__ import annotations

import asyncio
import ctypes
import faulthandler
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import yaml
from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 兼容两种启动方式：
#   python -m backend.main / uvicorn backend.main:app  -> 包内相对导入
#   python backend/main.py                             -> 把仓库根目录加入 sys.path
# ---------------------------------------------------------------------------
if __package__ in (None, ""):  # pragma: no cover - 直接运行脚本
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.services.annotator import AnnotateError, Annotator
    from backend.services.augmenter import AugmentError, op_specs
    from backend.services.camera import MJPEG_BOUNDARY, CameraError, CameraService
    from backend.services.detector import DetectorError, DetectorService
    from backend.services.pipeline import Pipeline, PipelineError
    from backend.services.serial_esp32 import SerialError, SerialService
else:
    from .services.annotator import AnnotateError, Annotator
    from .services.augmenter import AugmentError, op_specs
    from .services.camera import MJPEG_BOUNDARY, CameraError, CameraService
    from .services.detector import DetectorError, DetectorService
    from .services.pipeline import Pipeline, PipelineError
    from .services.serial_esp32 import SerialError, SerialService

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
CLASSES_PATH = CONFIG_DIR / "classes.json"

REPO_ROOT = BASE_DIR.parent
# 抓拍（采集自己的训练数据）的落盘根目录：training/captures/<类别>/*.jpg
CAPTURE_BASE = REPO_ROOT / "training" / "captures"
# 标注时类别索引的唯一口径：training/data.yaml 的 names 顺序
ANNOTATE_CLASSES_YAML = REPO_ROOT / "training" / "data.yaml"
# 「生成数据集」的输出位置
ANNOTATE_DATASET_DIR = REPO_ROOT / "training" / "datasets" / "captures"
ANNOTATE_DATASET_YAML = REPO_ROOT / "training" / "data-captures.yaml"

LOGGER = logging.getLogger("trash_sorter")

# 原生崩溃（OpenCV 段错误 / 访问违例）默认会让整个进程静默退出，什么日志都不留。
# 打开 faulthandler 后，这类崩溃至少会把当时的 Python 调用栈打到 stderr，便于定位。
faulthandler.enable(all_threads=True)

# settings.yaml 缺项时的兜底默认值
DEFAULT_SETTINGS: dict[str, Any] = {
    "app": {
        "host": "0.0.0.0",
        "port": 8000,
        "cors_origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
        "claim_ttl_seconds": 120,
    },
    "camera": {
        "scan_max_index": 10,
        "backend": "auto",
        "width": 1280,
        "height": 720,
        "fps": 30,
        "buffer_size": 1,
        "warmup_frames": 5,
        "read_retries": 3,
        "reopen_after_failures": 30,
        "min_mean_brightness": 0.5,
        "jpeg_quality": 80,
        "stream_fps": 25,
        "autostart_index": 0,
    },
    "model": {
        "dir": "models",
        "default": None,
        "conf": 0.35,
        "iou": 0.45,
        "imgsz": 640,
        "device": "auto",
        "max_det": 20,
        "half": False,
        "agnostic_nms": False,
        "warmup": True,
        "max_upload_mb": 512,
    },
    "serial": {
        "baudrate": 115200,
        "timeout": 0.5,
        "write_timeout": 1.0,
        "priority_keywords": ["CP210", "CH340", "CH910", "ESP32", "USB-SERIAL", "USB SERIAL", "SILICON LABS", "WCH"],
        "autoconnect": False,
        "ready_timeout": 3.0,
        "allow_any_port": True,
    },
    "detection": {
        "target_fps": 10,
        "cooldown_seconds": 3.0,
        "min_confidence": 0.35,
        "push_interval": 0.2,
        "push_empty": False,
        "sort_enabled": True,
        "max_box_area": 0.6,
        "multi_object": "priority",
        "multi_min_area": 0.01,
        "stable_frames": 3,
    },
}


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并配置：override 覆盖 base，缺失项保留默认值。"""
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def persist_setting(key: str, value: Any, path: Path = SETTINGS_PATH) -> bool:
    """把 settings.yaml 里 `key: xxx` 这一行的值就地替换掉。

    只改这一行，**注释和排版都保留**（不像 yaml.safe_dump 会把注释全丢掉）。
    找不到该键、或文件不可写时返回 False（调用方只当作"没持久化"，不影响运行时生效）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("读取 %s 失败，本次设置只在运行时生效: %s", path, exc)
        return False

    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*:\s*)(\S.*?)?\s*$", re.MULTILINE)
    if not pattern.search(text):
        LOGGER.warning("%s 里没有 %s 这一行，本次设置只在运行时生效", path.name, key)
        return False

    rendered = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True).strip()
    if rendered.endswith("..."):  # yaml 对标量的收尾标记
        rendered = rendered[: -len("...")].strip()
    updated = pattern.sub(lambda match: f"{match.group(1)}{rendered}", text, count=1)
    if updated == text:
        return True
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("写入 %s 失败，本次设置只在运行时生效: %s", path, exc)
        return False
    LOGGER.info("%s 已更新为 %s（已写回配置文件）", key, rendered)
    return True


def load_settings(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    """读取 settings.yaml，并与默认值合并。"""
    if not path.is_file():
        LOGGER.warning("未找到 %s，使用内置默认配置", path)
        return deep_merge(DEFAULT_SETTINGS, {})
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except Exception as exc:
        raise RuntimeError(f"解析配置文件失败 {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"配置文件格式错误（应为映射）: {path}")
    return deep_merge(DEFAULT_SETTINGS, loaded)


def load_classes(path: Path = CLASSES_PATH) -> dict[str, Any]:
    """读取 classes.json（类别 -> 槽位/角度映射）。"""
    if not path.is_file():
        LOGGER.warning("未找到 %s，类别映射为空（全部走默认槽位）", path)
        return {"classes": {}, "default": {"slot": 0, "angle": 90, "label_zh": "未知"}}
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except Exception as exc:
        raise RuntimeError(f"解析类别配置失败 {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"类别配置格式错误（应为对象）: {path}")
    loaded.setdefault("classes", {})
    loaded.setdefault("default", {"slot": 0, "angle": 90, "label_zh": "未知", "cooldown": None})
    return loaded


# ---------------------------------------------------------------------------
# 线程安全的 WS 事件总线
# ---------------------------------------------------------------------------
class EventBus:
    """把后台线程产生的事件安全地送到 asyncio 事件循环。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int = 512) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)

    def emit(self, event: dict[str, Any]) -> None:
        """线程安全：可从任意线程/协程调用。"""
        try:
            self._loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:  # 事件循环已关闭
            pass

    def _enqueue(self, event: dict[str, Any]) -> None:
        if self._queue.full():  # 丢弃最旧事件，避免阻塞生产者
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover
            pass

    async def pump(self, manager: "ConnectionManager") -> None:
        """后台任务：持续把事件广播给所有 WebSocket 客户端。

        逐条兜底：任何一条广播失败都不能让这个任务退出，否则前端会出现
        "WS 显示已连接、但再也收不到任何数据"的假死状态。
        """
        while True:
            try:
                event = await self._queue.get()
                await manager.broadcast(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 广播失败不应中断
                LOGGER.exception("广播事件失败（已跳过该条）: %s", exc)
                await asyncio.sleep(0.05)


class ConnectionManager:
    """WebSocket 连接管理。"""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    @staticmethod
    def _encode(event: dict[str, Any]) -> str:
        return json.dumps(event, ensure_ascii=False, default=str)

    async def send(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        await websocket.send_text(self._encode(event))

    async def broadcast(self, event: dict[str, Any]) -> None:
        if not self._connections:
            return
        text = self._encode(event)
        async with self._lock:
            targets = list(self._connections)
        dead: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_text(text)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            await self.disconnect(websocket)


class AppServices:
    """应用级服务容器（挂在 app.state.services 上）。"""

    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}
        self.classes: dict[str, Any] = {}
        self.camera: CameraService
        self.detector: DetectorService
        self.serial: SerialService
        self.pipeline: Pipeline
        self.annotator: Annotator
        self.bus: EventBus
        self.manager: ConnectionManager
        self.tasks: list[asyncio.Task[Any]] = []
        self.pump_task: Optional[asyncio.Task[Any]] = None


async def supervise(services: AppServices) -> None:
    """看门狗：保证事件泵、摄像头抓帧、检测线程不会悄悄停住。

    长时间运行或电脑休眠唤醒后，最容易失效的是摄像头句柄——表现为
    "接口还能访问、界面还在，但画面和检测都不更新了"。这里每几秒体检一次，
    能自愈的一律自愈，修不好的降频重试，绝不静默卡死。
    """
    interval = float(services.settings.get("app", {}).get("watchdog_interval", 5.0) or 5.0)
    stale = float(services.settings.get("camera", {}).get("stale_frame_seconds", 6.0) or 6.0)
    while True:
        await asyncio.sleep(interval)
        try:
            # 1) 事件泵（WS 广播）：死了就重启
            task = services.pump_task
            if task is None or task.done():
                detail = ""
                if task is not None and not task.cancelled():
                    exc = task.exception()
                    detail = f"（{exc!r}）" if exc else ""
                services.pump_task = asyncio.create_task(
                    services.bus.pump(services.manager), name="ws-pump"
                )
                services.tasks.append(services.pump_task)
                LOGGER.warning("看门狗：事件泵已退出%s，已重启", detail)

            # 2) 摄像头：抓帧线程死了、或长时间没有新帧（休眠唤醒 / USB 掉线）就整机重开
            camera_result = await asyncio.to_thread(services.camera.ensure_healthy, stale)
            if camera_result in ("restarted", "failed"):
                LOGGER.warning("看门狗：摄像头 %s", camera_result)
                services.bus.emit({"type": "camera_state", "data": services.camera.status()})

            # 3) 检测线程：用户开着却意外退出了就重启
            detection_result = await asyncio.to_thread(services.pipeline.ensure_running)
            if detection_result == "restarted":
                LOGGER.warning("看门狗：检测线程已重启")
                services.bus.emit({"type": "detection_state", "data": services.pipeline.snapshot()})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 看门狗自己绝不能被某一轮异常打死
            LOGGER.exception("看门狗本轮异常（已忽略，下轮继续）: %s", exc)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
def quiet_client_disconnects() -> None:
    """把"客户端被强杀"造成的 asyncio 噪音降下来。

    Windows 上 WebSocket / MJPEG 客户端突然消失时，Proactor 传输层会抛
    ConnectionResetError(WinError 10054) 并打一整段 ERROR 堆栈 —— 看着吓人，
    其实是正常现象（关窗口、强杀进程都会这样）。这里只过滤这一类，其它异常照旧上报。
    """
    loop = asyncio.get_running_loop()
    default_handler = loop.get_exception_handler()

    def handler(current_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exception = context.get("exception")
        handle = str(context.get("handle") or "")
        noisy = isinstance(exception, (ConnectionResetError, ConnectionAbortedError)) or "connection_lost" in handle
        if noisy:
            LOGGER.debug("忽略客户端断开噪音：%s", exception or handle)
            return
        if default_handler is not None:
            default_handler(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


def start_parent_watchdog() -> None:
    """启动器把它的 PID 通过环境变量传进来；启动器一消失，后端也跟着退出。

    为什么需要：正常关窗时启动器会主动结束后端（`tools/desktop.py`），但如果启动器
    被任务管理器强杀、自己崩了、或者调试用的控制台被关掉，后端就会变成**孤儿进程**
    —— 一直占着摄像头，下次启动就打不开了。这里每 2 秒确认一次"父亲"还在不在。
    """
    raw = os.environ.get("TRASH_SORTER_PARENT_PID")
    if not raw:
        return
    try:
        parent_pid = int(raw)
    except ValueError:
        LOGGER.warning("TRASH_SORTER_PARENT_PID=%r 不是合法 PID，忽略", raw)
        return

    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102

        def parent_alive() -> bool:
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
            if not handle:
                return False
            try:
                # 还在跑 -> WAIT_TIMEOUT；已退出 -> WAIT_OBJECT_0
                return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)

    else:  # pragma: no cover - 非 Windows 走信号 0 探测
        def parent_alive() -> bool:
            try:
                os.kill(parent_pid, 0)
                return True
            except OSError:
                return False

    def watch() -> None:
        while True:
            time.sleep(2.0)
            if not parent_alive():
                LOGGER.warning("启动器进程 %d 已退出，后端随之退出（避免孤儿进程占着摄像头）", parent_pid)
                logging.shutdown()
                os._exit(0)  # 直接退出：确保不会卡在相机线程的收尾上，句柄由系统回收

    threading.Thread(target=watch, name="parent-watchdog", daemon=True).start()
    LOGGER.info("已启用启动器看护：启动器 PID=%d（它退出则后端退出）", parent_pid)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=os.environ.get("TRASH_SORTER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    start_parent_watchdog()
    quiet_client_disconnects()

    # 关键修复：默认线程池只有 min(32, CPU+4) 个线程（本机约 8~12 个），而
    #   · /api/video/feed 这类长连接 MJPEG 流
    #   · 摄像头探测（dshow 单次 3 秒起，异常设备可达数分钟）
    # 都会长期占用 to_thread 的线程。池子一满，所有走 to_thread 的接口就一起排队、
    # 永远不出结果 —— 现象就是"程序跑一会儿后所有列表都在刷新但没结果"。
    # 这里显式放大到 64，并给线程起名字方便排查。
    from concurrent.futures import ThreadPoolExecutor

    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=64, thread_name_prefix="blocking")
    )
    LOGGER.info("阻塞调用线程池已放大到 64 个线程")

    settings = load_settings()
    classes = load_classes()

    services = AppServices()
    services.settings = settings
    services.classes = classes
    services.manager = ConnectionManager()
    services.bus = EventBus(asyncio.get_running_loop())

    model_dir_setting = str(settings["model"].get("dir") or "models")
    model_dir = Path(model_dir_setting)
    if not model_dir.is_absolute():
        model_dir = (BASE_DIR / model_dir).resolve()

    services.camera = CameraService(settings.get("camera", {}))
    services.detector = DetectorService(model_dir, settings.get("model", {}))
    services.serial = SerialService(settings.get("serial", {}), on_event=services.bus.emit)
    services.pipeline = Pipeline(
        services.camera,
        services.detector,
        services.serial,
        settings=settings,
        classes=classes,
        emit=_emit_with_records,
    )
    services.camera.set_overlay_provider(services.pipeline.get_overlay)
    services.annotator = Annotator(
        CAPTURE_BASE,
        ANNOTATE_CLASSES_YAML,
        services.pipeline.class_table(),
        claim_ttl_seconds=float((settings.get("app") or {}).get("claim_ttl_seconds") or 120.0),
    )
    app.state.services = services

    services.pump_task = asyncio.create_task(services.bus.pump(services.manager), name="ws-pump")
    services.tasks.append(services.pump_task)
    services.tasks.append(asyncio.create_task(supervise(services), name="watchdog"))

    LOGGER.info("后端启动中：模型目录=%s，类别数=%d", model_dir, len(services.pipeline.class_table()))

    # --- 重活全部丢到后台，先让 uvicorn 开始监听 ---
    # 为什么要这样：模型加载（导入 torch + 加载权重 + 预热）和打开摄像头加起来要十几秒，
    # 以前是在这里 await 的，导致 uvicorn 在全部干完前不 accept 请求 —— 前端窗口只能空等。
    # 现在 lifespan 立刻返回，界面 1~2 秒就能出来，模型/摄像头在后台加载，
    # 前端本来就会显示"模型未加载"并轮询 /api/models，加载完自动变"就绪"。
    async def warm_up() -> None:
        default_model = services.detector.pick_default_model()
        if default_model:
            try:
                await asyncio.to_thread(services.detector.load_model, default_model)
            except Exception as exc:
                LOGGER.warning("自动加载模型失败（可通过 /api/models/select 手动切换）: %s", exc)
        else:
            LOGGER.warning("模型目录中没有 .pt 文件：%s", model_dir)

        autostart_index = settings["camera"].get("autostart_index")
        if autostart_index is not None:
            try:
                await asyncio.to_thread(services.camera.open, int(autostart_index))
            except Exception as exc:
                LOGGER.warning("自动打开摄像头 %s 失败（可在前端手动选择）: %s", autostart_index, exc)

        if settings["serial"].get("autoconnect"):
            try:
                await asyncio.to_thread(services.serial.connect, None, None)
            except Exception as exc:
                LOGGER.warning("自动连接串口失败（可在前端手动选择）: %s", exc)
        LOGGER.info("后台预热完成（模型 / 摄像头 / 串口）")

    services.tasks.append(asyncio.create_task(warm_up(), name="warm-up"))

    async def log_resources() -> None:
        """每 30 秒记录一次活跃线程数。

        线程数持续上涨 = 有东西在泄漏线程（长连接流/阻塞调用），
        这是"接口突然全都不返回"的前兆，留着这条日志下次能一眼看出来。
        """
        while True:
            await asyncio.sleep(30.0)
            LOGGER.info("运行状态：活跃线程 %d 个（线程池 64）", threading.active_count())

    services.tasks.append(asyncio.create_task(log_resources(), name="resource-log"))
    async def cleanup_records() -> None:
        """每 6 小时清理一次过期记录（保留天数见 settings.local.yaml 的 records.retention_days）。"""
        await asyncio.sleep(120)
        while True:
            try:
                from .db.session import _read_yaml, CONFIG_DIR as _CFG
                from .records_api import cleanup_older_than

                cfg = _read_yaml(_CFG / "settings.local.yaml").get("records") or {}
                days = int(cfg.get("retention_days", 30) or 30)
                removed = await asyncio.to_thread(cleanup_older_than, days)
                if removed:
                    LOGGER.info("分拣记录自动清理：删除 %s 天前的 %d 条", days, removed)
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("自动清理跳过：%s", exc)
            await asyncio.sleep(6 * 3600)

    services.tasks.append(asyncio.create_task(cleanup_records(), name="cleanup-records"))
    # ---- 分拣记录（MySQL）：数据库不可用时自动降级，不影响主流程 ----
    global _RECORDER, _EMIT_INNER
    try:
        from . import db as _db
        from .services.recorder import RecordRecorder

        _EMIT_INNER = services.bus.emit
        _db.session.init()
        _RECORDER = RecordRecorder(
            services.camera,
            model_name_fn=lambda: (services.detector.status() or {}).get("current"),
        )
        _RECORDER.start()
        app.state.recorder = _RECORDER
        # 路由在模块尾部注册（lifespan 内注册不生效）
        LOGGER.info(
            "分拣记录已接入：数据库 %s", "可用" if _db.session.available() else "不可用（已降级）"
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("分拣记录模块初始化失败（主流程不受影响）: %s", exc)

    LOGGER.info("后端就绪，等待前端连接")
    try:
        yield
    finally:
        LOGGER.info("后端关闭中 ...")
        for task in services.tasks:
            task.cancel()
        for task in services.tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - 关闭阶段吞掉所有异常
                pass
        services.tasks.clear()
        try:
            services.pipeline.stop()
        except Exception:  # pragma: no cover
            pass
        try:
            services.camera.close()
        except Exception:  # pragma: no cover
            pass
        try:
            services.serial.disconnect()
        except Exception:  # pragma: no cover
            pass
        LOGGER.info("后端已关闭")


app = FastAPI(title="YOLO 垃圾自动识别分类系统", version="1.0.0", lifespan=lifespan)
# ---------------------------------------------------------------------------
# 分拣记录（MySQL）—— 必须在 app 创建后立刻注册，
# 且要早于后面的 app.mount("/", StaticFiles) 挂载，否则请求会被静态资源吃掉。
# ---------------------------------------------------------------------------
try:
    from .records_api import router as _records_router

    app.include_router(_records_router)
    logging.getLogger("trash_sorter").info("分拣记录接口已注册（9 个路由）")
except Exception as _exc:  # noqa: BLE001
    logging.getLogger("trash_sorter").warning("分拣记录接口注册失败（不影响主流程）: %s", _exc)

try:
    _app_settings: dict[str, Any] = load_settings() if SETTINGS_PATH.is_file() else deep_merge(DEFAULT_SETTINGS, {})
except Exception as _exc:  # 配置损坏时不允许启动失败到无法访问的程度
    LOGGER.warning("读取 settings.yaml 失败，改用默认配置: %s", _exc)
    _app_settings = deep_merge(DEFAULT_SETTINGS, {})
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_app_settings.get("app", {}).get("cors_origins") or ["*"]),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def services_of(request: Request) -> AppServices:
    return request.app.state.services


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class CameraSelectRequest(BaseModel):
    index: int = Field(ge=0, le=64, description="摄像头序号（OpenCV device index）")


class ModelSelectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="backend/models 下的 .pt 文件名")


class SerialConnectRequest(BaseModel):
    port: Optional[str] = Field(default=None, description="留空则自动挑选优先级最高的端口")
    baudrate: Optional[int] = Field(default=None, ge=300, le=4000000)


class DetectionStartRequest(BaseModel):
    model: Optional[str] = Field(default=None, description="可选：开始前先热切换到该模型")
    reset_stats: bool = Field(default=False, description="是否重置统计与冷却记录")
    conf: Optional[float] = Field(
        default=None,
        ge=0.01,
        le=0.99,
        description="本次检测的置信度阈值，覆盖 settings.yaml 的 detection.min_confidence",
    )


class MultiObjectRequest(BaseModel):
    """多目标策略：一帧里同时出现多个不同类别的垃圾时倒哪一个。"""

    policy: str = Field(
        min_length=1,
        max_length=16,
        description="priority（按桶优先级）/ largest（最大件）/ confidence（置信度最高）/ hold（不下发）",
    )


class ManualSortRequest(BaseModel):
    class_name: Optional[str] = Field(default=None, max_length=64)
    slot: Optional[int] = Field(default=None, ge=0, le=15)
    angle: Optional[int] = Field(default=None, ge=0, le=270, description="仅随协议下发，固件不使用")
    force: bool = Field(default=True, description="是否忽略冷却时间")
    h: Optional[int] = Field(default=None, ge=0, le=270, description="水平舵机角度覆盖，固件优先使用")
    v: Optional[int] = Field(default=None, ge=0, le=180, description="竖直舵机角度覆盖，固件优先使用")


class CaptureRequest(BaseModel):
    """抓拍一张训练图。class_name 用 "background" 表示空托盘背景图。"""

    class_name: str = Field(min_length=1, max_length=32)
    quality: int = Field(default=95, ge=50, le=100, description="JPEG 质量")


class AnnotateBox(BaseModel):
    """标注框，坐标是相对整图的 0~1 归一化值。"""

    class_name: str = Field(min_length=1, max_length=32)
    x1: float
    y1: float
    x2: float
    y2: float


class AnnotateSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="类别/文件名，如 paper/paper_20261014.jpg")
    boxes: list[AnnotateBox] = Field(default_factory=list, description="传空列表等于清空该图标注")


class AnnotateBuildRequest(BaseModel):
    val_ratio: float = Field(default=0.2, ge=0.0, le=0.5, description="验证集比例")
    include_background: bool = Field(default=True, description="background 目录里没标注的图当背景样本")


class ClaimRequest(BaseModel):
    """认领/释放一张图片，避免两个标注员同时标同一张。"""

    name: str = Field(min_length=1, max_length=255, description="类别/文件名")
    client_id: str = Field(min_length=1, max_length=64, description="浏览器标签页的随机 ID")


class AugmentRequest(BaseModel):
    """对已标注图片做增强（旋转/翻转/明暗/噪点），标注同步变换。"""

    ops: list[str] = Field(min_length=1, description="增强方式列表，见 GET /api/augment/ops")
    classes: Optional[list[str]] = Field(default=None, description="只处理这些类别目录；不传=全部")
    names: Optional[list[str]] = Field(default=None, description="只处理这几张图（单张增强用）")
    only_labeled: bool = Field(default=True, description="只处理已标注的图（背景图默认跳过）")
    include_background: bool = Field(default=False, description="是否也增强 background 目录（机位固定，不建议）")
    overwrite: bool = Field(default=False, description="已存在同名增强图时是否覆盖")
    limit: Optional[int] = Field(default=None, ge=1, le=5000, description="最多处理多少张源图（试跑用）")


class AugmentCleanRequest(BaseModel):
    """删除增强产物（只删带增强后缀的文件，不动原图）。"""

    classes: Optional[list[str]] = Field(default=None, description="只清理这些类别；不传=全部")
    ops: Optional[list[str]] = Field(default=None, description="只清理这些增强方式；不传=全部")
    dry_run: bool = Field(default=False, description="true = 只统计不删除")


class ReferenceRequest(BaseModel):
    """设置背景差分用的参考图（空托盘）。"""

    name: Optional[str] = Field(
        default=None,
        max_length=255,
        description="用这张已有抓拍图当参考图；不传则抓当前摄像头画面",
    )
    frames: int = Field(
        default=8,
        ge=1,
        le=30,
        description="抓多少帧取逐像素中值（消除传感器噪声，强烈建议 >=4）；1 = 直接存单帧",
    )


class AutoAnnotateRequest(BaseModel):
    """背景差分自动标注。all=true 时批量写盘，否则只返回建议框。"""

    name: Optional[str] = Field(default=None, max_length=255, description="单张图片名")
    all: bool = Field(default=False, description="批量处理所有（未标注的）图片并直接写盘")
    class_name: Optional[str] = Field(default=None, max_length=32, description="不传则用抓拍目录名")
    threshold: int = Field(default=18, ge=1, le=120, description="噪声下限（绝对灰度差）")
    sigma_k: float = Field(
        default=8.0, ge=1.0, le=20.0, description="自适应灵敏度：实际阈值 = k × 实测噪声σ，越大越严格"
    )
    min_fill: float = Field(default=0.35, ge=0.0, le=1.0, description="轮廓实心度下限，滤掉细碎噪点")
    blur: int = Field(default=5, ge=1, le=15, description="差分前的高斯模糊核（会取奇数）")
    min_area: float = Field(default=0.005, ge=0.0, le=0.5, description="连通区域最小面积（占整图）")
    max_area: float = Field(default=0.6, ge=0.0, le=1.0, description="连通区域最大面积")
    dilate: int = Field(default=1, ge=0, le=10, description="膨胀次数，用于把碎片连起来")
    padding: float = Field(default=0.01, ge=0.0, le=0.2, description="框外扩比例")
    only_unlabeled: bool = Field(default=True, description="批量时只处理未标注的图")
    overwrite: bool = Field(default=False, description="批量时是否覆盖已有标注")


# ---------------------------------------------------------------------------
# 摄像头接口
# ---------------------------------------------------------------------------
@app.get("/api/cameras")
async def list_cameras(request: Request, force: bool = False) -> dict[str, Any]:
    """枚举 0 ~ scan_max_index 号摄像头；能真正读到帧才算可用。

    `?force=1` 绕过缓存做一次真实探测（给「刷新列表」按钮用）；
    普通轮询走缓存，避免每次刷新都卡在几秒到几分钟的设备探测上。
    """
    services = services_of(request)
    try:
        cameras = await asyncio.to_thread(
            services.camera.list_cameras, 0.0 if force else None
        )
    except CameraError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"枚举摄像头失败: {exc}") from exc

    items = [camera.to_dict() for camera in cameras]
    current = services.camera.current_index
    # 正在使用的设备可能被本进程占用而枚举不到，补进列表
    if current is not None and all(item["index"] != current for item in items):
        info = services.camera.status().get("camera")
        if info:
            items.insert(0, info)
    return {"ok": True, "cameras": items, "current": current, "count": len(items)}


@app.post("/api/camera/select")
async def select_camera(payload: CameraSelectRequest, request: Request) -> dict[str, Any]:
    """选择并打开摄像头（由后端 OpenCV 控制）。"""
    services = services_of(request)
    try:
        info = await asyncio.to_thread(services.camera.open, payload.index)
    except CameraError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打开摄像头失败: {exc}") from exc

    services.bus.emit({"type": "camera_state", "data": services.camera.status()})
    return {"ok": True, "camera": info.to_dict(), "state": services.camera.status()}


# ---------------------------------------------------------------------------
# 模型接口
# ---------------------------------------------------------------------------
@app.get("/api/models")
async def list_models(request: Request) -> dict[str, Any]:
    """列出 backend/models/*.pt。"""
    services = services_of(request)
    try:
        models = services.detector.list_models()
    except DetectorError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "ok": True,
        "models": models,
        "current": services.detector.current_model,
        "model_dir": str(services.detector.model_dir),
        "info": services.detector.model_info(),
    }


@app.post("/api/models/select")
async def select_model(payload: ModelSelectRequest, request: Request) -> dict[str, Any]:
    """热切换模型（加载完成后新模型立即生效）。"""
    services = services_of(request)
    name = Path(payload.name).name
    if not name.lower().endswith(".pt"):
        raise HTTPException(status_code=400, detail="只支持 .pt 模型文件")
    try:
        info = await asyncio.to_thread(services.detector.load_model, name)
    except DetectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"加载模型失败: {exc}") from exc

    services.bus.emit({"type": "model_state", "data": info})
    return {"ok": True, "model": info}


@app.post("/api/models/upload")
async def upload_model(
    request: Request,
    file: UploadFile = File(..., description="YOLO 权重文件（*.pt）"),
    overwrite: bool = Query(default=False, description="同名文件是否覆盖"),
) -> dict[str, Any]:
    """上传 .pt 模型到 backend/models/。"""
    services = services_of(request)
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    if not filename.lower().endswith(".pt"):
        raise HTTPException(status_code=400, detail="只允许上传 .pt 文件")

    detector = services.detector
    try:
        target = detector.resolve_model_path(filename)
    except DetectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if target.exists() and not overwrite:
        raise HTTPException(status_code=409, detail=f"模型 {filename} 已存在（可用 overwrite=true 覆盖）")

    max_bytes = int(services.settings["model"].get("max_upload_mb", 512)) * 1024 * 1024
    temp_path = target.with_suffix(target.suffix + ".part")
    written = 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过上限 {max_bytes // (1024 * 1024)} MB",
                    )
                handle.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="上传内容为空")
        os.replace(temp_path, target)
    except HTTPException:
        temp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"保存模型失败: {exc}") from exc
    finally:
        await file.close()

    LOGGER.info("模型已上传: %s (%.2f MB)", target.name, written / (1024 * 1024))
    models = detector.list_models()
    uploaded = next((item for item in models if item["name"] == target.name), None)
    services.bus.emit({"type": "models_changed", "data": {"models": models}})
    return {"ok": True, "uploaded": uploaded, "models": models, "size_mb": round(written / (1024 * 1024), 2)}


# ---------------------------------------------------------------------------
# 串口接口
# ---------------------------------------------------------------------------
@app.get("/api/serial/ports")
async def list_serial_ports(request: Request) -> dict[str, Any]:
    """扫描串口，CP210x / CH340 / ESP32 / USB-SERIAL 排在最前。"""
    services = services_of(request)
    try:
        ports = await asyncio.to_thread(services.serial.list_ports)
    except SerialError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    state = services.serial.status()
    return {
        "ok": True,
        "ports": [port.to_dict() for port in ports],
        "count": len(ports),
        "connected": state["connected"],
        "connection": state["connection"],
        "state": state,
    }


@app.post("/api/serial/connect")
async def connect_serial(payload: SerialConnectRequest, request: Request) -> dict[str, Any]:
    """连接串口；port 为空时自动挑选优先级最高的端口。"""
    services = services_of(request)
    try:
        state = await asyncio.to_thread(services.serial.connect, payload.port, payload.baudrate)
    except SerialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"连接串口失败: {exc}") from exc
    return {"ok": True, "state": state, "connection": state["connection"]}


@app.post("/api/serial/disconnect")
async def disconnect_serial(request: Request) -> dict[str, Any]:
    """断开串口。"""
    services = services_of(request)
    try:
        state = await asyncio.to_thread(services.serial.disconnect)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"断开串口失败: {exc}") from exc
    return {"ok": True, "state": state, "connection": state["connection"]}


# ---------------------------------------------------------------------------
# 检测接口
# ---------------------------------------------------------------------------
@app.post("/api/detection/start")
async def start_detection(payload: DetectionStartRequest, request: Request) -> dict[str, Any]:
    """开始检测（后台线程推理，不阻塞接口）。"""
    services = services_of(request)
    if payload.model:
        try:
            await asyncio.to_thread(services.detector.load_model, Path(payload.model).name)
        except DetectorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        snapshot = await asyncio.to_thread(services.pipeline.start, payload.reset_stats, payload.conf)
    except PipelineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"启动检测失败: {exc}") from exc
    return {"ok": True, "detection": snapshot}


@app.post("/api/detection/stop")
async def stop_detection(request: Request) -> dict[str, Any]:
    """停止检测。"""
    services = services_of(request)
    try:
        snapshot = await asyncio.to_thread(services.pipeline.stop)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"停止检测失败: {exc}") from exc
    return {"ok": True, "detection": snapshot}


@app.post("/api/detection/multi-object")
async def set_multi_object(payload: MultiObjectRequest, request: Request) -> dict[str, Any]:
    """切换「一帧里同时出现多个不同类别垃圾」时的处理策略。

    立即生效，并写回 backend/config/settings.yaml（重启后仍然保持）。
    """
    services = services_of(request)
    policy = payload.policy.strip().lower()
    try:
        snapshot = await asyncio.to_thread(services.pipeline.set_multi_policy, policy)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(services.settings.get("detection"), dict):
        services.settings["detection"]["multi_object"] = policy
    persisted = await asyncio.to_thread(persist_setting, "multi_object", policy)
    return {"ok": True, "policy": policy, "persisted": persisted, "detection": snapshot}


@app.post("/api/sort/manual")
async def manual_sort(payload: ManualSortRequest, request: Request) -> dict[str, Any]:
    """手动下发一次分类指令（用于调试舵机或人工分拣）。"""
    services = services_of(request)
    if not services.serial.is_connected:
        raise HTTPException(status_code=409, detail="串口未连接，无法下发分类指令")
    try:
        outcome = await asyncio.to_thread(
            services.pipeline.manual_sort,
            payload.class_name,
            payload.slot,
            payload.angle,
            payload.force,
            payload.h,
            payload.v,
        )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"下发失败: {exc}") from exc
    return {"ok": True, "result": outcome}


# ---------------------------------------------------------------------------
# 抓拍：用实机摄像头采集自己的训练数据
# ---------------------------------------------------------------------------
def capture_counts() -> dict[str, int]:
    """统计各抓拍类别已有的图片数量。"""
    counts: dict[str, int] = {}
    if not CAPTURE_BASE.is_dir():
        return counts
    for sub in sorted(CAPTURE_BASE.iterdir()):
        if not sub.is_dir():
            continue
        counts[sub.name] = sum(
            1
            for path in sub.iterdir()
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
        )
    return counts


@app.get("/api/captures")
async def list_captures() -> dict[str, Any]:
    """查看已抓拍数量（按类别），前端用来显示进度。"""
    counts = await asyncio.to_thread(capture_counts)
    return {
        "ok": True,
        "base": str(CAPTURE_BASE),
        "counts": counts,
        "total": sum(counts.values()),
    }


@app.post("/api/capture")
async def capture_frame(payload: CaptureRequest, request: Request) -> dict[str, Any]:
    """把当前摄像头**原始帧**（不含检测框）存成训练用图片。

    类别取 classes.json 里的 6 类，外加 background（空托盘背景图）。
    文件名形如 paper_20261014_193012_345.jpg，纯 ASCII。
    """
    services = services_of(request)
    name = payload.class_name.strip()

    valid = {"background"} | set(services.pipeline.class_table().keys())
    if name not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"未知的抓拍类别 {name!r}；可用：{'、'.join(sorted(valid))}",
        )

    target_dir = CAPTURE_BASE / name
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = target_dir / f"{name}_{stamp}.jpg"

    try:
        saved = await asyncio.to_thread(services.camera.save_snapshot, path, payload.quality)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"抓拍失败：{exc}") from exc
    if not saved:
        raise HTTPException(
            status_code=409,
            detail="没有可抓拍的画面——请先打开摄像头并保持预览（检测是否开启都可以）",
        )

    counts = await asyncio.to_thread(capture_counts)
    LOGGER.info("抓拍：%s -> %s", name, path.name)
    return {
        "ok": True,
        "class_name": name,
        "name": f"{name}/{path.name}",
        "file": str(path),
        "count": counts.get(name, 0),
        "total": sum(counts.values()),
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# 数据标注：给抓拍图打框，并导出成可训练数据集
# ---------------------------------------------------------------------------
@app.get("/api/annotate/images")
async def annotate_images(request: Request) -> dict[str, Any]:
    """列出所有抓拍图及其标注状态（供标注页左侧列表使用）。"""
    services = services_of(request)
    try:
        images = await asyncio.to_thread(services.annotator.list_images)
        classes = await asyncio.to_thread(services.annotator.class_list)
    except AnnotateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    labeled = sum(1 for item in images if item["labeled"])
    return {
        "ok": True,
        "base": str(services.annotator.base),
        "classes": classes,
        "images": images,
        "total": len(images),
        "labeled": labeled,
        "unlabeled": len(images) - labeled,
    }


@app.get("/api/annotate/image")
async def annotate_image(request: Request, name: str = Query(..., min_length=1)) -> FileResponse:
    """取抓拍图原图给前端画框用。"""
    services = services_of(request)
    try:
        path = services.annotator.resolve_image(name)
    except AnnotateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/api/annotate/labels")
async def annotate_labels(request: Request, name: str = Query(..., min_length=1)) -> dict[str, Any]:
    """读某张图已有的标注框。"""
    services = services_of(request)
    try:
        boxes = await asyncio.to_thread(services.annotator.read_labels, name)
    except AnnotateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "name": name, "boxes": boxes, "count": len(boxes)}


@app.post("/api/annotate/labels")
async def annotate_save(payload: AnnotateSaveRequest, request: Request) -> dict[str, Any]:
    """保存某张图的标注（覆盖写，传空列表即清空）。"""
    services = services_of(request)
    try:
        result = await asyncio.to_thread(
            services.annotator.write_labels,
            payload.name,
            [box.model_dump() for box in payload.boxes],
        )
    except AnnotateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "name": payload.name, **result}


@app.delete("/api/annotate/image")
async def annotate_delete_image(request: Request, name: str = Query(..., min_length=1)) -> dict[str, Any]:
    """删除一张抓拍图及其同名标签（不可恢复）。"""
    services = services_of(request)
    try:
        result = await asyncio.to_thread(services.annotator.delete_image, name)
    except AnnotateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/annotate/claim")
async def annotate_claim(payload: ClaimRequest, request: Request) -> dict[str, Any]:
    """认领一张图片（打开前调用）。被另一位标注员占用时返回 ok=false。

    目的是让两个人在同一份数据上协作时不会同时标同一张、
    避免后保存的覆盖先保存的。
    """
    services = services_of(request)
    try:
        result = await asyncio.to_thread(services.annotator.claim_image, payload.name, payload.client_id)
    except AnnotateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "ttl": services.annotator.claim_ttl}


@app.post("/api/annotate/release")
async def annotate_release(payload: ClaimRequest, request: Request) -> dict[str, Any]:
    """释放认领（切图 / 离开页面时调用）。"""
    services = services_of(request)
    result = await asyncio.to_thread(services.annotator.release_image, payload.name, payload.client_id)
    return {"ok": True, **result}


@app.get("/api/annotate/claims")
async def annotate_claims(request: Request) -> dict[str, Any]:
    """当前所有图片的占用情况：{图片名: client_id}。"""
    services = services_of(request)
    claims = await asyncio.to_thread(services.annotator.claims)
    return {"ok": True, "claims": claims, "count": len(claims), "ttl": services.annotator.claim_ttl}


# ---------------------------------------------------------------------------
# 数据增强：给已标注图生成旋转/翻转/明暗副本，标注同步变换
# ---------------------------------------------------------------------------
@app.get("/api/augment/ops")
async def augment_ops() -> dict[str, Any]:
    """可用的增强方式（前端据此渲染选项，改 augmenter.py 里的表即可扩展）。"""
    return {"ok": True, "ops": op_specs()}


@app.post("/api/augment/apply")
async def augment_apply(payload: AugmentRequest, request: Request) -> dict[str, Any]:
    """生成增强副本（不覆盖原图）。

    产物与原图**同目录**、文件名加后缀（`paper_x.jpg` -> `paper_x_r90.jpg`），
    所以会直接出现在标注列表里、也能被「生成数据集」自动带上。
    """
    services = services_of(request)
    try:
        result = await asyncio.to_thread(
            services.annotator.augment_images,
            payload.ops,
            payload.classes,
            payload.names,
            payload.only_labeled,
            payload.include_background,
            payload.overwrite,
            payload.limit,
        )
    except (AnnotateError, AugmentError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/augment/clean")
async def augment_clean(payload: AugmentCleanRequest, request: Request) -> dict[str, Any]:
    """删除增强产物（连同同名标签）；`dry_run=true` 只统计不删除。**不会动原图。**"""
    services = services_of(request)
    try:
        result = await asyncio.to_thread(
            services.annotator.delete_augmented, payload.classes, payload.ops, payload.dry_run
        )
    except (AnnotateError, AugmentError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/annotate/build")
async def annotate_build(payload: AnnotateBuildRequest, request: Request) -> dict[str, Any]:
    """把已标注的抓拍图导出成 YOLO 数据集 + data.yaml。"""
    services = services_of(request)
    try:
        result = await asyncio.to_thread(
            services.annotator.build_dataset,
            ANNOTATE_DATASET_DIR,
            ANNOTATE_DATASET_YAML,
            payload.val_ratio,
            payload.include_background,
        )
    except AnnotateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "dataset": result}


@app.get("/api/annotate/reference")
async def annotate_reference(request: Request) -> dict[str, Any]:
    """查看当前的空托盘参考图（背景差分自动标注用）。"""
    services = services_of(request)
    info = await asyncio.to_thread(services.annotator.reference_info)
    return {"ok": True, "reference": info, "url": "/api/annotate/reference/image" if info else None}


@app.get("/api/annotate/reference/image")
async def annotate_reference_image(request: Request) -> FileResponse:
    """参考图原图，前端用来显示缩略图。"""
    services = services_of(request)
    path = services.annotator.reference_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="还没有参考图")
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.post("/api/annotate/reference")
async def annotate_set_reference(payload: ReferenceRequest, request: Request) -> dict[str, Any]:
    """设置参考图：传 name 用已有抓拍图，不传则抓当前摄像头画面。"""
    services = services_of(request)
    try:
        if payload.name:
            info = await asyncio.to_thread(services.annotator.set_reference_from_image, payload.name)
        else:
            count = max(1, int(payload.frames))
            if count == 1:
                saved = await asyncio.to_thread(
                    services.camera.save_snapshot, services.annotator.reference_path, 95
                )
                if not saved:
                    raise HTTPException(
                        status_code=409,
                        detail="没有可用的摄像头画面——请先打开摄像头，或改为选一张已有的空托盘抓拍图",
                    )
                info = await asyncio.to_thread(services.annotator.reference_info) or {}
            else:
                # 多帧取中值：把传感器噪声消掉，差分才不会满屏小框
                frames: list[Any] = []
                for _ in range(count):
                    frame = await asyncio.to_thread(services.camera.get_frame, True)
                    if frame is not None:
                        frames.append(frame)
                    await asyncio.sleep(0.1)
                if len(frames) < 2:
                    raise HTTPException(
                        status_code=409,
                        detail=f"只抓到 {len(frames)} 帧有效画面，无法合成参考图——请先打开摄像头预览",
                    )
                info = await asyncio.to_thread(services.annotator.set_reference_from_frames, frames)
    except AnnotateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "reference": info, "url": "/api/annotate/reference/image"}


@app.delete("/api/annotate/reference")
async def annotate_clear_reference(request: Request) -> dict[str, Any]:
    """删除参考图。"""
    services = services_of(request)
    removed = await asyncio.to_thread(services.annotator.clear_reference)
    return {"ok": True, "removed": removed}


@app.post("/api/annotate/auto")
async def annotate_auto(payload: AutoAnnotateRequest, request: Request) -> dict[str, Any]:
    """背景差分自动标注。

    - `all=false`（默认）：只算这一张，返回建议框让前端预览确认，**不写文件**
    - `all=true`：批量处理并直接写标签文件，返回逐张结果
    """
    services = services_of(request)
    options = {
        "threshold": payload.threshold,
        "min_area": payload.min_area,
        "max_area": payload.max_area,
        "dilate": payload.dilate,
        "padding": payload.padding,
        "sigma_k": payload.sigma_k,
        "min_fill": payload.min_fill,
        "blur": payload.blur,
    }
    try:
        if payload.all:
            result = await asyncio.to_thread(
                services.annotator.auto_label_all,
                payload.class_name,
                payload.only_unlabeled,
                payload.overwrite,
                **options,
            )
            return {"ok": True, **result}
        if not payload.name:
            raise HTTPException(status_code=400, detail="请提供 name（单张）或 all=true（批量）")
        default_class = payload.class_name
        if not default_class:
            category = payload.name.split("/")[0]
            if category in services.annotator.class_index():
                default_class = category
        result = await asyncio.to_thread(services.annotator.auto_boxes, payload.name, default_class, **options)
    except AnnotateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "name": payload.name, **result}


# ---------------------------------------------------------------------------
# 视频流
# ---------------------------------------------------------------------------
@app.get("/api/video/feed")
async def video_feed(request: Request) -> StreamingResponse:
    """MJPEG 预览流，可直接给 <img src> 使用。"""
    services = services_of(request)
    return StreamingResponse(
        services.camera.frames(),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
def snapshot_of(services: AppServices) -> dict[str, Any]:
    return {
        "detection": services.pipeline.snapshot(),
        "camera": services.camera.status(),
        "model": services.detector.model_info(),
        "serial": services.serial.status(),
    }


async def handle_ws_command(services: AppServices, websocket: WebSocket, message: dict[str, Any]) -> None:
    """处理前端通过 WS 下发的指令。"""
    command = str(message.get("cmd") or message.get("action") or "").strip().lower()
    payload = message.get("data") if isinstance(message.get("data"), dict) else message
    manager = services.manager

    if command in ("ping", "heartbeat"):
        await manager.send(websocket, {"type": "pong", "data": {"ts": payload.get("ts")}})
        return

    if command in ("state", "status", "snapshot"):
        await manager.send(websocket, {"type": "state", "data": snapshot_of(services)})
        return

    if command in ("detection.start", "start"):
        try:
            result = await asyncio.to_thread(
                services.pipeline.start,
                bool(payload.get("reset_stats", False)),
                payload.get("conf"),
            )
        except PipelineError as exc:
            await manager.send(websocket, {"type": "error", "data": {"scope": command, "message": str(exc)}})
            return
        await manager.send(websocket, {"type": "ack", "data": {"cmd": command, "detection": result}})
        return

    if command in ("detection.stop", "stop"):
        result = await asyncio.to_thread(services.pipeline.stop)
        await manager.send(websocket, {"type": "ack", "data": {"cmd": command, "detection": result}})
        return

    if command in ("sort.manual", "manual_sort"):
        try:
            result = await asyncio.to_thread(
                services.pipeline.manual_sort,
                payload.get("class_name") or payload.get("class"),
                payload.get("slot"),
                payload.get("angle"),
                bool(payload.get("force", True)),
                payload.get("h"),
                payload.get("v"),
            )
        except PipelineError as exc:
            await manager.send(websocket, {"type": "error", "data": {"scope": command, "message": str(exc)}})
            return
        await manager.send(websocket, {"type": "ack", "data": {"cmd": command, "result": result}})
        return

    if command in ("camera.select", "camera.open"):
        try:
            info = await asyncio.to_thread(services.camera.open, int(payload.get("index", 0)))
        except Exception as exc:
            await manager.send(websocket, {"type": "error", "data": {"scope": command, "message": str(exc)}})
            return
        await manager.send(websocket, {"type": "ack", "data": {"cmd": command, "camera": info.to_dict()}})
        return

    await manager.send(
        websocket,
        {"type": "error", "data": {"scope": "ws", "message": f"未知指令: {command or '(空)'}"}},
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """状态 / 检测结果推送通道，同时接受少量控制指令。"""
    services: AppServices = websocket.app.state.services
    manager = services.manager
    await manager.connect(websocket)
    LOGGER.info("WebSocket 客户端接入，当前连接数=%d", manager.count)
    try:
        await manager.send(
            websocket,
            {"type": "hello", "data": {"version": "1.0.0", "state": snapshot_of(services)}},
        )
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                await manager.send(websocket, {"type": "error", "data": {"scope": "ws", "message": "JSON 解析失败"}})
                continue
            if not isinstance(message, dict):
                await manager.send(websocket, {"type": "error", "data": {"scope": "ws", "message": "报文必须是对象"}})
                continue
            try:
                await handle_ws_command(services, websocket, message)
            except WebSocketDisconnect:
                raise
            except Exception as exc:  # 单条指令失败不影响连接
                LOGGER.exception("处理 WS 指令失败: %s", exc)
                await manager.send(websocket, {"type": "error", "data": {"scope": "ws", "message": str(exc)}})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - 网络异常
        LOGGER.debug("WebSocket 异常关闭: %s", exc)
    finally:
        await manager.disconnect(websocket)
        LOGGER.info("WebSocket 客户端断开，当前连接数=%d", manager.count)


# ---------------------------------------------------------------------------
# 全局异常兜底
# ---------------------------------------------------------------------------
@app.exception_handler(CameraError)
async def camera_error_handler(request: Request, exc: CameraError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc), "scope": "camera"})


@app.exception_handler(DetectorError)
async def detector_error_handler(request: Request, exc: DetectorError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc), "scope": "detector"})


@app.exception_handler(SerialError)
async def serial_error_handler(request: Request, exc: SerialError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc), "scope": "serial"})


@app.exception_handler(PipelineError)
async def pipeline_error_handler(request: Request, exc: PipelineError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"ok": False, "error": str(exc), "scope": "pipeline"})


# ---------------------------------------------------------------------------
# 局域网协作：把地址告诉大家 + 托管前端构建产物
#
# 典型用法（一台电脑抓拍、另一台电脑标注）：
#   A 机：python -m backend.main          → 打开 http://<A的局域网IP>:8000 就能用
#   B 机：浏览器直接打开同一个地址即可，**不用装 Python / Node，也不用配 CORS**
# 前提是 A 机已经构建过一次前端：cd frontend && npm run build
# ---------------------------------------------------------------------------
def local_ipv4_addresses() -> list[str]:
    """本机可能被局域网访问的 IPv4 地址（用于告诉同事访问哪个网址）。"""
    found: list[str] = []
    try:
        # 不真的发包，只借路由表选出"对外那张网卡"的地址
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        found.append(str(probe.getsockname()[0]))
        probe.close()
    except OSError:  # pragma: no cover - 没有默认路由
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if address not in found and not address.startswith("127."):
                found.append(address)
    except OSError:  # pragma: no cover
        pass
    return found


def runtime_port() -> int:
    """实际监听的端口。

    `tools/desktop.py` 启动器可能换端口（8000 被占用时往后找），并把这个端口通过
    环境变量传进来；否则用 settings.yaml 里配置的值。局域网地址必须用真实端口拼，
    不然发给另一台电脑的网址是打不开的。
    """
    raw = os.environ.get("TRASH_SORTER_PORT")
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
            LOGGER.warning("TRASH_SORTER_PORT=%s 超出范围，忽略", raw)
        except ValueError:
            LOGGER.warning("TRASH_SORTER_PORT=%r 不是合法端口，忽略", raw)
    return int(load_settings()["app"].get("port", 8000))


def network_info() -> dict[str, Any]:
    settings = load_settings()
    host = str(settings["app"].get("host", "0.0.0.0"))
    port = runtime_port()
    urls = [f"http://{address}:{port}" for address in local_ipv4_addresses()]
    return {
        "ok": True,
        "host": host,
        "port": port,
        "urls": urls,
        "frontend_served": FRONTEND_DIST_READY,
        "claim_ttl_seconds": float(settings["app"].get("claim_ttl_seconds") or 120.0),
    }


@app.get("/api/network")
async def get_network() -> dict[str, Any]:
    """返回局域网访问地址，方便把网址发给另一台电脑。"""
    return await asyncio.to_thread(network_info)


# ---------------------------------------------------------------------------
# index.html 禁用缓存
#
# 静态资源带哈希可以长缓存，但 **index.html 不能缓存**：
# Windows WebView2 会缓存它，导致旧 index.html 仍然引用旧 bundle，
# 表现就是"改完前端，exe 窗口里看不到任何变化"。
# ---------------------------------------------------------------------------
@app.middleware("http")
async def no_cache_html(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/index.html") or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        # 追加：让 Chromium/WebView2 主动丢弃已缓存的旧资源（一次性生效）
        response.headers["Clear-Site-Data"] = '"cache"'
    return response

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
FRONTEND_DIST_READY = (FRONTEND_DIST / "index.html").is_file()
if FRONTEND_DIST_READY:
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")


# ---------------------------------------------------------------------------
# 直接运行
# ---------------------------------------------------------------------------
def main() -> None:
    """python -m backend.main 的入口。"""
    import uvicorn

    settings = load_settings()
    host = str(settings["app"].get("host", "0.0.0.0"))
    port = runtime_port()

    # 注意：这几行必须用 print —— 日志是在 lifespan 里才 basicConfig 的，
    # 用 LOGGER 打的话（INFO 级）会被直接丢掉，用户根本看不到。
    print("=" * 68)
    print("YOLO 垃圾自动识别分类系统 —— 后端")
    print("=" * 68)
    print(f"  本机访问   : http://127.0.0.1:{port}")
    if FRONTEND_DIST_READY:
        urls = network_info()["urls"]
        for index, url in enumerate(urls):
            suffix = "   <- 发给另一位标注员（推荐）" if index == 0 else ""
            print(f"  局域网访问 : {url}{suffix}")
        if len(urls) > 1:
            print("                （多出来的通常是虚拟机/虚拟网卡，选能 ping 通的那条）")
        print("  说明       : 前端已由后端托管，另一台电脑直接打开上面的网址即可，")
        print("               不用装 Python / Node，也不用配 CORS。")
        print(f"  打不开的话 : Windows 防火墙放行入站 {port} 端口")
    else:
        print("  提示       : 未找到前端构建产物，另一台电脑暂时只能用 npm run dev 访问。")
        print("               想直接开网址：cd frontend && npm run build")
    print("=" * 68)

    uvicorn.run(app, host=host, port=port, log_level=os.environ.get("TRASH_SORTER_LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# 分拣记录（MySQL）
#
# 不侵入 pipeline：pipeline 每次下发都会 emit({"type": "sort", ...})，
# 这里包一层即可把"一次下发"落库（EventBus.emit 是线程安全的同步函数）。
# ---------------------------------------------------------------------------
_EMIT_INNER: Any = None
_RECORDER: Any = None


def _emit_with_records(event: dict[str, Any]) -> Any:
    """先喂给记录采集器（绝不抛异常），再转发给原 EventBus。"""
    try:
        recorder = globals().get("_RECORDER")
        if recorder is not None:
            recorder.on_event(event)
    except Exception:  # noqa: BLE001
        pass
    inner = globals().get("_EMIT_INNER")
    if inner is None:
        return None
    return inner(event)
