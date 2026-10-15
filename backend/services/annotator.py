"""数据标注与数据集构建。

图片来自抓拍目录 `training/captures/<类别>/`（由 POST /api/capture 产生），
标签与图片同名同目录：`<名字>.txt`，内容是 YOLO 检测格式
    `类别索引 中心x 中心y 宽 高`（全部 0~1 归一化）

类别索引取自 `training/data.yaml` 的 names 顺序 —— 这是训练与推理共用的
唯一口径，标注时写错索引会导致整类串桶，所以这里强制以它为准。
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

_LOGGER = logging.getLogger(__name__)

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"})
# 小于这个尺寸（归一化）的框视为误点，直接丢弃
MIN_BOX_SIZE = 0.004
# 面积超过整图该比例的框，训练出来的模型会把整帧框住（托盘误检的主因），构建时给出警告
LARGE_BOX_AREA = 0.6
# 背景差分的默认参数
REFERENCE_NAME = "_reference.jpg"      # 参考图（空托盘）文件名，放在抓拍根目录
REFERENCE_PREFIX = "_"                 # 以此开头的文件/目录不算抓拍样本
DEFAULT_DIFF_THRESHOLD = 18            # 绝对噪声下限（灰度差 0~255）
DEFAULT_SIGMA_K = 8.0                  # 自适应阈值 = k × 实测噪声σ；越大越不敏感
EVIDENCE_RATIO = 2.0                   # 框内平均差异必须 >= 阈值 × 该倍数，才算"真有东西"
DEFAULT_MIN_AREA = 0.005               # 连通区域最小面积（占整图，0.5%）
DEFAULT_MAX_AREA = 0.6                 # 连通区域最大面积，和 detection.max_box_area 对齐
DEFAULT_DILATE = 1                     # 膨胀次数（轻微合并）——放在去噪之后
DEFAULT_PADDING = 0.01                 # 框外扩比例
DEFAULT_MIN_FILL = 0.35                # 轮廓实心度下限：轮廓面积 / 外接矩形面积
DEFAULT_BLUR = 5                       # 差分前的高斯模糊核（奇数），压噪点与 JPEG 伪影
MAX_AUTO_BOXES = 4                     # 单张最多给几个建议框
# 差分覆盖率超过这个值，说明参考图和当前画面根本对不上（光照/机位变了）
COVERAGE_LIMIT = 0.6


class AnnotateError(RuntimeError):
    """标注相关的参数/状态错误。"""


class ClaimRegistry:
    """内存里的"图片占用表"：防止两位标注员同时标同一张图。

    两个人在同一份数据上协作时，最容易出的事就是同时点开同一张、
    各自画一半、后保存的把先保存的覆盖掉。这里用"认领 + 自动过期"解决：

      - 打开一张图先认领；被别人认领且没过期时，前端会提示并拒绝打开
      - 认领有过期时间（默认 120s），前端每 45s 续一次；
        浏览器直接关掉/断网时靠过期自动释放，不会把图永久锁死
      - 只存内存、不落盘：后端重启即全部释放（协作场景下这是想要的行为）

    这里不记录"是谁标的"（不需要溯源），client_id 只用于区分浏览器标签页。
    """

    def __init__(self, ttl_seconds: float = 120.0) -> None:
        self._lock = threading.Lock()
        self._ttl = max(10.0, float(ttl_seconds))
        self._claims: dict[str, dict[str, Any]] = {}

    @property
    def ttl(self) -> float:
        return self._ttl

    def _purge(self, now: float) -> None:
        for name in [k for k, v in self._claims.items() if v["expires_at"] <= now]:
            self._claims.pop(name, None)

    def claim(self, name: str, client_id: str) -> dict[str, Any]:
        """尝试认领；被别人占着（且未过期）时返回 ok=False。"""
        now = time.time()
        with self._lock:
            self._purge(now)
            holder = self._claims.get(name)
            if holder is not None and holder["client_id"] != client_id:
                return {
                    "ok": False,
                    "name": name,
                    "holder": holder["client_id"],
                    "expires_in": round(max(0.0, holder["expires_at"] - now), 1),
                }
            self._claims[name] = {"client_id": str(client_id), "expires_at": now + self._ttl}
            return {"ok": True, "name": name, "holder": str(client_id), "expires_in": self._ttl}

    def release(self, name: str, client_id: str) -> bool:
        """主动释放；只有持有者能释放。"""
        with self._lock:
            holder = self._claims.get(name)
            if holder is None or holder["client_id"] != client_id:
                return False
            self._claims.pop(name, None)
            return True

    def release_all(self, client_id: str) -> int:
        """释放某个客户端持有的全部图片。"""
        with self._lock:
            names = [name for name, item in self._claims.items() if item["client_id"] == client_id]
            for name in names:
                self._claims.pop(name, None)
            return len(names)

    def snapshot(self) -> dict[str, str]:
        """当前占用情况：{图片名: client_id}（顺手清掉已过期的）。"""
        now = time.time()
        with self._lock:
            self._purge(now)
            return {name: item["client_id"] for name, item in self._claims.items()}

    def forget(self, name: str) -> None:
        """图片被删除时调用。"""
        with self._lock:
            self._claims.pop(name, None)


@dataclass(slots=True)
class Box:
    """一个标注框，坐标是相对整图的 0~1 归一化值。"""

    class_name: str
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return abs(self.x2 - self.x1)

    @property
    def height(self) -> float:
        return abs(self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    def normalized(self) -> "Box":
        x1, x2 = sorted((self.x1, self.x2))
        y1, y2 = sorted((self.y1, self.y2))
        return Box(
            class_name=self.class_name,
            x1=min(max(x1, 0.0), 1.0),
            y1=min(max(y1, 0.0), 1.0),
            x2=min(max(x2, 0.0), 1.0),
            y2=min(max(y2, 0.0), 1.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_name": self.class_name,
            "x1": round(self.x1, 6),
            "y1": round(self.y1, 6),
            "x2": round(self.x2, 6),
            "y2": round(self.y2, 6),
        }


def _link_or_copy(source: Path, target: Path) -> None:
    """同盘符用硬链接（零拷贝），失败退化为复制。"""
    try:
        os.link(source, target)
        return
    except OSError:
        pass
    shutil.copy2(source, target)


class Annotator:
    """抓拍图片的标注管理 + 数据集导出。"""

    def __init__(
        self,
        capture_base: Path,
        classes_yaml: Path,
        class_table: Optional[dict[str, Any]] = None,
        claim_ttl_seconds: float = 120.0,
    ):
        self._base = Path(capture_base)
        self._yaml = Path(classes_yaml)
        self._class_table = class_table or {}
        # 多人协作时的图片占用表（见 ClaimRegistry）
        self._claims = ClaimRegistry(claim_ttl_seconds)

    # ------------------------------------------------------------ 认领
    @property
    def claim_ttl(self) -> float:
        return self._claims.ttl

    def claim_image(self, name: str, client_id: str) -> dict[str, Any]:
        """认领一张图（打开前调用）；被他人占用时返回 ok=False。"""
        image = self.resolve_image(name)
        rel = image.relative_to(self._base).as_posix()
        return self._claims.claim(rel, client_id)

    def release_image(self, name: str, client_id: str) -> dict[str, Any]:
        """释放认领（切图/离开时调用）。"""
        raw = str(name or "").replace("\\", "/")
        released = self._claims.release(raw, client_id)
        return {"name": raw, "released": released}

    def release_client(self, client_id: str) -> int:
        """释放某个客户端的全部认领。"""
        return self._claims.release_all(client_id)

    def claims(self) -> dict[str, str]:
        return self._claims.snapshot()

    # ------------------------------------------------------------ 类别口径
    def names(self) -> dict[int, str]:
        """从 training/data.yaml 读 names：{索引: 类别名}。"""
        if not self._yaml.is_file():
            raise AnnotateError(f"找不到类别定义文件：{self._yaml}")
        try:
            import yaml

            data = yaml.safe_load(self._yaml.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise AnnotateError(f"解析 {self._yaml} 失败：{exc}") from exc

        raw = data.get("names")
        if isinstance(raw, dict):
            names = {int(key): str(value) for key, value in raw.items()}
        elif isinstance(raw, (list, tuple)):
            names = {index: str(value) for index, value in enumerate(raw)}
        else:
            raise AnnotateError(f"{self._yaml} 里没有可用的 names")
        if not names:
            raise AnnotateError(f"{self._yaml} 的 names 是空的")
        return dict(sorted(names.items()))

    def class_index(self) -> dict[str, int]:
        """{类别名: 索引}，写标签时用。"""
        return {name: index for index, name in self.names().items()}

    def class_list(self) -> list[dict[str, Any]]:
        """给前端的类别表：名字 / 索引 / 中文名。"""
        result = []
        for index, name in self.names().items():
            entry = self._class_table.get(name) if isinstance(self._class_table.get(name), dict) else {}
            result.append(
                {
                    "name": name,
                    "index": index,
                    "label_zh": str(entry.get("label_zh") or name),
                    "slot": entry.get("slot"),
                }
            )
        return result

    # ------------------------------------------------------------ 路径解析
    @property
    def base(self) -> Path:
        return self._base

    def resolve_image(self, name: str) -> Path:
        """把 `类别/文件名` 解析成绝对路径，并阻止目录穿越。"""
        raw = str(name or "").strip().replace("\\", "/")
        if not raw or raw.startswith("/") or ".." in raw.split("/"):
            raise AnnotateError(f"非法的图片名：{name!r}")
        path = (self._base / raw).resolve()
        try:
            if not path.is_relative_to(self._base.resolve()):
                raise AnnotateError(f"非法的图片路径：{name!r}")
        except AttributeError:  # pragma: no cover - Python < 3.9
            if not str(path).startswith(str(self._base.resolve())):
                raise AnnotateError(f"非法的图片路径：{name!r}") from None
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            raise AnnotateError(f"图片不存在：{name!r}")
        return path

    @staticmethod
    def label_path(image: Path) -> Path:
        return image.with_suffix(".txt")

    def delete_image(self, name: str) -> dict[str, Any]:
        """删除一张抓拍图及其同名标签（不可恢复）。

        参考图等以 `_` 开头的辅助文件不允许从这里删除。
        """
        raw = str(name or "").replace("\\", "/")
        if Path(raw).name.startswith(REFERENCE_PREFIX):
            raise AnnotateError("这是辅助文件（例如空托盘参考图），不能在标注页删除")
        image = self.resolve_image(raw)
        label = self.label_path(image)
        removed_label = False
        if label.is_file():
            label.unlink()
            removed_label = True
        image.unlink()
        self._claims.forget(raw)  # 图片没了，占用记录也一起清掉
        _LOGGER.info("已删除抓拍图 %s（标签%s）", raw, "一并删除" if removed_label else "本来就没有")
        return {"name": raw, "removed_label": removed_label, "remaining": len(self.list_images())}

    @staticmethod
    def _count_boxes(label: Path) -> int:
        if not label.is_file():
            return 0
        try:
            return sum(1 for line in label.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip())
        except OSError:
            return 0

    # ------------------------------------------------------------ 图片列表
    def list_images(self) -> list[dict[str, Any]]:
        """列出所有抓拍图片及其标注状态（含当前的认领情况）。"""
        items: list[dict[str, Any]] = []
        if not self._base.is_dir():
            return items
        claims = self._claims.snapshot()
        for category_dir in sorted(self._base.iterdir()):
            if not category_dir.is_dir() or category_dir.name.startswith(REFERENCE_PREFIX):
                continue
            for path in sorted(category_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                if path.name.startswith(REFERENCE_PREFIX):  # 参考图之类的辅助文件不算样本
                    continue
                boxes = self._count_boxes(self.label_path(path))
                rel = path.relative_to(self._base).as_posix()
                items.append(
                    {
                        "name": rel,
                        "category": category_dir.name,
                        "labeled": boxes > 0,
                        "boxes": boxes,
                        # 被谁认领着（None = 没人占）；前端据此显示"标注中"并跳过
                        "claimed_by": claims.get(rel),
                    }
                )
        return items

    # ------------------------------------------------------------ 读写标签
    def read_labels(self, name: str) -> list[dict[str, Any]]:
        image = self.resolve_image(name)
        label = self.label_path(image)
        if not label.is_file():
            return []
        index_to_name = {index: cls for cls, index in self.class_index().items()}
        boxes: list[dict[str, Any]] = []
        for line in label.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) != 5:
                continue
            try:
                class_id = int(float(parts[0]))
                cx, cy, width, height = (float(value) for value in parts[1:])
            except ValueError:
                continue
            box = Box(
                class_name=index_to_name.get(class_id, f"class_{class_id}"),
                x1=cx - width / 2,
                y1=cy - height / 2,
                x2=cx + width / 2,
                y2=cy + height / 2,
            ).normalized()
            boxes.append(box.to_dict())
        return boxes

    def write_labels(self, name: str, raw_boxes: list[dict[str, Any]]) -> dict[str, Any]:
        """保存一张图的标注；空列表等于清空该图标注。返回统计。"""
        image = self.resolve_image(name)
        index = self.class_index()
        lines: list[str] = []
        skipped: list[str] = []
        large = 0
        for raw in raw_boxes:
            class_name = str(raw.get("class_name") or "").strip()
            if class_name not in index:
                skipped.append(f"未知类别 {class_name!r}")
                continue
            try:
                box = Box(
                    class_name=class_name,
                    x1=float(raw["x1"]),
                    y1=float(raw["y1"]),
                    x2=float(raw["x2"]),
                    y2=float(raw["y2"]),
                ).normalized()
            except (KeyError, TypeError, ValueError) as exc:
                skipped.append(f"坐标非法（{exc}）")
                continue
            if box.width < MIN_BOX_SIZE or box.height < MIN_BOX_SIZE:
                skipped.append("框太小，已忽略")
                continue
            if box.area > LARGE_BOX_AREA:
                large += 1
            lines.append(
                f"{index[class_name]} {(box.x1 + box.x2) / 2:.6f} {(box.y1 + box.y2) / 2:.6f}"
                f" {box.width:.6f} {box.height:.6f}"
            )

        label = self.label_path(image)
        if lines:
            label.write_text("\n".join(lines) + "\n", encoding="utf-8")
        elif label.is_file():
            label.unlink()
        return {"count": len(lines), "skipped": skipped, "large": large, "labeled": bool(lines)}

    # -------------------------------------------- 参考图 + 背景差分自动标注
    @property
    def reference_path(self) -> Path:
        """空托盘参考图的位置。"""
        return self._base / REFERENCE_NAME

    def reference_info(self) -> Optional[dict[str, Any]]:
        path = self.reference_path
        if not path.is_file():
            return None
        info: dict[str, Any] = {"name": REFERENCE_NAME, "mtime": path.stat().st_mtime, "size": None}
        image = cv2.imread(str(path))
        if image is not None:
            info["size"] = [int(image.shape[1]), int(image.shape[0])]
        return info

    def set_reference_from_image(self, name: str) -> dict[str, Any]:
        """把某张抓拍图复制成参考图。"""
        source = self.resolve_image(name)
        target = self.reference_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        _LOGGER.info("参考图已设为 %s（来自 %s）", target.name, name)
        return self.reference_info() or {}

    def set_reference_from_frame(self, frame: np.ndarray) -> dict[str, Any]:
        """把当前摄像头画面存成参考图。"""
        target = self.reference_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95]):
            raise AnnotateError("保存参考图失败（写入被拒绝？）")
        _LOGGER.info("参考图已用当前摄像头画面更新：%s", target.name)
        return self.reference_info() or {}

    def set_reference_from_frames(self, frames: list[np.ndarray]) -> dict[str, Any]:
        """用多帧**逐像素中值**合成参考图。

        这是让背景差分稳下来的关键一步：单帧参考图里带着传感器噪声，
        每张新图的噪声又不一样，差分出来就是满屏小噪点；
        取中值可以把随机噪声基本消掉，只留下真正稳定的画面。
        """
        if len(frames) < 2:
            raise AnnotateError("至少需要 2 帧才能取中值合成参考图")
        shapes = {frame.shape for frame in frames}
        if len(shapes) != 1:
            raise AnnotateError("多帧尺寸不一致，无法合成参考图")
        stack = np.stack([frame.astype(np.float32) for frame in frames], axis=0)
        median = np.median(stack, axis=0).astype(np.uint8)
        target = self.reference_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), median, [int(cv2.IMWRITE_JPEG_QUALITY), 95]):
            raise AnnotateError("保存参考图失败（写入被拒绝？）")
        _LOGGER.info("参考图已用 %d 帧中值合成：%s", len(frames), target.name)
        return self.reference_info() or {}

    def clear_reference(self) -> bool:
        path = self.reference_path
        if path.is_file():
            path.unlink()
            _LOGGER.info("参考图已删除")
            return True
        return False

    def auto_boxes(
        self,
        name: str,
        class_name: Optional[str] = None,
        threshold: int = DEFAULT_DIFF_THRESHOLD,
        min_area: float = DEFAULT_MIN_AREA,
        max_area: float = DEFAULT_MAX_AREA,
        dilate: int = DEFAULT_DILATE,
        padding: float = DEFAULT_PADDING,
        sigma_k: float = DEFAULT_SIGMA_K,
        min_fill: float = DEFAULT_MIN_FILL,
        blur: int = DEFAULT_BLUR,
    ) -> dict[str, Any]:
        """背景差分找物体，返回建议框（**不写文件**，交给前端确认后保存）。

        原理：机位和托盘固定，所以"当前图 - 空托盘参考图"剩下的差异就是物体本身。

        为了不"太灵敏"（避免一堆不存在的框），这里做了四件事：
          1. 差分前先高斯模糊，压掉传感器噪点与 JPEG 伪影；
          2. 阈值不写死，用全图差异的 MAD 估出**当前噪声水平**，
             阈值 = max(下限, k×σ)，光照/噪声变了也能自适应；
          3. 形态学顺序反过来：**先开运算去碎点，再闭运算补空洞，最后才轻微膨胀**。
             旧代码先膨胀后开，等于先把噪点吹大再想擦掉，根本擦不掉；
          4. 每个候选区域还要过三关：面积区间、轮廓实心度、框内平均差异强度。
        """
        started = time.perf_counter()
        if not self.reference_path.is_file():
            raise AnnotateError(
                "还没有参考图：先在「自动标注」里点『用当前画面设为参考图』"
                "（托盘清空、机位光照固定时拍一张）"
            )

        image_path = self.resolve_image(name)
        image = cv2.imread(str(image_path))
        if image is None:
            raise AnnotateError(f"读不出图片：{name}")
        reference = cv2.imread(str(self.reference_path))
        if reference is None:
            raise AnnotateError("参考图损坏，请重新设置")

        height, width = image.shape[:2]
        if reference.shape[:2] != (height, width):
            reference = cv2.resize(reference, (width, height), interpolation=cv2.INTER_AREA)

        # 1) 差分前先模糊：压掉传感器噪点与 JPEG 伪影（核必须是奇数且 >=1）
        kernel_size = max(1, int(blur) | 1)
        gray_now = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray_ref = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
        if kernel_size > 1:
            gray_now = cv2.GaussianBlur(gray_now, (kernel_size, kernel_size), 0)
            gray_ref = cv2.GaussianBlur(gray_ref, (kernel_size, kernel_size), 0)
        diff = cv2.absdiff(gray_now, gray_ref)

        # 2) 用 MAD 估当前噪声σ（全图大部分区域没变化，所以中值/MAD 反映的就是噪声）
        median = float(np.median(diff))
        mad = float(np.median(np.abs(diff.astype(np.float32) - median)))
        noise_sigma = max(1.4826 * mad, 0.5)
        effective = max(float(threshold), float(sigma_k) * noise_sigma)
        _, mask = cv2.threshold(diff, effective, 255, cv2.THRESH_BINARY)

        # 3) 形态学：先开（去碎点）-> 再闭（补空洞）-> 最后才轻微膨胀
        small = np.ones((3, 3), np.uint8)
        big = np.ones((7, 7), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, small, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, big, iterations=1)
        if dilate > 0:
            mask = cv2.dilate(mask, small, iterations=int(dilate))

        frame_area = float(height * width) or 1.0
        coverage = float(np.count_nonzero(mask)) / frame_area
        if coverage > COVERAGE_LIMIT:
            raise AnnotateError(
                f"画面与参考图差异过大（{coverage:.0%} 的区域都不同）："
                "参考图的光照/机位和这张对不上，请重新拍一张空托盘参考图"
            )

        # 4) 候选区域三关筛选：面积区间 / 实心度 / 框内平均差异强度
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes: list[dict[str, Any]] = []
        rejected = {"area": 0, "fill": 0, "evidence": 0}
        for contour in contours:
            x, y, box_w, box_h = cv2.boundingRect(contour)
            if box_w <= 0 or box_h <= 0:
                continue
            area_ratio = (box_w * box_h) / frame_area
            if area_ratio < min_area or area_ratio > max_area:
                rejected["area"] += 1
                continue
            fill = float(cv2.contourArea(contour)) / float(box_w * box_h)
            if fill < min_fill:
                rejected["fill"] += 1
                continue
            # 框内平均差异必须明显高于阈值，否则大概率是残余噪点或光照漂移
            patch = diff[y : y + box_h, x : x + box_w]
            mean_diff = float(patch.mean()) if patch.size else 0.0
            if mean_diff < effective * EVIDENCE_RATIO:
                rejected["evidence"] += 1
                continue

            pad_x = padding * width
            pad_y = padding * height
            boxes.append(
                {
                    "class_name": class_name or "",
                    "x1": max(0.0, (x - pad_x) / width),
                    "y1": max(0.0, (y - pad_y) / height),
                    "x2": min(1.0, (x + box_w + pad_x) / width),
                    "y2": min(1.0, (y + box_h + pad_y) / height),
                    "_score": round(mean_diff * (box_w * box_h) ** 0.5, 2),
                }
            )

        # 按"差异强度 × 尺寸"排序，只保留最像物体的几个
        boxes.sort(key=lambda item: item.get("_score", 0.0), reverse=True)
        boxes = boxes[:MAX_AUTO_BOXES]

        info = {
            "method": "background-diff",
            "reference": REFERENCE_NAME,
            "threshold": int(threshold),
            "effective_threshold": round(effective, 2),
            "noise_sigma": round(noise_sigma, 2),
            "sigma_k": float(sigma_k),
            "min_area": float(min_area),
            "min_fill": float(min_fill),
            "coverage": round(coverage, 5),
            "contours": len(contours),
            "regions": len(boxes),
            "rejected": rejected,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        _LOGGER.info(
            "背景差分 %s：噪声σ=%.2f 阈值=%.1f -> %d 个区域（覆盖率 %.2f%%，已滤掉 面积%d/实心度%d/证据%d）",
            name,
            noise_sigma,
            effective,
            len(boxes),
            coverage * 100,
            rejected["area"],
            rejected["fill"],
            rejected["evidence"],
        )
        return {"boxes": boxes, "info": info}

    def auto_label_all(
        self,
        class_name: Optional[str] = None,
        only_unlabeled: bool = True,
        overwrite: bool = False,
        **options: Any,
    ) -> dict[str, Any]:
        """批量自动标注：直接写标签文件（background 目录跳过，它本来就是空标签样本）。"""
        index = self.class_index()
        results: list[dict[str, Any]] = []
        written = 0
        skipped = 0
        failed = 0

        for item in self.list_images():
            name = item["name"]
            category = item["category"]
            if category == "background":
                skipped += 1
                results.append({"name": name, "status": "skipped", "reason": "背景图不打框"})
                continue
            if only_unlabeled and item["labeled"] and not overwrite:
                skipped += 1
                continue
            target_class = class_name or category
            if target_class not in index:
                skipped += 1
                results.append(
                    {
                        "name": name,
                        "status": "skipped",
                        "reason": f"目录名 {category} 不是已知类别，请在界面上手动选类别",
                    }
                )
                continue
            try:
                found = self.auto_boxes(name, target_class, **options)
            except AnnotateError as exc:
                failed += 1
                results.append({"name": name, "status": "failed", "reason": str(exc)})
                continue
            if not found["boxes"]:
                failed += 1
                results.append(
                    {"name": name, "status": "empty", "reason": "没找到明显物体，可调低阈值或调小最小面积"}
                )
                continue
            self.write_labels(name, found["boxes"])
            written += 1
            results.append(
                {
                    "name": name,
                    "status": "ok",
                    "boxes": len(found["boxes"]),
                    "coverage": found["info"]["coverage"],
                }
            )

        summary = {"total": len(results), "written": written, "skipped": skipped, "failed": failed}
        _LOGGER.info("批量自动标注完成：%s", summary)
        return {"summary": summary, "results": results}

    # ------------------------------------------------------- 导出训练集
    def build_dataset(
        self,
        output: Path,
        yaml_path: Path,
        val_ratio: float = 0.2,
        include_background: bool = True,
        seed: int = 0,
    ) -> dict[str, Any]:
        """把已标注的抓拍图导出成 YOLO 检测数据集 + 对应的 data.yaml。"""
        items = self.list_images()
        if not items:
            raise AnnotateError("抓拍目录里还没有图片，先在「摄像头」页抓拍几张")

        labeled: list[tuple[Path, Path]] = []
        backgrounds: list[Path] = []
        for item in items:
            image = self.resolve_image(item["name"])
            label = self.label_path(image)
            if item["boxes"] > 0:
                labeled.append((image, label))
            elif include_background and item["category"] == "background":
                backgrounds.append(image)

        if len(labeled) < 2:
            raise AnnotateError(
                f"至少需要 2 张已标注的图片才能划分训练/验证集，当前只有 {len(labeled)} 张"
            )

        rng = random.Random(seed)
        rng.shuffle(labeled)
        rng.shuffle(backgrounds)

        val_ratio = min(max(val_ratio, 0.0), 0.5)
        val_count = max(1, round(len(labeled) * val_ratio))
        plan: list[tuple[str, Path, Optional[Path]]] = []
        for index, (image, label) in enumerate(labeled):
            plan.append(("val" if index < val_count else "train", image, label))
        bg_val = max(0, round(len(backgrounds) * val_ratio))
        for index, image in enumerate(backgrounds):
            plan.append(("val" if index < bg_val else "train", image, None))

        if output.exists():
            shutil.rmtree(output)

        stats = {"train_images": 0, "val_images": 0, "boxes": 0, "backgrounds": 0, "large_boxes": 0}
        for split, image, label in plan:
            image_dir = output / split / "images"
            label_dir = output / split / "labels"
            image_dir.mkdir(parents=True, exist_ok=True)
            label_dir.mkdir(parents=True, exist_ok=True)

            target = image_dir / f"{image.parent.name}__{image.name}"
            _link_or_copy(image, target)
            text = label.read_text(encoding="utf-8") if label is not None else ""
            (label_dir / f"{target.stem}.txt").write_text(text, encoding="utf-8")

            stats[f"{split}_images"] += 1
            if label is None:
                stats["backgrounds"] += 1
            else:
                for line in text.splitlines():
                    parts = line.split()
                    if len(parts) != 5:
                        continue
                    stats["boxes"] += 1
                    try:
                        if float(parts[3]) * float(parts[4]) > LARGE_BOX_AREA:
                            stats["large_boxes"] += 1
                    except ValueError:
                        pass

        # 写 data.yaml：train/val 路径相对 yaml 所在目录
        try:
            relative = os.path.relpath(output, yaml_path.parent).replace("\\", "/")
        except ValueError:  # 跨盘符
            relative = output.as_posix()
        names = self.names()
        lines = [
            "# 由「数据标注」页生成：来自 training/captures 的人工标注",
            f"# 图片：train {stats['train_images']} / val {stats['val_images']}"
            f"（其中背景图 {stats['backgrounds']}），标注框 {stats['boxes']}",
            "",
            f"train: {relative}/train/images",
            f"val: {relative}/val/images",
            "",
            f"nc: {len(names)}",
            "",
            "names:",
        ]
        lines.extend(f"  {index}: {names[index]}" for index in sorted(names))
        yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = {
            "output": str(output),
            "yaml": str(yaml_path),
            "classes": len(names),
            **stats,
        }
        _LOGGER.info("标注数据集已生成：%s", result)
        return result
