"""已标注数据的增强：旋转 / 翻转 / 明暗 / 噪点，**标注同步变换**。

为什么只做 90° 的整数倍和翻转
------------------------------
检测框是**轴对齐矩形**。旋转 90°/180°/270° 和翻转之后，矩形仍然是轴对齐的，
标注可以精确换算、框里不会多出任何背景像素；而任意角度旋转会把框"撑大"
（矩形转完要取外接矩形），框里混进背景，等于教模型学错。
明暗/噪点这类变换不改变位置，标注原样复制即可。

坐标换算（归一化 x,y ∈ [0,1]，图像坐标系 y 向下）
--------------------------------------------------
    旋转 90° 顺时针  (x, y) -> (1 - y, x)          → 框 (x1,y1,x2,y2) -> (1-y2, x1, 1-y1, x2)
    旋转 180°        (x, y) -> (1 - x, 1 - y)      → 框 -> (1-x2, 1-y2, 1-x1, 1-y1)
    旋转 270° 顺时针 (x, y) -> (y, 1 - x)          → 框 -> (y1, 1-x2, y2, 1-x1)
    水平翻转         (x, y) -> (1 - x, y)          → 框 -> (1-x2, y1, 1-x1, y2)
    垂直翻转         (x, y) -> (x, 1 - y)          → 框 -> (x1, 1-y2, x2, 1-y1)

生成的文件与原图**同目录**、文件名加后缀（`paper_x.jpg` -> `paper_x_r90.jpg`），
这样标注页能直接看到、生成数据集时也会自动带上；「清理增强图」只删带这些后缀的文件，
不会碰到原图。
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

import cv2
import numpy as np


class AugmentError(RuntimeError):
    """增强参数错误。"""


@dataclass(frozen=True)
class OpSpec:
    key: str
    label_zh: str
    kind: str  # geometry（会改标注） | photometric（只改像素）
    suffix: str
    hint: str = ""


OPS: tuple[OpSpec, ...] = (
    OpSpec("rot90", "旋转 90°（顺时针）", "geometry", "_r90", "标注框精确换算，画面变成竖幅"),
    OpSpec("rot180", "旋转 180°（上下颠倒）", "geometry", "_r180", "标注框精确换算，画幅不变"),
    OpSpec("rot270", "旋转 270°（逆时针 90°）", "geometry", "_r270", "标注框精确换算，画面变成竖幅"),
    OpSpec("flip_h", "水平翻转（左右镜像）", "geometry", "_fh", "标注框精确换算"),
    OpSpec("flip_v", "垂直翻转（上下镜像）", "geometry", "_fv", "标注框精确换算"),
    OpSpec("bright", "提亮（模拟强光）", "photometric", "_br", "位置不变，标注照抄；对付光照变化很有用"),
    OpSpec("dark", "压暗（模拟弱光）", "photometric", "_dk", "位置不变，标注照抄"),
    OpSpec("noise", "加噪点（模拟廉价摄像头）", "photometric", "_ns", "位置不变，标注照抄；提升暗光鲁棒性"),
)

OPS_BY_KEY: dict[str, OpSpec] = {op.key: op for op in OPS}
# 所有增强后缀：用于"这个文件是不是增强出来的"判断和清理
AUGMENT_SUFFIXES: tuple[str, ...] = tuple(op.suffix for op in OPS)
# 增强图的 JPEG 质量（和抓拍保持一致）
JPEG_QUALITY = 95

# 明暗强度（灰度加/减）
BRIGHTNESS_DELTA = 28
# 噪点标准差
NOISE_SIGMA = 8.0


def op_specs() -> list[dict[str, Any]]:
    """给前端用的变换清单。"""
    return [
        {"key": op.key, "label_zh": op.label_zh, "kind": op.kind, "suffix": op.suffix, "hint": op.hint}
        for op in OPS
    ]


def validate_ops(ops: list[str]) -> list[str]:
    """校验变换名，返回去重后的合法列表。"""
    result: list[str] = []
    for key in ops:
        name = str(key or "").strip()
        if name not in OPS_BY_KEY:
            raise AugmentError(f"未知的增强方式 {key!r}，可用：{'、'.join(OPS_BY_KEY)}")
        if name not in result:
            result.append(name)
    if not result:
        raise AugmentError("至少要选一种增强方式")
    return result


def suffix_of(op: str) -> str:
    spec = OPS_BY_KEY.get(op)
    if spec is None:
        raise AugmentError(f"未知的增强方式 {op!r}")
    return spec.suffix


def is_augmented(stem: str) -> bool:
    """文件名（不含扩展名）是不是增强产物。"""
    return any(stem.endswith(suffix) for suffix in AUGMENT_SUFFIXES)


def transform_image(image: np.ndarray, op: str, seed: int = 0) -> np.ndarray:
    """对图像做一次变换。"""
    if op == "rot90":
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if op == "rot180":
        return cv2.rotate(image, cv2.ROTATE_180)
    if op == "rot270":
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if op == "flip_h":
        return cv2.flip(image, 1)
    if op == "flip_v":
        return cv2.flip(image, 0)
    if op == "bright":
        return cv2.convertScaleAbs(image, alpha=1.0, beta=BRIGHTNESS_DELTA)
    if op == "dark":
        return cv2.convertScaleAbs(image, alpha=1.0, beta=-BRIGHTNESS_DELTA)
    if op == "noise":
        rng = np.random.default_rng(seed)
        noisy = image.astype(np.float32) + rng.normal(0.0, NOISE_SIGMA, image.shape)
        return np.clip(noisy, 0, 255).astype(np.uint8)
    raise AugmentError(f"未知的增强方式 {op!r}")


def transform_box(box: dict[str, Any], op: str) -> dict[str, Any]:
    """把一个归一化角点框按同样的变换换算过去（光度类变换位置不变）。"""
    x1, y1 = float(box["x1"]), float(box["y1"])
    x2, y2 = float(box["x2"]), float(box["y2"])
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1

    if op == "rot90":
        nx1, ny1, nx2, ny2 = 1.0 - y2, x1, 1.0 - y1, x2
    elif op == "rot180":
        nx1, ny1, nx2, ny2 = 1.0 - x2, 1.0 - y2, 1.0 - x1, 1.0 - y1
    elif op == "rot270":
        nx1, ny1, nx2, ny2 = y1, 1.0 - x2, y2, 1.0 - x1
    elif op == "flip_h":
        nx1, ny1, nx2, ny2 = 1.0 - x2, y1, 1.0 - x1, y2
    elif op == "flip_v":
        nx1, ny1, nx2, ny2 = x1, 1.0 - y2, x2, 1.0 - y1
    elif op in {"bright", "dark", "noise"}:
        nx1, ny1, nx2, ny2 = x1, y1, x2, y2
    else:
        raise AugmentError(f"未知的增强方式 {op!r}")

    clamp: Callable[[float], float] = lambda value: min(max(value, 0.0), 1.0)  # noqa: E731
    return {
        "class_name": str(box.get("class_name") or ""),
        "x1": clamp(nx1),
        "y1": clamp(ny1),
        "x2": clamp(nx2),
        "y2": clamp(ny2),
    }


def transform_boxes(boxes: list[dict[str, Any]], op: str) -> list[dict[str, Any]]:
    """整张图的标注一起换算。"""
    return [transform_box(box, op) for box in boxes]


def stable_seed(name: str, op: str) -> int:
    """由文件名+变换算出稳定的随机种子：同一张图重复增强结果一致（可重跑）。"""
    return int(zlib.crc32(f"{name}|{op}".encode("utf-8")) % (2**31 - 1))


__all__ = [
    "AUGMENT_SUFFIXES",
    "AugmentError",
    "OPS",
    "OPS_BY_KEY",
    "JPEG_QUALITY",
    "is_augmented",
    "op_specs",
    "stable_seed",
    "suffix_of",
    "transform_box",
    "transform_boxes",
    "transform_image",
    "validate_ops",
]
