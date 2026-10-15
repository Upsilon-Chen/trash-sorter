"""把 tools/app_launcher.py 冻成 exe（一键构建）。

用法：
    python tools/build_exe.py

产物：
    垃圾自动分拣系统.exe        ← 放在项目根目录，双击即用
    build/pyinstaller/          ← 中间文件（已 gitignore）
    dist-exe/                   ← PyInstaller 原始输出（已 gitignore）

为什么 exe 很小：它只包含"找 Python + 起 tools/desktop.py + 等它结束"这段逻辑，
纯标准库，不含 torch / opencv / pywebview —— 那些仍在项目目录里由本机 Python 环境提供。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "tools" / "app_launcher.py"
DESKTOP = ROOT / "tools" / "desktop.py"
ICON = ROOT / "assets" / "app.ico"
WORK = ROOT / "build" / "pyinstaller"
DIST = ROOT / "dist-exe"
BUILD_NAME = "TrashSorterApp"
FINAL_NAME = "垃圾自动分拣系统.exe"


def pyinstaller_available() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main() -> int:
    if not pyinstaller_available():
        print("  没有 PyInstaller。先安装：")
        print(f"    {sys.executable} -m pip install pyinstaller")
        return 1

    if not ICON.is_file():
        print("  图标不存在，先跑一次 python tools/make_icon.py")
        return 1

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",  # 不弹控制台；报错由 exe 自己弹窗
        "--name",
        BUILD_NAME,
        "--icon",
        str(ICON),
        # 把 desktop.py 也打进去：挪到别处也能跑（找不到仓库副本时用它）
        "--add-data",
        f"{DESKTOP}{os.pathsep}tools",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        "--specpath",
        str(WORK),
        "--log-level",
        "WARN",
        str(ENTRY),
    ]
    print("  构建中（首次约 1~3 分钟）…")
    print("  " + " ".join(command[:8]) + " …")
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"  构建失败，退出码 {result.returncode}")
        return result.returncode

    built = DIST / f"{BUILD_NAME}.exe"
    if not built.is_file():
        print(f"  没找到产物 {built}")
        return 2

    target = ROOT / FINAL_NAME
    shutil.copy2(built, target)
    print(f"  ✅ 已生成 {target.name}（{target.stat().st_size / 1024 / 1024:.1f} MB）")
    print("     双击它即可启动；放到桌面也行，但项目目录要在它的同级或上级。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
