"""ESP32 串口服务。

职责：
- 扫描系统串口，并按 CP210x / CH340 / ESP32 / USB-SERIAL 等关键字排优先级
- 连接 / 断开串口
- 按协议下发分类指令：一行 JSON {"cmd":"sort","class":"...","slot":0,"angle":90}
- 后台读线程解析 ESP32 上报：{"status":"ready"} / {"status":"done","slot":0}

设计要点：
- 所有写操作加锁并做异常兜底，串口掉线自动降级为断开状态并广播事件，
  不会把异常抛进推理线程或 FastAPI 事件循环。
- 读线程持有自己那一代连接的 stop 事件与句柄，断开时不需要 join，
  因此不会与主线程的锁产生死锁。
- 协议为按行 JSON（\\n 结尾）；对端未就绪、写入失败都不阻塞调用方。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

import serial
from serial.tools import list_ports

_LOGGER = logging.getLogger(__name__)

# 默认优先级关键字（可被 settings.yaml -> serial.priority_keywords 覆盖）
_DEFAULT_PRIORITY_KEYWORDS: tuple[str, ...] = (
    "CP210",
    "CH340",
    "CH910",
    "ESP32",
    "USB-SERIAL",
    "USB SERIAL",
    "SILICON LABS",
    "WCH",
)

_PRIORITY_TOP_SCORE = 100  # 命中第一个关键字的分值
_PRIORITY_STEP = 5  # 每往后一个关键字递减的分值


class SerialError(RuntimeError):
    """串口操作失败。"""


def _cfg(config: Optional[dict], *keys: str, default: Any = None) -> Any:
    node: Any = config
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return default if node is None else node


@dataclass(slots=True)
class SerialPortInfo:
    device: str
    description: str
    hwid: str
    manufacturer: str
    score: int = 0

    @property
    def preferred(self) -> bool:
        return self.score > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "description": self.description,
            "hwid": self.hwid,
            "manufacturer": self.manufacturer,
            "score": int(self.score),
            "preferred": self.preferred,
        }


class SerialService:
    """串口扫描 / 连接 / 收发。"""

    def __init__(
        self,
        config: Optional[dict] = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self._config = config or {}
        self._baudrate = int(_cfg(self._config, "baudrate", default=115200))
        self._timeout = float(_cfg(self._config, "timeout", default=0.5))
        self._write_timeout = float(_cfg(self._config, "write_timeout", default=1.0))
        self._ready_timeout = float(_cfg(self._config, "ready_timeout", default=3.0))
        self._autoconnect = bool(_cfg(self._config, "autoconnect", default=False))
        self._allow_any_port = bool(_cfg(self._config, "allow_any_port", default=True))
        keywords = _cfg(self._config, "priority_keywords", default=None)
        if isinstance(keywords, (list, tuple)) and keywords:
            self._priority_keywords: tuple[str, ...] = tuple(str(item).upper() for item in keywords)
        else:
            self._priority_keywords = _DEFAULT_PRIORITY_KEYWORDS

        self._on_event: Callable[[dict[str, Any]], None] = on_event or (lambda event: None)
        self._lock = threading.RLock()  # 保护连接状态（绝不在持锁时做阻塞 I/O）
        self._write_lock = threading.Lock()  # 只保护「写出这一行」，取锁带超时
        self._serial: Optional[serial.Serial] = None
        self._port: Optional[str] = None
        self._connected_at: Optional[float] = None
        self._reader: Optional[threading.Thread] = None
        self._reader_stop: Optional[threading.Event] = None
        self._last_status: Optional[str] = None
        self._ready = False
        self._ready_event = threading.Event()
        self._tx_count = 0
        self._rx_count = 0
        self._last_tx: Optional[dict[str, Any]] = None
        self._last_rx: Optional[dict[str, Any]] = None
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------ 基础属性
    @property
    def is_connected(self) -> bool:
        # 无锁读：属性查询绝不能因为别人持锁而阻塞
        port = self._serial
        return port is not None and getattr(port, "is_open", False)
    @property
    def port(self) -> Optional[str]:
        return self._port

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def baudrate(self) -> int:
        return self._baudrate

    @property
    def autoconnect(self) -> bool:
        return self._autoconnect

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self._on_event(event)
        except Exception as exc:  # pragma: no cover - 事件回调不应影响串口线程
            _LOGGER.debug("串口事件回调失败: %s", exc)

    def status(self) -> dict[str, Any]:
        # 取锁带超时：写操作在蓝牙串口上可能永久阻塞，死等会拖垮整个后端
        locked = self._lock.acquire(timeout=0.25)
        try:
            connection = None
            if self._serial is not None:
                connection = {
                    "port": self._port,
                    "baudrate": self._baudrate,
                    "connected_at": (
                        datetime.fromtimestamp(self._connected_at).isoformat(timespec="seconds")
                        if self._connected_at
                        else None
                    ),
                    "ready": self._ready,
                    "last_status": self._last_status,
                }
        finally:
            if locked:
                self._lock.release()
        return {
            "connected": self.is_connected,
            "connection": connection,
            "tx_count": self._tx_count,
            "rx_count": self._rx_count,
            "last_tx": self._last_tx,
            "last_rx": self._last_rx,
            "last_error": self._last_error,
        }

    # ---------------------------------------------------------------- 扫描
    def _score_port(self, port: Any) -> int:
        """按关键字命中顺序给端口打分，越靠前的关键字分值越高。"""
        haystack = " ".join(
            str(part or "")
            for part in (
                getattr(port, "device", ""),
                getattr(port, "description", ""),
                getattr(port, "manufacturer", ""),
                getattr(port, "hwid", ""),
                getattr(port, "product", ""),
            )
        ).upper()
        for index, keyword in enumerate(self._priority_keywords):
            if keyword and keyword in haystack:
                return _PRIORITY_TOP_SCORE - index * _PRIORITY_STEP
        return 0

    def list_ports(self) -> list[SerialPortInfo]:
        """扫描串口，按优先级降序（无匹配的排最后）。"""
        try:
            raw_ports = list(list_ports.comports())
        except Exception as exc:  # pragma: no cover - 平台异常
            raise SerialError(f"扫描串口失败: {exc}") from exc

        infos = [
            SerialPortInfo(
                device=str(getattr(port, "device", "") or ""),
                description=str(getattr(port, "description", "") or ""),
                hwid=str(getattr(port, "hwid", "") or ""),
                manufacturer=str(getattr(port, "manufacturer", "") or ""),
                score=self._score_port(port),
            )
            for port in raw_ports
        ]
        infos = [info for info in infos if info.device]
        infos.sort(key=lambda info: (-info.score, info.device.lower()))
        return infos

    def pick_port(self) -> Optional[str]:
        """挑选默认端口：优先关键字命中的第一个，其次第一个可用端口。"""
        try:
            ports = self.list_ports()
        except SerialError as exc:
            _LOGGER.warning("%s", exc)
            return None
        for info in ports:
            if info.preferred:
                return info.device
        if ports and self._allow_any_port:
            return ports[0].device
        return None

    # ------------------------------------------------------------ 连接管理
    def connect(self, port: Optional[str] = None, baudrate: Optional[int] = None) -> dict[str, Any]:
        """连接串口；port 为空时自动挑选优先级最高的端口。"""
        if port is None or str(port).strip() == "":
            picked = self.pick_port()
            if picked is None:
                raise SerialError("未发现可用串口，请插入 ESP32 后重试")
            port = picked
        port = str(port).strip()
        baud = int(baudrate) if baudrate else self._baudrate

        with self._lock:
            if self._serial is not None and getattr(self._serial, "is_open", False):
                if self._port == port and self._baudrate == baud:
                    return self.status()
                self._teardown_locked(reason="切换端口")

        _LOGGER.info("正在连接串口 %s @ %d", port, baud)
        try:
            connection = serial.Serial(
                port=port,
                baudrate=baud,
                timeout=self._timeout,
                write_timeout=self._write_timeout,
            )
        except Exception as exc:
            self._last_error = str(exc)
            raise SerialError(f"打开串口 {port} 失败: {exc}") from exc

        try:
            connection.reset_input_buffer()
            connection.reset_output_buffer()
        except Exception:  # pragma: no cover - 部分驱动不支持
            pass

        stop_event = threading.Event()
        reader = threading.Thread(
            target=self._reader_loop,
            args=(connection, stop_event),
            name="serial-reader",
            daemon=True,
        )
        with self._lock:
            self._serial = connection
            self._port = port
            self._baudrate = baud
            self._connected_at = time.time()
            self._ready = False
            self._ready_event.clear()
            self._last_error = None
            self._reader_stop = stop_event
            self._reader = reader
            reader.start()

        self._emit({"type": "serial_connected", "data": {"port": port, "baudrate": baud}})

        # 等待 ESP32 上报 ready（超时不算失败，可能固件只是没打印）
        if self._ready_timeout > 0 and self._ready_event.wait(timeout=self._ready_timeout):
            _LOGGER.info("ESP32 已上报 ready")
        elif self._ready_timeout > 0:
            _LOGGER.warning("等待 ESP32 ready 超时（%.1fs），连接保持", self._ready_timeout)
        return self.status()

    def disconnect(self) -> dict[str, Any]:
        """断开串口。"""
        with self._lock:
            self._teardown_locked(reason="手动断开")
        return self.status()

    def _teardown_locked(self, reason: str = "") -> None:
        """内部关闭实现（调用方需持有 self._lock）。

        只关闭句柄并置位 stop 事件，不 join 读线程：读线程是 daemon，
        且持有的是自己那一代连接的句柄，会自行退出。
        """
        connection = self._serial
        stop_event = self._reader_stop
        port = self._port
        self._serial = None
        self._port = None
        self._connected_at = None
        self._reader = None
        self._reader_stop = None
        self._ready = False
        self._ready_event.clear()
        if stop_event is not None:
            stop_event.set()
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pragma: no cover
                pass
        if port:
            _LOGGER.info("串口 %s 已断开 (%s)", port, reason or "closed")
            self._emit({"type": "serial_disconnected", "data": {"port": port, "reason": reason or "closed"}})

    # ---------------------------------------------------------------- 读线程
    def _reader_loop(self, connection: serial.Serial, stop_event: threading.Event) -> None:
        _LOGGER.debug("串口读线程启动")
        while not stop_event.is_set():
            try:
                raw = connection.readline()
            except Exception as exc:
                if not stop_event.is_set():
                    self._last_error = str(exc)
                    _LOGGER.warning("串口读取失败: %s", exc)
                    self._fail_connection(connection, f"读异常: {exc}")
                break
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            self._rx_count += 1
            self._handle_line(line)
        _LOGGER.debug("串口读线程退出")

    def _fail_connection(self, connection: serial.Serial, reason: str) -> None:
        """读线程发现连接异常时清理状态（不影响新连接）。"""
        with self._lock:
            if self._serial is connection:
                self._teardown_locked(reason=reason)
                return
        try:
            connection.close()
        except Exception:  # pragma: no cover
            pass

    def _handle_line(self, line: str) -> None:
        """解析 ESP32 上报的一行数据。"""
        try:
            message: Any = json.loads(line)
        except (ValueError, TypeError):
            message = None

        if isinstance(message, dict):
            self._last_rx = message
            status_value = str(message.get("status", "")).strip().lower()
            if status_value:
                self._last_status = status_value
                if status_value == "ready":
                    self._ready = True
                    self._ready_event.set()
            self._emit({"type": "serial_message", "data": {"message": message, "raw": line}})
            _LOGGER.debug("ESP32 -> PC: %s", message)
        else:
            self._emit({"type": "serial_log", "data": {"raw": line}})
            _LOGGER.debug("ESP32 文本输出: %s", line)

    # ---------------------------------------------------------------- 写
    def send_line(self, line: str) -> dict[str, Any]:
        """发送一行文本（自动补 \\n）。"""
        payload = line if line.endswith("\n") else line + "\n"
        # 只在锁内取连接引用，**写操作放到锁外**：
        # Windows 蓝牙/USB 串口的 write/flush 可能卡住不返回，持锁会永久堵死
        # status()/send()/teardown，进而拖垮整个后端（表现为所有列表卡在刷新）。
        with self._lock:
            connection = self._serial
            if connection is None or not getattr(connection, "is_open", False):
                return {"ok": False, "error": "串口未连接", "line": payload.strip()}
        # 写锁带超时：上一次写还没返回时，别让后续命令无限排队（那会占满线程池）
        if not self._write_lock.acquire(timeout=1.0):
            return {"ok": False, "error": "串口忙（上一次写入未返回）", "line": payload.strip()}
        wrote_ok = True
        write_error = None
        try:
            connection.write(payload.encode("utf-8"))
            # 不做 flush()：pyserial 的 flush 会等驱动确认，在蓝牙串口上可能永久阻塞
        except Exception as exc:
            wrote_ok = False
            write_error = exc
        finally:
            self._write_lock.release()
        if not wrote_ok:
            self._last_error = str(write_error)
            _LOGGER.warning("串口写入失败: %s", write_error)
            with self._lock:
                if self._serial is connection:
                    self._teardown_locked(reason=f"写异常: {write_error}")
            return {"ok": False, "error": str(write_error), "line": payload.strip()}
        self._tx_count += 1
        return {"ok": True, "error": None, "line": payload.strip()}

    def send_sort(
        self,
        class_name: str,
        slot: int,
        angle: int,
        h: Optional[int] = None,
        v: Optional[int] = None,
    ) -> dict[str, Any]:
        """下发分类指令：{"cmd":"sort","class":"...","slot":0,"angle":90}。

        固件按 slot 查表决定角度，`angle` 只是记录值；要临时覆盖角度必须带 h/v：
        {"cmd":"sort","slot":1,"angle":90,"h":210,"v":170}
        """
        payload: dict[str, Any] = {
            "cmd": "sort",
            "class": str(class_name),
            "slot": int(slot),
            "angle": int(angle),
        }
        if h is not None:
            payload["h"] = int(h)
        if v is not None:
            payload["v"] = int(v)
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:  # pragma: no cover
            return {"ok": False, "error": f"序列化失败: {exc}", "payload": payload, "line": None}

        result = self.send_line(text)
        if result.get("ok"):
            self._last_tx = {"payload": payload, "ts": time.time()}
            self._emit({"type": "serial_tx", "data": {"payload": payload, "raw": text}})
            _LOGGER.info("PC -> ESP32: %s", text)
        return {**result, "payload": payload}

    def send_raw(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发送任意 JSON 字典（调试 / 协议扩展用）。"""
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"序列化失败: {exc}", "payload": payload, "line": None}
        result = self.send_line(text)
        if result.get("ok"):
            self._last_tx = {"payload": payload, "ts": time.time()}
        return {**result, "payload": payload}

    def __enter__(self) -> "SerialService":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        with self._lock:
            self._teardown_locked(reason="退出")
