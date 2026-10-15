"""一键启动：拉起后端 + 开一个原生窗口（WebView2）显示界面。

设计取舍
--------
界面就是网站本身（后端托管的 `frontend/dist`），所以"独立软件界面"不需要重写 UI：
用 Windows 自带的 **WebView2**（pywebview 的 edgechromium 后端）把这个页面渲染在一个
**没有地址栏、没有标签页**的原生窗口里，样式与网页 100% 一致，但看起来就是一个桌面软件。
Win10/11 自带 WebView2 运行时，所以不用额外装东西。

启动流程
--------
1. 先看有没有**已经在运行**的实例（扫 8000~8010），有就直接开窗口复用它
   —— 避免起第二个后端去抢摄像头；
2. 否则找一个空闲端口，用**同一个 Python 解释器**起后端子进程（uvicorn）；
3. 轮询 `/api/network` 直到就绪（模型加载通常 8~10 秒），期间如果子进程挂了就报错退出；
4. 打开窗口；窗口一关就结束后端子进程（否则摄像头不释放，下次启动会失败）。

用法
----
    双击 启动.bat                    正常启动（无控制台窗口）
    双击 启动-调试.bat               带控制台 + 日志，出问题时用

命令行参数：
    --port 8000     指定首选端口
    --check         只启动后端并等待就绪，不开窗口（自检）
    --selftest N    开窗口并在 N 秒后自动关闭（自检）
    --browser       强制用 Edge 的 --app 模式（WebView2 异常时的兜底）
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "desktop.log"
ICON_FILE = REPO_ROOT / "assets" / "app.ico"
WEBVIEW_DATA = LOG_DIR / "webview-profile"
WINDOW_TITLE = "YOLO 垃圾自动识别分类系统"
DEFAULT_PORT = 8000
PORT_SCAN = 11          # 8000~8010
READY_TIMEOUT = 90.0    # 模型加载 + 首次推理预热，给足时间
IS_WINDOWS = os.name == "nt"
# 后端子进程不要弹控制台（父进程是 pythonw 时本来就无窗口）
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

_log_lock = threading.Lock()


def log(message: str) -> None:
    """写日志文件，同时在有控制台时打到屏幕上。"""
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    with _log_lock:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        try:
            if sys.stdout is not None and sys.stdout.isatty():
                print(line, flush=True)
        except (OSError, ValueError):
            pass


def message_box(text: str, title: str = WINDOW_TITLE, error: bool = False) -> None:
    """无控制台时的报错窗口（pythonw 下 print 是看不见的）。"""
    if IS_WINDOWS:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
            return
        except Exception:  # pragma: no cover
            pass
    print(text, file=sys.stderr)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def probe(port: int, timeout: float = 1.5) -> dict | None:
    """这个端口上跑的是不是本项目的后端？"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/network", timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) and "urls" in payload else None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def find_existing() -> tuple[int, dict] | None:
    """已经有一个实例在跑？返回它的端口与网络信息。"""
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_SCAN):
        info = probe(port, timeout=1.0)
        if info:
            return port, info
    return None


def check_imports() -> list[str]:
    """后端依赖是否齐全（route ① 用的是本机 Python 环境，这是最常见的失败原因）。"""
    import importlib

    missing = []
    for name in ("fastapi", "uvicorn", "cv2", "ultralytics", "serial", "yaml", "numpy"):
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 任何导入失败都算缺失
            missing.append(name)
    return missing


def start_backend(port: int) -> subprocess.Popen:
    """用同一个解释器起后端；日志统一进 logs/desktop.log。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOG_FILE.open("a", encoding="utf-8", buffering=1)
    handle.write(f"\n===== 启动后端 {time.strftime('%Y-%m-%d %H:%M:%S')} (port={port}) =====\n")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    # 告诉后端真实端口：/api/network 用它拼局域网地址（否则 8000 被占换端口后，
    # 发给另一台电脑的网址是错的）
    env["TRASH_SORTER_PORT"] = str(port)
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    log(f"启动后端：{' '.join(command)}")
    return subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )


def wait_ready(port: int, process: subprocess.Popen, timeout: float = READY_TIMEOUT) -> dict | None:
    """等后端就绪；子进程中途退出就立刻失败（不用干等 90 秒）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            log(f"后端进程提前退出，返回码 {process.returncode}")
            return None
        info = probe(port, timeout=2.0)
        if info:
            log(f"后端就绪：http://127.0.0.1:{port}")
            return info
        time.sleep(0.4)
    log("等待后端就绪超时")
    return None


def tail_log(lines: int = 12) -> str:
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return "(没有日志)"


def stop_backend(process: subprocess.Popen | None) -> None:
    """结束后端。先温和 terminate，给它一点时间自己收尾，超时再强杀。"""
    if process is None or process.poll() is not None:
        return
    log("正在关闭后端…")
    try:
        process.terminate()
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        log("后端没有及时退出，强制结束")
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
    except OSError as exc:  # pragma: no cover
        log(f"关闭后端出错：{exc}")
    log("后端已关闭")


