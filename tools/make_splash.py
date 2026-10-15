"""生成 exe 启动画面（PyInstaller --splash 用的 PNG）。

为什么要它：windows 下双击 exe 后，PyInstaller 的 onefile 引导进程要先把自己解压到
临时目录（几百毫秒到几秒），然后才轮到 Python 找解释器、起后端。这期间屏幕上什么都
没有，用户会以为"没反应"。PyInstaller 的 --splash 由引导进程**最先**显示一张图片，
所以双击后立刻有反馈。

设计：和界面同一套语言（深水底 + 荧光绿 + 细线），尺寸小、无透明、纯色背景。
跑一次即可：python tools/make_splash.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "splash.png"

WIDTH, HEIGHT = 440, 280
BG = (6, 17, 13)
INK = (230, 242, 236)
MUTED = (157, 179, 168)
ACCENT = (53, 227, 154)
CYAN = (79, 214, 232)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """优先用系统里的中文字体，找不到就退回默认（不引外部字体文件）。"""
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> int:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    # 顶部柔光：两张同心半透明圆模拟径向渐变（PIL 没有原生径向渐变）
    glow = Image.new("RGB", (WIDTH, HEIGHT), BG)
    gdraw = ImageDraw.Draw(glow)
    for step in range(26, 0, -1):
        ratio = step / 26
        radius = int(150 * ratio)
        color = tuple(
            round(BG[index] + (ACCENT[index] - BG[index]) * (1 - ratio) * 0.5) for index in range(3)
        )
        gdraw.ellipse(
            [WIDTH * 0.78 - radius, 40 - radius, WIDTH * 0.78 + radius, 40 + radius],
            fill=color,
        )
    image = Image.blend(image, glow, 0.55)
    draw = ImageDraw.Draw(image)

    # 品牌标：圆环 + 中心点（呼应界面里的圆形编号与环母题）
    cx, cy, r = 52, 62, 17
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT, width=2)
    draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=CYAN)

    draw.text((84, 44), "YOLO 垃圾自动识别分类系统", font=load_font(19, bold=True), fill=INK)
    draw.text((85, 72), "WASTE SORTING · VISION → MAPPING → SERVO", font=load_font(10), fill=MUTED)

    # 进度条：引导进程会在这张图上叠加自己的进度条，这里只画底槽
    y = 168
    draw.line([(36, y), (WIDTH - 36, y)], fill=(38, 49, 58), width=2)

    draw.text((36, 190), "正在启动…", font=load_font(13), fill=INK)
    draw.text((36, 216), "首次启动需加载检测模型，通常 10~25 秒", font=load_font(11), fill=MUTED)
    draw.text((36, 240), "日志：logs/desktop.log", font=load_font(10), fill=MUTED)

    # 1px 边框，和界面的细线语言一致
    draw.rectangle([0, 0, WIDTH - 1, HEIGHT - 1], outline=(30, 45, 38), width=1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT, format="PNG", optimize=True)
    print(f"  写出 {OUT.relative_to(ROOT)}  （{OUT.stat().st_size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
