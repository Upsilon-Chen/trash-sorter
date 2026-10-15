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

def _resolve_repo_root() -> Path:
    """找到项目根目录（含 backend/main.py 的那一层）。

    为什么要找而不是直接 `parents[1]`：冻成 exe 后，启动器可能会从解压出来的临时目录
    运行这个脚本，靠 `__file__` 推算会算错。启动器会把真实根目录通过
    `TRASH_SORTER_ROOT` 传进来，这里再兜底几个候选位置。
    """
    candidates: list[Path] = []
    env_root = os.environ.get("TRASH_SORTER_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path(__file__).resolve().parents[1])
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    candidates.append(Path.cwd())
    for candidate in candidates:
        try:
            if (candidate / "backend" / "main.py").is_file():
                return candidate.resolve()
        except OSError:
            continue
    return candidates[0].resolve()


REPO_ROOT = _resolve_repo_root()
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
    """后端依赖是否齐全（route ① 用的是本机 Python 环境，这是最常见的失败原因）。

    注意：这里只查"包在不在"（`find_spec`），**不能真的 import** ——
    `import ultralytics` 会连带加载 torch，白白多等 8~10 秒启动时间。
    """
    import importlib.util

    missing = []
    for name in ("fastapi", "uvicorn", "cv2", "ultralytics", "serial", "yaml", "numpy"):
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
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
    # 让后端看护启动器：启动器被强杀/崩溃时，后端自己退出，不会留着占摄像头
    env["TRASH_SORTER_PARENT_PID"] = str(os.getpid())
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


def _pid_alive(pid: int) -> bool:
    """进程还活着吗。"""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:  # pragma: no cover
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_parent_watchdog(process: subprocess.Popen | None) -> list[int]:
    """看护启动器：它一退出，界面就自己关掉（顺带结束后端）。

    为什么需要：只有"关窗口"这条正常路径会主动收尾。如果启动器被任务管理器强杀、
    崩了，或者 `--onefile` 打包出来的引导进程被杀掉，界面就会变成没人管的孤儿窗口。
    启动器会把要盯的 PID 列表通过 `TRASH_SORTER_PARENT_PIDS` 传进来 ——
    **只有它主动传了才盯**，所以 `启动.bat`（直接跑）这条路径不受影响。
    """
    raw = os.environ.get("TRASH_SORTER_PARENT_PIDS") or os.environ.get("TRASH_SORTER_PARENT_PID") or ""
    pids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            pids.append(int(chunk))
    if not pids:
        return []
    # 自己不算
    pids = [pid for pid in pids if pid != os.getpid()]
    if not pids:
        return []

    def watch() -> None:
        while True:
            time.sleep(2.0)
            # 语义：**只要有一个被看守的进程消失**，就认为启动器已经没了。
            # （--onefile 的 exe 有"引导进程 + 应用进程"两个，杀掉任意一个 ——
            #   比如任务管理器里结束任务 —— 另一个也会跟着收尾，所以任一消失都该退。）
            if not all(_pid_alive(pid) for pid in pids):
                log(f"启动器进程 {pids} 已有退出，界面自动关闭")
                stop_backend(holder[0])
                os._exit(0)

    threading.Thread(target=watch, name="launcher-watchdog", daemon=True).start()
    log(f"已启用启动器看护：盯着 PID {pids}")
    return pids


SPLASH_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>__TITLE__</title><style>
  html,body{margin:0;height:100%;background:#eae5e1;color:#080a08;
    font-family:'MiSans','PingFang SC','Microsoft YaHei',system-ui,sans-serif}
  body{display:grid;place-items:center}
  .box{width:min(520px,72vw)}
  .bar{height:1px;background:#aaa59a;position:relative;margin:22px 0 12px}
  .bar i{position:absolute;left:0;top:0;height:1px;background:#9b7247;width:0;
    animation:go 9s cubic-bezier(.22,.61,.36,1) forwards}
  @keyframes go{to{width:100%}}
  .t{font-size:19px;font-weight:600;letter-spacing:.02em}
  .s{font-family:Consolas,monospace;font-size:10px;letter-spacing:.22em;
    text-transform:uppercase;color:#77756d;margin-top:2px}
  .m{font-family:Consolas,monospace;font-size:11px;color:#77756d;letter-spacing:.08em;line-height:1.9}
  .m b{color:#080a08;font-weight:400}
</style></head><body><div class="box">
  <div class="t">YOLO 垃圾自动识别分类系统</div>
  <div class="s">Waste Sorting &middot; Vision &rarr; Mapping &rarr; Servo</div>
  <div class="bar"><i></i></div>
  <div class="m">
    <div>正在启动后端&hellip;</div>
    <div>加载检测模型与摄像头通常需要 <b>10&ndash;20 秒</b>，请稍候</div>
    <div id="hint">若长时间没有反应，请看 logs/desktop.log</div>
  </div>
</div></body></html>"""



class WindowApi:
    """暴露给前端的窗口控制（无边框窗口用）。

    为什么不用 pywebview 的 easy_drag：那会让整个窗口都能拖动，
    表格里选文字、拖画布都会误触。这里只让标题区拖动，通过 move_to 实现。
    """

    def __init__(self) -> None:
        self._window = None

    def bind(self, window) -> None:
        self._window = window

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        if self._window is None:
            return
        try:
            if self._window.maximized:
                self._window.restore()
            else:
                self._window.maximize()
        except Exception as exc:  # noqa: BLE001
            log(f"最大化切换失败：{exc}")

    def move_to(self, x: int, y: int) -> None:
        if self._window is not None:
            try:
                self._window.move(int(x), int(y))
            except Exception as exc:  # noqa: BLE001
                log(f"移动窗口失败：{exc}")

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()

def open_window(
    port: int,
    selftest: int | None,
    process: subprocess.Popen | None = None,
    preparer: "Callable[[], bool] | None" = None,
) -> str:
    """打开原生窗口，返回实际使用的后端名。

    关键体验改动：**窗口立刻出现**（先显示一张启动页），后端在后台线程里起来，
    就绪后再把窗口切到真实界面。以前是"等 15~25 秒才弹窗"，看起来像卡死了。
    """
    import webview  # 延迟导入：没有 pywebview 时也能走 --check / --browser

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}/"
    window_api = WindowApi()
    window = webview.create_window(
        WINDOW_TITLE,
        url=url if preparer is None else None,
        html=None if preparer is None else SPLASH_HTML.replace("__TITLE__", WINDOW_TITLE),
        width=1440,
        height=920,
        min_size=(1120, 720),
        background_color="#EAE5E1",
        text_select=True,
        frameless=True,       # 去掉系统标题栏，改用界面自己的标题区
        easy_drag=False,      # 不让整窗可拖，避免干扰选文字/拖画布
        js_api=window_api,
    )

    window_api.bind(window)

    def worker() -> None:
        """后台线程：起后端 → 等就绪 → 把窗口切到真实界面。"""
        assert preparer is not None
        try:
            ready = preparer()
        except Exception as exc:  # noqa: BLE001
            log(f"启动后端异常：{type(exc).__name__}: {exc}")
            ready = False
        if ready:
            log("后端就绪，切到主界面")
            window.load_url(url)
        else:
            log("后端没有就绪，窗口显示错误提示")
            window.load_html(
                SPLASH_HTML.replace("__TITLE__", WINDOW_TITLE)
                .replace("正在启动后端&hellip;", "后端启动失败")
                .replace("加载检测模型与摄像头通常需要 <b>10&ndash;20 秒</b>，请稍候",
                         "日志最后几行：<br>" + "<br>".join(tail_log(6)).replace("\n", "<br>"))
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
            stop_backend(holder[0])
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
        webview.start(worker if preparer is not None else None, gui="edgechromium", **start_kwargs)
        return "edgechromium"
    except Exception as exc:  # noqa: BLE001 - 后端初始化失败就降级
        log(f"WebView2 后端启动失败（{type(exc).__name__}: {exc}），改用默认后端重试")
        webview.start(worker if preparer is not None else None, **start_kwargs)
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
    holder: list[subprocess.Popen | None] = [None]

    def boot(current_port: int) -> dict | None:
        """起后端并等就绪（在窗口路径里由后台线程调用，所以窗口能先弹出来）。"""
        holder[0] = start_backend(current_port)
        return wait_ready(current_port, holder[0])

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
        # --check / --browser / 无窗口路径需要同步等就绪；窗口路径交给 preparer
        if args.check or args.browser or sys.platform != "win32":
            info = boot(port)
            if info is None:
                stop_backend(holder[0])
                message_box(
                    "后端启动失败。\n\n"
                    f"日志最后几行（完整日志见 logs/desktop.log）：\n{tail_log(8)}",
                    error=True,
                )
                return 4
        else:
            info = {"urls": []}

    urls = "、".join(info.get("urls", [])[:3]) if isinstance(info, dict) else ""
    log(f"可用地址：{urls}")

    if args.check:
        print(f"  就绪：http://127.0.0.1:{port}/    局域网：{urls}")
        stop_backend(holder[0])
        return 0

    if args.browser:
        open_in_browser_window(port, keep_alive=True)
        stop_backend(holder[0])
        return 0

    # 已经就绪（复用实例或已同步启动）→ 直接开界面；否则先出窗口再后台启动
    preparer = None if existing else (lambda: boot(port) is not None)

    try:
        start_parent_watchdog(holder[0])
        backend_used = open_window(port, args.selftest, holder[0], preparer)
        log(f"窗口已关闭（后端 {backend_used}）")
    except ImportError:
        log("没有 pywebview，改用 Edge --app 模式")
        if holder[0] is None and preparer is not None:
            boot(port)
        if not open_in_browser_window(port, keep_alive=True):
            message_box(
                "没有安装 pywebview，也没找到 Edge，无法打开窗口。\n\n"
                "请执行：pip install pywebview\n"
                "或直接用浏览器打开 http://127.0.0.1:%d/" % port,
                error=True,
            )
            stop_backend(holder[0])
            return 5
    except Exception as exc:  # noqa: BLE001 - 窗口阶段任何异常都不能让后端变孤儿
        log(f"打开窗口失败：{type(exc).__name__}: {exc}")
        message_box(f"打开窗口失败：{exc}\n\n日志：logs/desktop.log", error=True)
        if not open_in_browser_window(port, keep_alive=True):
            stop_backend(holder[0])
            return 6

    # 只有"窗口真关了"才关后端；复用已有实例时不能去关别人的进程
    if holder[0] is not None:
        stop_backend(holder[0])
    else:
        log("窗口已关闭（后端是之前的实例，保持运行）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        # 无控制台时异常会被静默吞掉，表现成"双击没反应"，所以必须弹窗
        import traceback

        log(f"启动器异常：{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        message_box(f"启动失败：{type(exc).__name__}: {exc}\n\n日志：logs/desktop.log", error=True)
        raise SystemExit(1) from exc
