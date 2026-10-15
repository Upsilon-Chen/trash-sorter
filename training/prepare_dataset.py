#!/usr/bin/env python3
"""把分割（多边形）标注转换成检测（bbox）标注，生成一份可直接训练检测模型的数据集。

⚠ 当前项目的主流程是「自己抓拍 → 网页标注 → 生成数据集」（见 README 的"数据采集 → 标注 → 训练"），
   本脚本是为早期从 Roboflow / trashnet 多边形数据集转换而写的工具，保留备用。
   它默认读取源数据集自己的 data.yaml 决定类别，所以不会和当前 7 类体系混淆。

为什么需要它
------------
trashnet/ 是 Roboflow 的 **YOLOv8 分割**导出：每行是
    class x1 y1 x2 y2 x3 y3 ... xn yn      （多边形顶点，坐标 0~1 归一化）
而本项目的分拣只需要"这是什么 + 在哪（bbox）"，用检测模型（yolov8n.pt）比分割模型
（yolov8n-seg.pt）在 CPU 上快 2~3 倍、模型也更小。本脚本把多边形取外接矩形，
生成一份检测格式的数据集。

**源数据集不会被修改**：图片用硬链接（同盘符时零拷贝），失败自动退化为复制。

用法
----
    python prepare_dataset.py --dry-run          # 只看统计，不写文件
    python prepare_dataset.py --max-annotation-area 0.9 --skip-full-frame
    python prepare_dataset.py --backgrounds ..\\backgrounds   # 把空托盘照片当背景图加进去
    python train.py --data data-det.yaml --model yolov8n.pt --epochs 100 --imgsz 640

`--max-annotation-area 0.9` 的作用：把"某个类别的框占了整幅画面 90% 以上"的图片整张剔除。
这类标注会让模型学成"整个画面 = 某类"，于是把托盘、桌面这种占满画面的东西识别成该类。
（早期的 trashnet 数据集里 paper 有 69 个、cardboard 有 54 个这种整图标注。）
`--backgrounds` 则相反：把空托盘照片当作"没有目标"的背景样本喂给模型。

如果不想转换，也可以直接用源数据集训练分割模型：
    python train.py --data data.yaml --model yolov8n-seg.pt --epochs 100 --imgsz 640
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = SCRIPT_DIR.parent / "trashnet"
DEFAULT_OUTPUT = SCRIPT_DIR / "datasets" / "trash-det"
DEFAULT_YAML = SCRIPT_DIR / "data-det.yaml"

# 源数据集里的划分目录名 -> 生成配置里的键名
SPLITS: tuple[tuple[str, str], ...] = (("train", "train"), ("valid", "val"), ("test", "test"))
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"})

# 外接矩形面积超过整图这个比例，就认为标注"框住了整张图"，属于可疑标注
FULL_FRAME_AREA = 0.95
# 宽或高小于该值视为退化框（多边形近似一条线）
DEGENERATE_SIZE = 1e-4


class PrepareError(RuntimeError):
    """数据集或参数问题。"""


@dataclass
class SplitStats:
    split: str
    images: int = 0
    labels: int = 0
    missing_labels: int = 0
    orphan_labels: int = 0
    lines_in: int = 0
    converted: int = 0
    passthrough: int = 0
    skipped: int = 0
    hardlinks: int = 0
    vertices_total: int = 0
    vertices_max: int = 0
    degenerate: int = 0
    class_counts: dict[int, int] = field(default_factory=dict)
    full_frame: dict[int, int] = field(default_factory=dict)
    full_frame_rect: dict[int, int] = field(default_factory=dict)
    dropped_images: int = 0
    dropped_annotations: int = 0
    dropped_full_frame_images: int = 0
    dropped_large_images: int = 0
    largest_kept: float = 0.0
    samples: list[str] = field(default_factory=list)

    @property
    def avg_vertices(self) -> float:
        return (self.vertices_total / self.converted) if self.converted else 0.0


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def read_source_names(source: Path, override: Optional[str]) -> dict[int, str]:
    """确定类别表：优先 --names，其次源数据集的 data.yaml。"""
    if override:
        names = [item.strip() for item in override.split(",") if item.strip()]
        return {index: name for index, name in enumerate(names)}

    yaml_path = source / "data.yaml"
    if not yaml_path.is_file():
        raise PrepareError(
            f"源数据集缺少 {yaml_path}，请用 --names paper,plastic,battery,medicine,peel,mask,tableware 指定类别"
        )

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - ultralytics 自带 PyYAML
        raise PrepareError(f"需要 PyYAML：pip install pyyaml（{exc}）") from exc

    with yaml_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    raw = data.get("names")
    if isinstance(raw, dict):
        names_map = {int(key): str(value) for key, value in raw.items()}
    elif isinstance(raw, (list, tuple)):
        names_map = {index: str(value) for index, value in enumerate(raw)}
    else:
        raise PrepareError(f"{yaml_path} 里没有可用的 names")
    return dict(sorted(names_map.items()))


def polygon_to_box(coords: list[float]) -> tuple[float, float, float, float]:
    """多边形顶点 -> YOLO 检测框 (cx, cy, w, h)，全部 0~1 归一化。"""
    xs = coords[0::2]
    ys = coords[1::2]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    # 夹到图像范围内，避免个别越界点把框顶出画面
    x_min = min(max(x_min, 0.0), 1.0)
    x_max = min(max(x_max, 0.0), 1.0)
    y_min = min(max(y_min, 0.0), 1.0)
    y_max = min(max(y_max, 0.0), 1.0)
    return (x_min + x_max) / 2.0, (y_min + y_max) / 2.0, x_max - x_min, y_max - y_min


def is_full_frame_rect(parts: list[str]) -> bool:
    """判断一行标注是不是"整图矩形"：4 个顶点、且贴满整幅图（0,0)-(1,1)。

    这类标注是典型的偷懒/自动标注（等于说"整张图都是这一类"），
    留着会让模型学会把整帧框成该类别。
    """
    if len(parts) != 9:
        return False
    try:
        values = [float(value) for value in parts[1:]]
    except ValueError:
        return False
    xs = values[0::2]
    ys = values[1::2]
    return min(xs) <= 0.001 and min(ys) <= 0.001 and max(xs) >= 0.999 and max(ys) >= 0.999


def count_full_frame_rects(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and is_full_frame_rect(line.split()))


def max_annotation_area(text: str) -> float:
    """返回该标注文件里最大的外接矩形面积占比（0~1）；解析不了的行忽略。"""
    largest = 0.0
    for raw_line in text.splitlines():
        parts = raw_line.strip().split()
        if len(parts) < 5:
            continue
        try:
            values = [float(value) for value in parts[1:]]
        except ValueError:
            continue
        if len(values) == 4:
            area = values[2] * values[3]
        elif len(values) >= 6 and len(values) % 2 == 0:
            xs = values[0::2]
            ys = values[1::2]
            area = max(0.0, min(max(xs), 1.0) - max(min(xs), 0.0)) * max(
                0.0, min(max(ys), 1.0) - max(min(ys), 0.0)
            )
        else:
            continue
        largest = max(largest, area)
    return largest


def convert_label_text(text: str, class_count: int, stats: SplitStats, stem: str) -> list[str]:
    """把一份分割标注文本转换成检测标注行。已有 5 列 bbox 行原样透传。"""
    output: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        stats.lines_in += 1
        parts = line.split()
        try:
            class_id = int(float(parts[0]))
            values = [float(value) for value in parts[1:]]
        except ValueError:
            stats.skipped += 1
            if len(stats.samples) < 5:
                stats.samples.append(f"{stem}.txt: 非数字字段")
            continue

        if class_id < 0 or class_id >= class_count:
            stats.skipped += 1
            if len(stats.samples) < 5:
                stats.samples.append(f"{stem}.txt: 类别 {class_id} 超出 0..{class_count - 1}")
            continue

        if len(values) == 4:
            # 已经是 bbox
            cx, cy, width, height = values
            stats.passthrough += 1
        elif len(values) >= 6 and len(values) % 2 == 0:
            # 多边形：class + 2N 个坐标
            points = len(values) // 2
            xs = values[0::2]
            ys = values[1::2]
            raw = (min(xs), min(ys), max(xs), max(ys))
            stats.vertices_total += points
            stats.vertices_max = max(stats.vertices_max, points)
            # 4 个顶点、且贴满整幅图 —— 典型的"整图矩形"偷懒标注
            if points == 4 and raw[0] <= 0.001 and raw[1] <= 0.001 and raw[2] >= 0.999 and raw[3] >= 0.999:
                stats.full_frame_rect[class_id] = stats.full_frame_rect.get(class_id, 0) + 1
            cx, cy, width, height = polygon_to_box(values)
            stats.converted += 1
        else:
            stats.skipped += 1
            if len(stats.samples) < 5:
                stats.samples.append(f"{stem}.txt: 无法识别的列数 {len(parts)}")
            continue

        if width < DEGENERATE_SIZE or height < DEGENERATE_SIZE:
            stats.degenerate += 1
            continue

        stats.class_counts[class_id] = stats.class_counts.get(class_id, 0) + 1
        if width * height > FULL_FRAME_AREA:
            stats.full_frame[class_id] = stats.full_frame.get(class_id, 0) + 1

        output.append(f"{class_id} {cx:.6f} {cy:.6f} {width:.6f} {height:.6f}")
    return output


def link_or_copy(source: Path, target: Path, force_copy: bool) -> bool:
    """硬链接（零拷贝）；不支持时退化为复制。返回 True 表示用了硬链接。"""
    if not force_copy:
        try:
            os.link(source, target)
            return True
        except OSError:
            pass
    shutil.copy2(source, target)
    return False


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 YOLOv8 分割（多边形）标注转换成检测（bbox）数据集",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="源数据集目录（含 train/valid/test）")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出数据集目录")
    parser.add_argument("--yaml", default=str(DEFAULT_YAML), help="生成的检测数据集配置路径")
    parser.add_argument("--names", default=None, help="类别名逗号分隔；默认读源数据集的 data.yaml")
    parser.add_argument("--force-copy", action="store_true", help="强制复制图片而不是硬链接")
    parser.add_argument(
        "--skip-full-frame",
        action="store_true",
        help="跳过含『整图矩形』标注的图片（该标注等于声明整张图都属于某类，会污染训练）",
    )
    parser.add_argument(
        "--max-annotation-area",
        type=float,
        default=0.0,
        help="跳过含『外接矩形面积超过该比例』标注的图片，0 表示关闭。"
        "推荐 0.9：这类标注会把托盘/桌面这种占满画面的东西教成某一类",
    )
    parser.add_argument(
        "--backgrounds",
        default=None,
        help="背景图目录（例如空托盘的照片）：图片会被复制进 train/images 并写入空标签，"
        "教模型认识『托盘本身不是垃圾』",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写任何文件")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    yaml_path = Path(args.yaml).expanduser().resolve()

    try:
        if not source.is_dir():
            raise PrepareError(f"源数据集目录不存在：{source}")
        names = read_source_names(source, args.names)
        if not names:
            raise PrepareError("类别表为空")
        class_count = len(names)

        print("=" * 68)
        print("分割标注 -> 检测标注 转换")
        print("=" * 68)
        print(f"  源数据集 : {source}")
        print(f"  输出目录 : {output}{'（--dry-run，不写入）' if args.dry_run else ''}")
        print(f"  类别     : {class_count} 个 -> {', '.join(names[index] for index in sorted(names))}")

        # 输出目录是"派生数据"，每次重建都先清空，避免上一轮被剔除的图片残留下来
        if not args.dry_run:
            if output == source or (output.exists() and source.is_relative_to(output)):
                raise PrepareError(f"输出目录不能是源数据集或其父目录：{output}")
            if output.exists():
                shutil.rmtree(output)
                print(f"  已清空旧的输出目录：{output}")

        all_stats: list[SplitStats] = []
        for source_split, _yaml_key in SPLITS:
            images_dir = source / source_split / "images"
            labels_dir = source / source_split / "labels"
            stats = SplitStats(split=source_split)
            if not images_dir.is_dir():
                print(f"\n  ! 跳过 {source_split}：找不到 {images_dir}")
                continue

            images = sorted(
                path for path in images_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            )
            stats.images = len(images)

            if not args.dry_run:
                (output / source_split / "images").mkdir(parents=True, exist_ok=True)
                (output / source_split / "labels").mkdir(parents=True, exist_ok=True)

            hardlinks = 0
            for image in images:
                label_path = labels_dir / f"{image.stem}.txt"
                if not label_path.is_file():
                    stats.missing_labels += 1
                    if not args.dry_run:
                        link_or_copy(image, output / source_split / "images" / image.name, args.force_copy)
                    continue
                stats.labels += 1
                text = label_path.read_text(encoding="utf-8", errors="ignore")
                if args.skip_full_frame:
                    suspect = count_full_frame_rects(text)
                    if suspect:
                        stats.dropped_images += 1
                        stats.dropped_full_frame_images += 1
                        stats.dropped_annotations += suspect
                        continue  # 整张图不进入数据集，避免教出"整帧=某类"
                if args.max_annotation_area > 0:
                    largest = max_annotation_area(text)
                    stats.largest_kept = max(stats.largest_kept, largest)
                    if largest > args.max_annotation_area:
                        stats.dropped_large_images += 1
                        stats.dropped_images += 1
                        stats.dropped_annotations += 1
                        continue  # 超大标注的图整张剔除
                lines = convert_label_text(text, class_count, stats, image.stem)
                if not args.dry_run:
                    if link_or_copy(image, output / source_split / "images" / image.name, args.force_copy):
                        hardlinks += 1
                    target = output / source_split / "labels" / f"{image.stem}.txt"
                    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            if not args.dry_run:
                stats.hardlinks = hardlinks
            all_stats.append(stats)

        # ------------------------------------------------------------ 报告
        print("\n各划分统计")
        for stats in all_stats:
            print(
                f"  {stats.split:<6} {stats.images:>5} 图 / {stats.labels:>5} 标签"
                f" | 标注行 {stats.lines_in:>5} -> 转换 {stats.converted:>5}"
                f" 透传 {stats.passthrough} 跳过 {stats.skipped}"
                f" | 平均顶点 {stats.avg_vertices:.1f} 最多 {stats.vertices_max}"
            )
            if stats.missing_labels:
                print(f"         {stats.missing_labels} 张图没有标签（按背景图处理）")
            if stats.dropped_images:
                print(
                    f"         剔除 {stats.dropped_images} 张问题图片"
                    f"（整图矩形 {stats.dropped_full_frame_images} 张"
                    f" + 面积超阈值 {stats.dropped_large_images} 张）"
                )
            if not args.dry_run and stats.hardlinks:
                print(f"         图片硬链接 {stats.hardlinks} 个（零拷贝），其余为复制")
            if stats.samples:
                for sample in stats.samples:
                    print(f"         - {sample}")

        totals: dict[int, int] = {}
        full_frame: dict[int, int] = {}
        full_frame_rect: dict[int, int] = {}
        degenerate = 0
        for stats in all_stats:
            degenerate += stats.degenerate
            for key, value in stats.class_counts.items():
                totals[key] = totals.get(key, 0) + value
            for key, value in stats.full_frame.items():
                full_frame[key] = full_frame.get(key, 0) + value
            for key, value in stats.full_frame_rect.items():
                full_frame_rect[key] = full_frame_rect.get(key, 0) + value

        print("\n类别实例总数（转换后）")
        for index in sorted(totals):
            print(f"  {index}  {names.get(index, index):<12} {totals[index]:>6}")

        if degenerate:
            print(f"\n  ! 有 {degenerate} 个标注退化成线/点（宽或高≈0），已丢弃")
        if full_frame_rect:
            print("  ! 整图矩形标注（4 个顶点贴满整幅图）：这类标注等于声明整张图都属于该类别，")
            print("    会让模型学会『整幅画面＝某类』，检测时容易把整帧框成这一类：")
            for index in sorted(full_frame_rect):
                print(f"      {names.get(index, index):<12} {full_frame_rect[index]:>5} 个"
                      f"（占该类 {full_frame_rect[index] / max(totals.get(index, 1), 1) * 100:.0f}%）")
            if not args.skip_full_frame:
                print("    建议加 --skip-full-frame 重新生成：把这类图片整张剔除，避免教出『整帧=某类』")
        other_full = {
            index: count - full_frame_rect.get(index, 0)
            for index, count in full_frame.items()
            if count - full_frame_rect.get(index, 0) > 0
        }
        if other_full:
            print("  ! 另外这些类别的外接矩形面积 >95%（不是整图矩形，可能是物体真的占满画面）：")
            for index in sorted(other_full):
                print(f"      {names.get(index, index):<12} {other_full[index]:>5} 个")

        # -------------------------------------------------- 背景图（空托盘等）
        if args.backgrounds:
            bg_dir = Path(args.backgrounds).expanduser().resolve()
            if not bg_dir.is_dir():
                raise PrepareError(f"背景图目录不存在：{bg_dir}")
            bg_files = sorted(
                path
                for path in bg_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            )
            if not args.dry_run:
                target_images = output / "train" / "images"
                target_labels = output / "train" / "labels"
                target_images.mkdir(parents=True, exist_ok=True)
                target_labels.mkdir(parents=True, exist_ok=True)
                for index, path in enumerate(bg_files):
                    stem = f"bg_{index:04d}_{path.stem}"
                    link_or_copy(path, target_images / f"{stem}{path.suffix.lower()}", args.force_copy)
                    (target_labels / f"{stem}.txt").write_text("", encoding="utf-8")
            print(
                f"\n背景图：{len(bg_files)} 张（写空标签，教模型『托盘本身不是垃圾』）"
                + ("，--dry-run 未写入" if args.dry_run else "，已加入 train")
            )
            if not bg_files:
                print("  ! 背景图目录里没有图片，空托盘图建议拍 50~200 张（不同光照/角度/有无阴影）")

        if args.dry_run:
            print("\n--dry-run：未写入任何文件。")
            return 0

        # ---------------------------------------------------------- 写配置
        key_of = {source_split: yaml_key for source_split, yaml_key in SPLITS}
        produced = [stats.split for stats in all_stats]
        lines_out = [
            "# 由 prepare_dataset.py 自动生成：分割标注已转换为检测(bbox)标注",
            f"# 源数据集：{source}",
            "# 类别顺序与源数据集 data.yaml 完全一致，不要手工调整",
            "",
        ]
        for split in ("train", "valid", "test"):
            if split in produced:
                lines_out.append(f"{key_of[split]}: datasets/trash-det/{split}/images")
        lines_out.extend(["", f"nc: {class_count}", "", "names:"])
        lines_out.extend(f"  {index}: {names[index]}" for index in sorted(names))
        yaml_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")

        print(f"\n完成。检测数据集：{output}")
        print(f"       配置文件  ：{yaml_path}")
        print("\n下一步：")
        print(f"  python train.py --data {yaml_path.name} --model yolov8n.pt --epochs 100 --imgsz 640")
        return 0

    except PrepareError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