def find_edge() -> Path | None:
    """Edge 的位置（--app 兜底模式用）。"""
    for candidate in (
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def open_in_browser_window(port: int, keep_alive: bool) -> bool:
    """兜底：用 Edge 的 --app 模式开一个没有地址栏的窗口（独立 profile，退出可控）。"""
    edge = find_edge()
    if edge is None:
        return False
    profile = LOG_DIR / "edge-profile"
    profile.mkdir(parents=True, exist_ok=True)
    log(f"用 Edge --app 模式打开窗口：{edge}")
    process = subprocess.Popen(
        [
            str(edge),
            f"--app=http://127.0.0.1:{port}/",
            f"--user-data-dir={profile}",
            "--window-size=1440,920",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    )
    if keep_alive:
        process.wait()
    return True


def open_window(port: int, selftest: int | None, process: subprocess.Popen | None = None) -> str:
    """打开原生窗口，返回实际使用的后端名（edgechromium / fallback…）。"""
    import webview  # 延迟导入：没有 pywebview 时也能走 --check / --browser

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    window = webview.create_window(
        WINDOW_TITLE,
        f"http://127.0.0.1:{port}/",
        width=1440,
        height=920,
        min_size=(1120, 720),
        background_color="#0B1418",
        text_select=True,
    )

    if selftest:
        def auto_close() -> None:
            time.sleep(float(selftest))
            log(f"自检：{selftest}s 到，关闭窗口")
            try:
                window.destroy()
            except Exception as exc:  # noqa: BLE001
                log(f"关闭窗口失败：{exc}")

        def hard_exit() -> None:
            """自检专用保险：窗口没按时关就收拾干净再硬退，别把会话/后端挂住。"""
            time.sleep(float(selftest) + 15)
            log("自检：窗口没有按时关闭，强制退出")
            stop_backend(process)
            os._exit(9)

        threading.Thread(target=auto_close, daemon=True).start()
        threading.Thread(target=hard_exit, daemon=True).start()

    start_kwargs: dict = {
        "private_mode": False,          # 让主题偏好等 localStorage 能留下来
        "storage_path": str(WEBVIEW_DATA),
        "debug": os.environ.get("TRASH_SORTER_WEBVIEW_DEBUG") == "1",
    }
    if ICON_FILE.is_file():
        start_kwargs["icon"] = str(ICON_FILE)

    # 优先强制 EdgeChromium（WebView2）；万一不可用，再退回 pywebview 自选后端
    try:
        webview.start(gui="edgechromium", **start_kwargs)
        return "edgechromium"
    except Exception as exc:  # noqa: BLE001 - 后端初始化失败就降级
        log(f"WebView2 后端启动失败（{type(exc).__name__}: {exc}），改用默认后端重试")
        webview.start(**start_kwargs)
        return "default"


def main() -> int:
    parser = argparse.ArgumentParser(description="YOLO 垃圾自动识别分类系统 · 一键启动")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="首选端口（默认 8000）")
    parser.add_argument("--check", action="store_true", help="只启动后端并等待就绪，不开窗口")
    parser.add_argument("--selftest", type=int, default=None, help="N 秒后自动关窗（自检）")
    parser.add_argument("--browser", action="store_true", help="强制用 Edge --app 模式")
    parser.add_argument("--force-new", action="store_true", help="忽略已在运行的实例，强制新起一个（自检用）")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log(f"启动器开始运行（解释器 {sys.executable}）")

    missing = check_imports()
    if missing:
        hint = (
            "缺少后端依赖：" + "、".join(missing) + "\n\n"
            "请用项目所需的 Python 环境安装：\n"
            "    pip install -r backend/requirements.txt"
        )
        log("依赖缺失：" + "、".join(missing))
        message_box(hint, error=True)
        return 2

    existing = None if args.force_new else find_existing()
    process: subprocess.Popen | None = None
    if existing:
        port, info = existing
        log(f"检测到已在运行的实例（端口 {port}），直接复用它，不再起第二个后端")
    else:
        port = args.port
        for candidate in range(args.port, args.port + PORT_SCAN):
            if port_free(candidate):
                port = candidate
                break
        else:
            message_box(
                f"端口 {args.port}~{args.port + PORT_SCAN - 1} 都被占用了。\n"
                "请先关掉占用端口的程序，或用 --port 指定其它端口。",
                error=True,
            )
            return 3
        if port != args.port:
            log(f"端口 {args.port} 被占用，改用 {port}")
        process = start_backend(port)
        info = wait_ready(port, process)
        if info is None:
            stop_backend(process)
            message_box(
                "后端启动失败，界面没有打开。\n\n"
                f"日志最后几行（完整日志见 logs/desktop.log）：\n{tail_log(8)}",
                error=True,
            )
            return 4

    urls = "、".join(info.get("urls", [])[:3]) if isinstance(info, dict) else ""
    log(f"可用地址：{urls}")

    if args.check:
        print(f"  就绪：http://127.0.0.1:{port}/    局域网：{urls}")
        stop_backend(process)
        return 0

    if args.browser:
        open_in_browser_window(port, keep_alive=True)
        stop_backend(process)
        return 0

    try:
        backend_used = open_window(port, args.selftest, process)
        log(f"窗口已关闭（后端 {backend_used}）")
    except ImportError:
        log("没有 pywebview，改用 Edge --app 模式")
        if not open_in_browser_window(port, keep_alive=True):
            message_box(
                "没有安装 pywebview，也没找到 Edge，无法打开窗口。\n\n"
                "请执行：pip install pywebview\n"
                "或直接用浏览器打开 http://127.0.0.1:%d/" % port,
                error=True,
            )
            stop_backend(process)
            return 5
    except Exception as exc:  # noqa: BLE001 - 窗口阶段任何异常都不能让后端变孤儿
        log(f"打开窗口失败：{type(exc).__name__}: {exc}")
        message_box(f"打开窗口失败：{exc}\n\n日志：logs/desktop.log", error=True)
        if not open_in_browser_window(port, keep_alive=True):
            stop_backend(process)
            return 6

    # 只有"窗口真关了"才关后端；复用已有实例时不能去关别人的进程
    if process is not None:
        stop_backend(process)
    else:
        log("窗口已关闭（后端是之前的实例，保持运行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
