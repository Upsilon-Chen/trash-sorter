"""桌面启动器（会被冻成 exe）。

exe 里**不放** pywebview / pythonnet
----------------------------------
冻结 pywebview + pythonnet 很容易踩坑（运行时 DLL、PyInstaller 钩子、CLR 加载路径）。
所以这个 exe 只干四件事，窗口与业务逻辑都留在 Python 侧（`tools/desktop.py`）：

    1. 找一个**装了本项目依赖**的 Python 解释器（快速探测，不真正 import torch）
    2. 建一个 Job Object（KILL_ON_JOB_CLOSE）并让子进程加入
    3. 用 pythonw.exe 起 `tools/desktop.py`，把参数原样传下去
    4. 等它结束；非 0 退出码就弹窗提示 + 附日志尾部

清理保证
--------
Job Object 是 Windows 的"进程组"机制：**只要 exe 一退出（正常退出、被任务管理器强杀、
崩溃都算），组里的进程会被系统一起杀掉**。所以不会出现"关了窗口后端还在跑、摄像头被占"
的情况。后端自身还有一道"启动器看护"（见 backend/main.py 的 start_parent_watchdog），
两条路都堵上了。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_TITLE = "YOLO 垃圾自动识别分类系统"
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
FROZEN = bool(getattr(sys, "frozen", False))

# 探测依赖是否存在（用 find_spec 而不是 import：torch 一 import 就要好几秒）
PROBE_CODE = (
    "import importlib.util as u, sys;"
    "mods=['fastapi','uvicorn','cv2','ultralytics','serial','yaml','numpy'];"
    "sys.exit(0 if all(u.find_spec(m) for m in mods) else 1)"
)


def repo_root() -> Path:
    """项目根目录：exe 同级，或往上最多找 4 层（认 backend/main.py）。"""
    starts = [Path(sys.executable).resolve().parent] if FROZEN else [Path(__file__).resolve().parents[1]]
    for start in starts:
        candidate = start
        for _ in range(5):
            if (candidate / "backend" / "main.py").is_file():
                return candidate
            if candidate.parent == candidate:
                break
            candidate = candidate.parent
    return starts[0]


ROOT = repo_root()
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "launcher.log"
CACHE_FILE = LOG_DIR / "python-path.txt"


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except (OSError, ValueError):
        pass


def message_box(text: str, error: bool = True) -> None:
    if IS_WINDOWS:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10 if error else 0x40)
            return
        except Exception:  # pragma: no cover
            pass
    print(text, file=sys.stderr)


# --------------------------------------------------------------- 找解释器

def candidate_pythons() -> list[str]:
    """按"最可能装了依赖"的顺序列出候选解释器。"""
    found: list[str] = []

    def add(value: str | os.PathLike[str] | None) -> None:
        if not value:
            return
        path = Path(value)
        try:
            if path.is_file() and str(path) not in found:
                found.append(str(path))
        except OSError:
            pass

    local = os.environ.get("LOCALAPPDATA")
    if local:
        for version in ("313", "312", "311", "310", "39"):
            add(Path(local) / "Programs" / "Python" / f"Python{version}" / "pythonw.exe")
            add(Path(local) / "Programs" / "Python" / f"Python{version}" / "python.exe")

    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), "C:\\", "D:\\"):
        if not root:
            continue
        for name in ("Python313", "Python312", "Python311", "Python310", "miniconda3", "anaconda3", "miniconda", "anaconda"):
            add(Path(root) / name / "pythonw.exe")
            add(Path(root) / name / "python.exe")

    add(shutil.which("pythonw.exe"))
    add(shutil.which("python.exe"))
    if not FROZEN:
        add(sys.executable)
    return found


def python_with_deps() -> tuple[str | None, list[str]]:
    """挑第一个依赖齐全的解释器；返回 (解释器, 试过的列表)。"""
    tried: list[str] = []
    cached = ''
    try:
        if CACHE_FILE.is_file():
            cached = CACHE_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        cached = ''
    # 上次成功过的解释器优先，省掉重复探测（只查 find_spec 也要 0.5~1 秒）
    for python in ([cached] if cached else []) + candidate_pythons():
        tried.append(python)
        try:
            result = subprocess.run(
                [python, "-c", PROBE_CODE],
                capture_output=True,
                timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            try:
                CACHE_FILE.write_text(python, encoding='utf-8')
            except OSError:
                pass
            log(f"选定解释器：{python}")
            return python, tried
    return None, tried


# --------------------------------------------------------------- Job Object

class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


def create_kill_on_close_job() -> int | None:
    """建 Job Object。返回句柄（不要关！句柄一关就会立刻杀掉组内进程）。"""
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            log(f"CreateJobObject 失败：{ctypes.get_last_error()}")
            return None
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            ctypes.c_void_p(job),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            log(f"SetInformationJobObject 失败：{ctypes.get_last_error()}（继续，靠后端自身看护兜底）")
            return None
        return int(job)
    except Exception as exc:  # noqa: BLE001 - Job Object 只是加固，失败不影响启动
        log(f"创建 Job Object 异常：{type(exc).__name__}: {exc}")
        return None


def assign_to_job(job: int, pid: int) -> bool:
    if not IS_WINDOWS or not job:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not handle:
            log(f"OpenProcess({pid}) 失败：{ctypes.get_last_error()}")
            return False
        try:
            ok = kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(handle))
            if not ok:
                log(f"AssignProcessToJobObject 失败：{ctypes.get_last_error()}")
            return bool(ok)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception as exc:  # noqa: BLE001
        log(f"加入 Job Object 异常：{type(exc).__name__}: {exc}")
        return False


# --------------------------------------------------------------- 主流程

def desktop_script() -> Path | None:
    """优先用仓库里的 tools/desktop.py；找不到就用打包进 exe 的那份。"""
    repo_copy = ROOT / "tools" / "desktop.py"
    if repo_copy.is_file():
        return repo_copy
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "tools" / "desktop.py"
    if bundled.is_file():
        return bundled
    return None


def launcher_log_tail(lines: int = 10) -> str:
    for path in (LOG_DIR / "desktop.log", LOG_FILE):
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if content:
                return "\n".join(content[-lines:])
        except OSError:
            continue
    return "(没有日志)"


def main() -> int:
    args = sys.argv[1:]
    log("=" * 60)
    log(f"启动器 exe 开始（frozen={FROZEN}，根目录 {ROOT}）")

    script = desktop_script()
    if script is None:
        message_box(
            "找不到 tools/desktop.py。\n\n"
            "请把这个 exe 放在项目根目录（与 backend/、tools/ 同级）后再运行。"
        )
        return 2

    python, tried = python_with_deps()
    if python is None:
        detail = "\n".join(f"  · {item}" for item in tried[:8]) or "  （没找到任何 Python）"
        message_box(
            "没有找到装了本项目依赖的 Python 解释器。\n\n"
            "已尝试：\n"
            f"{detail}\n\n"
            "请在项目目录执行：\n"
            "    pip install -r backend/requirements.txt"
        )
        log("找不到可用解释器，候选：" + "; ".join(tried))
        return 3

    job = create_kill_on_close_job()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    child_log = (LOG_DIR / "desktop-child.log").open("a", encoding="utf-8", buffering=1)
    child_log.write(f"\n===== 由 exe 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    env = dict(os.environ)
    env["TRASH_SORTER_ROOT"] = str(ROOT)          # desktop.py 在临时目录里也能找对根目录
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    # 让界面进程看护本启动器：本进程（或 --onefile 的引导进程）一死，界面就自己关掉。
    # 只传"确定会一直活着"的 PID，所以 .bat 直接启动那条路径不受影响。
    watch_pids = [str(os.getpid())]
    if FROZEN and IS_WINDOWS:
        try:
            parent = os.getppid()  # --onefile 下这是引导进程，任务管理器里也能看到它
            if parent and parent > 0:
                watch_pids.append(str(parent))
        except OSError:
            pass
    env["TRASH_SORTER_PARENT_PIDS"] = ",".join(watch_pids)
    log(f"界面进程需要看护的 PID：{watch_pids}")
    command = [python, str(script), *args]
    log("启动：" + " ".join(command))
    try:
        child = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=child_log,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        message_box(f"无法启动界面进程：{exc}")
        log(f"Popen 失败：{exc}")
        return 4

    if job:
        if assign_to_job(job, child.pid):
            log(f"已把界面进程 {child.pid} 加入 Job Object（exe 退出时它会被一起结束）")
    # PyInstaller 的 --splash 必须显式关闭，否则会一直挂在屏幕上
    try:
        import pyi_splash  # type: ignore[import-not-found]

        pyi_splash.update_text("正在启动界面…")
        pyi_splash.close()
        log("已关闭 exe 启动画面")
    except Exception:  # noqa: BLE001 - 非 frozen 环境没有这个模块
        pass

    code = child.wait()
    log(f"界面进程结束，退出码 {code}")
    if code != 0:
        message_box(f"启动失败（退出码 {code}）。\n\n日志尾部：\n{launcher_log_tail(8)}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
