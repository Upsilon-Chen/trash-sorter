"""生成应用图标（assets/app.ico + 前端 favicon）。

窗口和任务栏如果顶着默认图标，一眼就是"没做完"。这里用 PIL 画一个简单的环保绿
圆角方块 + 白色垃圾桶，输出多尺寸 .ico（16/24/32/48/64/128/256），
Windows 会按场合自动挑合适的那一档。

跑一次就够：python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
ICON_PATH = REPO_ROOT / "assets" / "app.ico"
FAVICON_PATH = REPO_ROOT / "frontend" / "public" / "favicon.ico"

SIZE = 256
GREEN_TOP = (26, 199, 120)
GREEN_BOTTOM = (11, 143, 82)
WHITE = (255, 255, 255, 255)
GROOVE = (11, 143, 82, 255)


def gradient_square(size: int, radius: int) -> Image.Image:
    """竖直线性渐变的圆角方块（用逐行画线 + 圆角遮罩）。"""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(gradient)
    for y in range(size):
        ratio = y / max(size - 1, 1)
        color = tuple(
            round(GREEN_TOP[index] + (GREEN_BOTTOM[index] - GREEN_TOP[index]) * ratio) for index in range(3)
        )
        draw.line([(0, y), (size, y)], fill=(*color, 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    canvas.paste(gradient, (0, 0), mask)
    return canvas


def draw_bin(image: Image.Image) -> None:
    """白色垃圾桶：盖子 + 提手 + 桶身 + 两道凹槽。"""
    draw = ImageDraw.Draw(image)
    scale = image.width / 256

    def box(values: tuple[float, float, float, float]) -> list[float]:
        return [value * scale for value in values]

    # 提手
    draw.rounded_rectangle(box((106, 54, 150, 70)), radius=int(8 * scale), fill=WHITE)
    # 盖子
    draw.rounded_rectangle(box((58, 70, 198, 94)), radius=int(11 * scale), fill=WHITE)
    # 桶身
    draw.rounded_rectangle(box((72, 102, 184, 204)), radius=int(14 * scale), fill=WHITE)
    # 两道凹槽（用深绿画，形成"刻痕"）
    for x in (108, 140):
        draw.rounded_rectangle(box((x, 122, x + 8, 184)), radius=int(4 * scale), fill=GROOVE)


def main() -> int:
    icon = gradient_square(SIZE, radius=58)
    draw_bin(icon)

    ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    icon.save(ICON_PATH, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"  写出 {ICON_PATH.relative_to(REPO_ROOT)}  （{ICON_PATH.stat().st_size / 1024:.1f} KB）")

    FAVICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    icon.resize((64, 64), Image.LANCZOS).save(FAVICON_PATH, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    print(f"  写出 {FAVICON_PATH.relative_to(REPO_ROOT)}  （{FAVICON_PATH.stat().st_size / 1024:.1f} KB）")

    # 顺带存一张 png，方便放进 README 或 PPT
    png = REPO_ROOT / "assets" / "app-icon.png"
    icon.resize((256, 256), Image.LANCZOS).save(png)
    print(f"  写出 {png.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
