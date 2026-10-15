#!/usr/bin/env python3
"""YOLO 垃圾识别模型训练脚本（ultralytics）。

用法（默认 20 轮，先快速验证整条流程能不能跑通）：
    python train.py --data data-det.yaml --model yolov8n.pt      # 20 轮，几分钟
    python train.py --data data-det.yaml --epochs 100            # 正式训练

怎么改训练轮数
--------------
训练轮数由命令行参数 --epochs 决定，**改这一个参数就行，不用改代码**：
    --epochs 20     快速试跑（当前默认）
    --epochs 100    常用的正式训练轮数
    --epochs 300    数据量小或效果不够时再加
脚本启动时会打印「快速验证模式」提示，提醒你当前不是正式训练。
另外 --fraction 0.3 表示每个 epoch 只用 30% 的训练图，配合小轮数做冒烟测试更快。

只做数据集体检、不训练：
    python train.py --dry-run

先确认 GPU / 内存能不能用（推荐第一次跑之前先执行）：
    python train.py --check-gpu

Windows 注意：dataloader 的每个 worker 都是独立进程，会各自重新加载一份
torch + CUDA（约 1.6 GB 提交内存），worker 开太多会报
"OSError: [WinError 1455] 页面文件太小"。所以本脚本在 Windows 上默认 --workers 0，
建议配合 --cache 使用。

训练脚本在做三件正事之前会先做三件"防坑"的事：
    1. 校验 data.yaml 的 nc / names 是否自洽、索引是否连续；
    2. 校验数据集目录、图片与标签是否配套，标签里的类别索引是否越界；
    3. 校验 data.yaml 的类别集合与 backend/config/classes.json 是否一致
       （两者对不上时，检测结果会串类或查不到槽位）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

# --------------------------------------------------------------------------- #
# 路径基准：所有默认路径都相对脚本所在目录，保证在仓库根目录或 training/ 下
# 执行 `python training/train.py` / `python train.py` 都能工作。
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_CLASSES_JSON = REPO_ROOT / "backend" / "config" / "classes.json"
DEFAULT_PROJECT = SCRIPT_DIR / "runs"

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif"})
DEFAULT_DATA = "data.yaml"
DEFAULT_MODEL = "yolov8n.pt"
# 默认轮数：先用小轮数确认整条流程能跑通（几分钟），正式训练用 --epochs 100 覆盖。
# 这是唯一需要动的地方，改命令行即可，不必改代码。
DEFAULT_EPOCHS = 20
DEFAULT_IMGSZ = 640
DEFAULT_NAME = "trash"
QUICK_TEST_EPOCHS = 30  # 不超过这个轮数时，启动会提示"这是快速验证，不是正式训练"

# Windows 上每个 dataloader worker 都是独立进程，会各自重新加载一份 torch + CUDA
# （约 1.5 GB 提交内存）。默认 8 个 worker 在 16 GB 内存的机器上极易触发
# "OSError: [WinError 1455] 页面文件太小" —— 所以 Windows 默认不启用多进程加载。
DEFAULT_WORKERS = 0 if sys.platform == "win32" else 8
WORKER_COMMIT_GB = 1.6  # 每个 worker 粗估的提交内存
BASE_COMMIT_GB = 2.5  # 主进程 torch + 数据集的粗估提交内存

PAGEFILE_HELP = (
    "这是 Windows 提交内存（物理内存 + 页面文件）不足导致的，不是代码问题。按有效性排序处理：\n"
    "  1) 降低 --workers：Windows 上每个 worker 都会再加载一份 torch+CUDA（约 1.6 GB）。\n"
    "     加 --cache 后建议直接 --workers 0（数据集小的话速度几乎不受影响）\n"
    "  2) 结束残留的 python 进程：上次失败的训练可能没退出，仍占着几个 GB\n"
    "     Get-Process python | Select Id, @{n='MB';e={[int]($_.PrivateMemorySize64/1MB)}}\n"
    "  3) 关掉浏览器 / 微信 / VS Code 等占内存的程序（任务管理器看『提交大小』）\n"
    "  4) 增大页面文件：系统属性 → 高级 → 性能『设置』→ 高级 → 虚拟内存『更改』\n"
    "     → 选 D 盘（剩余空间大）→ 自定义大小，初始 32768 MB / 最大 65536 MB → 重启\n"
    "  5) 用 --imgsz 减小输入尺寸、--batch 减小批大小，也能降低峰值内存"
)


def is_pagefile_error(exc: BaseException) -> bool:
    """判断是不是 Windows 提交内存不足（WinError 1455）。"""
    if getattr(exc, "winerror", None) == 1455:
        return True
    return "1455" in str(exc) or "paging file" in str(exc).lower()


class TrainingError(RuntimeError):
    """配置或数据集问题，用清晰的中文信息终止流程。"""


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "训练 YOLO 垃圾识别模型（7 类：paper/plastic/battery/medicine/peel/mask/tableware，"
            "映射到 4 个物理桶：可回收物/有害垃圾/厨余垃圾/其他垃圾）"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # --- 题目要求的 6 个参数 ---
    parser.add_argument("--data", default=DEFAULT_DATA, help="数据集配置文件（data.yaml）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="预训练权重或模型结构（如 yolov8n.pt / yolo11s.pt）")
    parser.add_argument(
        "--epochs",
        type=int,
        default=DEFAULT_EPOCHS,
        help=f"训练轮数（默认 {DEFAULT_EPOCHS}，快速验证用；正式训练建议 100~300）",
    )
    parser.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ, help="训练/验证输入尺寸")
    parser.add_argument("--project", default=str(DEFAULT_PROJECT), help="训练结果输出目录")
    parser.add_argument("--name", default=DEFAULT_NAME, help="本次实验名（决定 project/name 子目录）")

    # --- 常用可调项 ---
    parser.add_argument("--batch", type=int, default=16, help="批大小，-1 表示自动")
    parser.add_argument("--device", default=None, help="训练设备：cpu / 0 / 0,1，默认自动选择（先用 --check-gpu 确认 CUDA 可用）")
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="数据加载进程数。Windows 默认 0：每个 worker 会再加载一份 torch+CUDA，"
        "多开极易触发 WinError 1455（提交内存不足）",
    )
    parser.add_argument("--patience", type=int, default=50, help="早停耐心值（轮）")
    parser.add_argument("--seed", type=int, default=0, help="随机种子，保证可复现")
    parser.add_argument("--optimizer", default="auto", help="优化器：auto / SGD / AdamW ...")
    parser.add_argument("--lr0", type=float, default=None, help="初始学习率，默认由 ultralytics 决定")
    parser.add_argument("--resume", action="store_true", help="从上次中断处继续训练")
    parser.add_argument("--cache", action="store_true", help="把图片缓存到内存/磁盘加速训练")
    parser.add_argument(
        "--fraction",
        type=float,
        default=1.0,
        help="每个 epoch 只用该比例的训练图（0~1），冒烟测试用，例如 0.3",
    )
    parser.add_argument("--no-val", action="store_true", help="训练过程中不做验证")
    parser.add_argument("--no-plots", action="store_true", help="不生成训练曲线图")
    parser.add_argument("--exist-ok", action="store_true", help="允许写入已存在的 project/name 目录")

    # --- 本脚本附加的检查/工具项 ---
    parser.add_argument("--classes-json", default=str(DEFAULT_CLASSES_JSON), help="后端类别映射文件，用于一致性校验")
    parser.add_argument("--skip-dataset-check", action="store_true", help="跳过数据集完整性检查")
    parser.add_argument("--check-gpu", action="store_true", help="只检查 torch/CUDA/显卡状态并退出，不读数据集")
    parser.add_argument("--dry-run", action="store_true", help="只做检查并打印将执行的训练参数，不真正训练")
    parser.add_argument("--export", choices=["none", "onnx", "torchscript", "engine"], default="none",
                        help="训练结束后额外导出的格式（后端使用 .pt，一般不需要）")
    return parser


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def read_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML；PyYAML 缺失或格式错误时给出可执行的提示。"""
    try:
        import yaml  # 惰性导入：ultralytics 本身就依赖 PyYAML
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise TrainingError(f"需要 PyYAML：pip install pyyaml（{exc}）") from exc

    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except Exception as exc:
        raise TrainingError(f"解析 {path} 失败：{exc}") from exc

    if not isinstance(loaded, dict):
        raise TrainingError(f"{path} 的内容应该是一个映射（key: value）")
    return loaded


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except Exception as exc:
        raise TrainingError(f"解析 {path} 失败：{exc}") from exc
    if not isinstance(loaded, dict):
        raise TrainingError(f"{path} 的内容应该是一个 JSON 对象")
    return loaded


def resolve_path(value: str, label: str, must_exist: bool = True) -> Path:
    """把命令行里的路径解析成绝对路径：先按当前目录，再按脚本所在目录。"""
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        if candidate.exists() or not must_exist:
            return candidate.resolve()
        raise TrainingError(f"{label} 不存在：{candidate}")

    bases = [Path.cwd(), SCRIPT_DIR]
    for base in bases:
        path = (base / candidate)
        if path.exists() or not must_exist:
            return path.resolve()
    tried = "、".join(str(base / candidate) for base in bases)
    raise TrainingError(f"{label} 不存在：{value}（已尝试 {tried}）")


def normalize_names(raw: Any) -> dict[int, str]:
    """把 data.yaml 的 names 统一成 {索引: 名称}，并校验索引连续。"""
    if isinstance(raw, dict):
        names: dict[int, str] = {}
        for key, value in raw.items():
            try:
                index = int(key)
            except (TypeError, ValueError) as exc:
                raise TrainingError(f"names 的键必须是整数索引，收到：{key!r}") from exc
            names[index] = str(value)
    elif isinstance(raw, (list, tuple)):
        names = {index: str(value) for index, value in enumerate(raw)}
    else:
        raise TrainingError("data.yaml 缺少 names，或它的格式不是列表/映射")

    if not names:
        raise TrainingError("data.yaml 的 names 是空的")
    expected = list(range(len(names)))
    if sorted(names) != expected:
        raise TrainingError(f"names 的索引必须是 0..{len(names) - 1} 连续整数，当前是 {sorted(names)}")
    return dict(sorted(names.items()))


# --------------------------------------------------------------------------- #
# 数据集体检
# --------------------------------------------------------------------------- #
@dataclass
class SplitReport:
    """单个 split（train/val/test）的体检结果。"""

    split: str
    images_dir: Optional[Path] = None
    labels_dir: Optional[Path] = None
    missing_dir: bool = False
    missing_labels_dir: bool = False
    images: int = 0
    labelled: int = 0
    unlabelled: int = 0
    empty_labels: int = 0
    box_lines: int = 0  # 5 列 bbox 标注
    polygon_lines: int = 0  # 1+2N 列分割标注
    class_counts: dict[int, int] = field(default_factory=dict)
    bad_lines: int = 0
    bad_ids: int = 0
    bad_coords: int = 0
    samples: list[str] = field(default_factory=list)

    @property
    def label_format(self) -> str:
        if self.box_lines and self.polygon_lines:
            return "mixed"
        if self.polygon_lines:
            return "polygon"
        if self.box_lines:
            return "box"
        return "unknown"

    def describe(self) -> str:
        if self.missing_dir:
            return f"目录不存在：{self.images_dir}"
        parts = [f"{self.images} 张图", f"{self.labelled} 个标签"]
        format_text = {
            "box": "检测(bbox)标注",
            "polygon": "分割(多边形)标注",
            "mixed": "混合标注",
        }.get(self.label_format)
        if format_text:
            parts.append(format_text)
        if self.unlabelled:
            parts.append(f"{self.unlabelled} 张无标签")
        if self.empty_labels:
            parts.append(f"{self.empty_labels} 个空标签(纯背景)")
        if self.bad_lines:
            parts.append(f"{self.bad_lines} 行格式错误")
        if self.bad_ids:
            parts.append(f"{self.bad_ids} 行类别越界")
        if self.bad_coords:
            parts.append(f"{self.bad_coords} 行坐标越界")
        return "，".join(parts)


def labels_dir_for(images_dir: Path) -> Optional[Path]:
    """按 YOLO 约定把路径里的 images 段换成 labels 段（与 ultralytics 行为一致）。"""
    parts = list(images_dir.parts)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].lower() == "images":
            parts[index] = "labels"
            return Path(*parts)
    return None


def iter_images(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )


def inspect_split(
    split: str,
    images_dir: Path,
    labels_dir: Optional[Path],
    class_count: int,
    sample_limit: int = 5,
) -> SplitReport:
    """统计图片/标签数量、类别分布，并检查标注是否合法。"""
    report = SplitReport(split=split, images_dir=images_dir, labels_dir=labels_dir)
    if not images_dir.is_dir():
        report.missing_dir = True
        return report
    if labels_dir is None:
        report.missing_dir = True
        return report
    if not labels_dir.is_dir():
        report.missing_labels_dir = True

    images = iter_images(images_dir)
    report.images = len(images)

    for image in images:
        label_path = labels_dir / f"{image.stem}.txt"
        if not label_path.is_file():
            report.unlabelled += 1
            if len(report.samples) < sample_limit:
                report.samples.append(f"缺标签：{image.name}")
            continue
        report.labelled += 1
        try:
            text = label_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            report.bad_lines += 1
            if len(report.samples) < sample_limit:
                report.samples.append(f"读不到：{label_path.name}（{exc}）")
            continue

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            report.empty_labels += 1
            continue

        for line in lines:
            parts = line.split()
            try:
                class_id = int(float(parts[0]))
                coords = [float(value) for value in parts[1:]]
            except (ValueError, IndexError):
                report.bad_lines += 1
                if len(report.samples) < sample_limit:
                    report.samples.append(f"{label_path.name}: 非数字字段")
                continue

            # YOLO 检测标注是 5 列；分割标注是 class + 2N 个多边形顶点
            is_box = len(coords) == 4
            is_polygon = len(coords) >= 6 and len(coords) % 2 == 0
            if not (is_box or is_polygon):
                report.bad_lines += 1
                if len(report.samples) < sample_limit:
                    report.samples.append(
                        f"{label_path.name}: {len(parts)} 列（应为 5 列 bbox 或 1+2N 列多边形）"
                    )
                continue
            if is_polygon:
                report.polygon_lines += 1
            else:
                report.box_lines += 1

            if class_id < 0 or class_id >= class_count:
                report.bad_ids += 1
                if len(report.samples) < sample_limit:
                    report.samples.append(f"{label_path.name}: 类别 {class_id} 超出 0..{class_count - 1}")
                continue
            if any(value < -0.001 or value > 1.001 for value in coords):
                report.bad_coords += 1
                if len(report.samples) < sample_limit:
                    report.samples.append(f"{label_path.name}: 坐标应归一化到 0~1")
            report.class_counts[class_id] = report.class_counts.get(class_id, 0) + 1

    return report


def resolve_split_root(data: dict[str, Any], yaml_path: Path) -> Path:
    """按 ultralytics 的规则确定数据集根目录。"""
    raw_path = data.get("path")
    if not raw_path:
        # 没有 path 时，ultralytics 以 data.yaml 所在目录为根
        return yaml_path.parent
    root = Path(str(raw_path)).expanduser()
    if not root.is_absolute():
        print(
            f"  ! data.yaml 里的 path 是相对路径（{root}）。ultralytics 会把它当成\n"
            f"    它自己 settings 里 datasets_dir 的相对路径，而不是 data.yaml 的相对路径。\n"
            f"    建议改成绝对路径，或删掉 path 让 train/val 相对 data.yaml 解析。"
        )
    return root


def check_dataset(
    data: dict[str, Any],
    yaml_path: Path,
    names: dict[int, str],
    strict: bool = True,
    model_name: str = "",
) -> list[str]:
    """逐 split 体检，返回问题清单；strict=True 时发现问题直接抛错。"""
    root = resolve_split_root(data, yaml_path)
    class_count = len(names)
    reports: dict[str, SplitReport] = {}

    print(f"\n数据集检查（根目录：{root}）")
    for split in ("train", "val", "test", "minival"):
        value = data.get(split)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if not value:
            continue
        candidate = Path(str(value)).expanduser()
        images_dir = candidate if candidate.is_absolute() else (root / candidate)
        report = inspect_split(split, images_dir, labels_dir_for(images_dir), class_count)
        reports[split] = report
        flag = "  " if not report.missing_dir else "!!"
        print(f"  {flag} {split:<6} {report.describe()}")
        for sample in report.samples:
            print(f"       - {sample}")
        if report.class_counts:
            detail = "，".join(
                f"{names.get(index, index)}={count}" for index, count in sorted(report.class_counts.items())
            )
            print(f"       {split} 标注实例：{detail}")

    problems: list[str] = []
    train = reports.get("train")
    if train is None:
        problems.append("data.yaml 没有配置 train 划分")
    elif train.missing_dir:
        problems.append(f"训练图片目录不存在：{train.images_dir}")
    elif train.images == 0:
        problems.append(f"训练图片目录里没有图片：{train.images_dir}")

    val = reports.get("val")
    if val is None:
        problems.append("data.yaml 没有配置 val 划分")
    elif val.missing_dir:
        problems.append(f"验证图片目录不存在：{val.images_dir}")
    elif val.images == 0:
        problems.append(f"验证图片目录里没有图片：{val.images_dir}")

    for split, report in reports.items():
        if report.missing_labels_dir:
            problems.append(f"{split} 的标签目录不存在：{report.labels_dir}")
        if report.bad_ids:
            problems.append(f"{split} 有 {report.bad_ids} 行类别索引越界（标注和 data.yaml 的 names 顺序对不上？）")
        if report.bad_lines:
            problems.append(f"{split} 有 {report.bad_lines} 行标注格式错误")
        if report.bad_coords:
            problems.append(f"{split} 有 {report.bad_coords} 行坐标未归一化到 0~1")

    if problems:
        print("\n发现以下问题：")
        for item in problems:
            print(f"  - {item}")
        if strict:
            raise TrainingError("数据集检查未通过，请先修复（或用 --skip-dataset-check 跳过）")
    else:
        print("  数据集检查通过")

    train_report = reports.get("train")
    class_counts = train_report.class_counts if train_report else {}
    missing_classes = [index for index in names if index not in class_counts]
    if missing_classes:
        print(
            "  ! 训练集里没有出现这些类别："
            + "，".join(f"{index}({names[index]})" for index in missing_classes)
            + "，模型将学不到它们"
        )

    # 标注格式 vs 模型类型：分割标注不能拿来训检测模型
    polygon_splits = [
        split for split, report in reports.items() if report.label_format in ("polygon", "mixed")
    ]
    box_splits = [split for split, report in reports.items() if report.label_format == "box"]
    is_seg_model = "-seg" in str(model_name).lower()

    if polygon_splits and not is_seg_model:
        problems.append(
            f"标注是分割(多边形)格式（{'、'.join(polygon_splits)}），但 --model {model_name or '?'} 是检测模型，"
            "检测训练只认 5 列 bbox 标注。两种做法："
            "① 直接训练分割模型：--model yolov8n-seg.pt；"
            "② 先转换：python prepare_dataset.py，再用 --data data-det.yaml 训练检测模型（CPU 上更快）"
        )
    elif polygon_splits and is_seg_model:
        print(f"  分割标注 + 分割模型（{model_name}）匹配，可以直接训练")
    if box_splits and is_seg_model:
        print(
            f"  ! 标注是 bbox 格式，但用的是分割模型 {model_name}："
            "ultralytics 会用矩形掩码训练，效果不如多边形标注"
        )
    return problems


# --------------------------------------------------------------------------- #
# 类别一致性检查
# --------------------------------------------------------------------------- #
def check_class_mapping(names: dict[int, str], classes_json: Path) -> list[str]:
    """比对 data.yaml 与 backend/config/classes.json 的类别集合与槽位。

    返回**阻断性问题**列表（空表示没问题）。
    数据集里的类别必须全部能在 classes.json 里查到：查不到的类别在推理时
    会统统落到默认槽位（倒错桶），所以这里当成致命错误而不是告警——
    典型触发场景就是拿旧体系（6 类）的数据集配新体系（7 类）的 classes.json。
    """
    if not classes_json.is_file():
        print(f"\n类别一致性：跳过（未找到 {classes_json}）")
        return []

    problems: list[str] = []
    config = read_json(classes_json)
    table = config.get("classes") if isinstance(config.get("classes"), dict) else {}
    default = config.get("default") if isinstance(config.get("default"), dict) else {}

    print(f"\n类别映射（{classes_json.name}）")
    for index, name in names.items():
        entry = table.get(name) if isinstance(table.get(name), dict) else None
        if entry is None:
            print(f"  {index}  {name:<12} -> ! classes.json 里没有这个类别，将走默认槽位 {default.get('slot', '-')}")
        else:
            print(f"  {index}  {name:<12} -> slot {entry.get('slot', '-')}  {entry.get('label_zh', '')}")

    # 物理桶视角：确认识别类别是怎么合并到 4 个槽位的
    groups = config.get("groups") if isinstance(config.get("groups"), dict) else {}
    if groups:
        members: dict[str, list[str]] = {}
        for name in names.values():
            entry = table.get(name)
            if isinstance(entry, dict):
                members.setdefault(str(entry.get("slot")), []).append(name)
        print("\n物理槽位分组")
        for key in sorted(groups, key=lambda item: int(item) if str(item).isdigit() else 0):
            group = groups[key] if isinstance(groups[key], dict) else {}
            slot = group.get("slot", key)
            owner = "、".join(members.get(str(slot), [])) or "（没有识别类别指向它）"
            print(f"  slot {slot}  {str(group.get('label_zh', '')):<8} <- {owner}")

    yaml_names = set(names.values())
    config_names = set(table) if table else set()
    only_yaml = sorted(yaml_names - config_names)
    only_config = sorted(config_names - yaml_names)
    if only_yaml:
        print("  ! 数据集的类别在 classes.json 里查不到：")
        print(f"      {'、'.join(only_yaml)}")
        problems.append(
            f"data.yaml 里的这些类别在 classes.json 中不存在：{'、'.join(only_yaml)}"
            "（推理时会全部落到默认槽位，等于倒错桶）——"
            "请改用与 classes.json 匹配的数据集，或同步更新 backend/config/classes.json"
        )
    if only_config:
        print("  ! classes.json 里有、但本数据集没有的类别（不算错，只是它们学不到）：")
        print(f"      {'、'.join(only_config)}")
    if not only_yaml:
        print("  类别集合一致（顺序不影响映射，映射按名字查表）")
    return problems


# --------------------------------------------------------------------------- #
# 训练
# --------------------------------------------------------------------------- #
def import_yolo() -> Any:
    try:
        from ultralytics import YOLO
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise TrainingError(
            f"需要 ultralytics：pip install -r backend/requirements.txt（{exc}）"
        ) from exc
    return YOLO


def windows_commit_status() -> Optional[dict[str, float]]:
    """Windows：读取提交内存（物理内存 + 页面文件）的上限与已用量，单位 GB。

    CUDA 版 torch 在导入和加载 CUDA DLL 时需要大量提交内存，这是
    "WinError 1455 页面文件太小" 的判断依据。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PerformanceInformation(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("CommitTotal", ctypes.c_size_t),
                ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t),
                ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t),
                ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t),
                ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t),
                ("PageSize", ctypes.c_size_t),
                ("HandleCount", wintypes.DWORD),
                ("ProcessCount", wintypes.DWORD),
                ("ThreadCount", wintypes.DWORD),
            ]

        info = PerformanceInformation()
        info.cb = ctypes.sizeof(info)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            return None
        page = float(info.PageSize or 4096)
        gb = 1024.0**3
        return {
            "limit_gb": info.CommitLimit * page / gb,
            "used_gb": info.CommitTotal * page / gb,
            "free_gb": (info.CommitLimit - info.CommitTotal) * page / gb,
            "physical_total_gb": info.PhysicalTotal * page / gb,
            "physical_free_gb": info.PhysicalAvailable * page / gb,
        }
    except Exception:  # pragma: no cover - 非 Windows 或 API 不可用
        return None


def candidate_interpreters() -> list[Path]:
    """列出机器上其它可能的 Python 解释器（用于"你 python 指错环境了"的场景）。"""
    candidates: list[Path] = []
    if sys.platform == "win32":
        try:  # py 启动器登记的官方 Python
            result = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=15)
            for line in result.stdout.splitlines():
                match = re.search(r"([A-Za-z]:\\[^\s].*?python\.exe)", line)
                if match:
                    candidates.append(Path(match.group(1)))
        except Exception:  # pragma: no cover - 没有 py 启动器
            pass
        for base in (
            Path("D:/miniconda"),
            Path("C:/ProgramData/miniconda3"),
            Path.home() / "miniconda3",
            Path.home() / "anaconda3",
        ):
            envs = base / "envs"
            if envs.is_dir():
                candidates.extend(env / "python.exe" for env in envs.iterdir())
            candidates.append(base / "python.exe")
    else:
        candidates.extend([Path("/usr/bin/python3"), Path("/usr/local/bin/python3")])

    unique: list[Path] = []
    seen = set()
    try:
        seen.add(str(Path(sys.executable).resolve()).lower())
    except OSError:
        pass
    for path in candidates:
        try:
            if not path.is_file():
                continue
            key = str(path.resolve()).lower()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def probe_torch(interpreter: Path) -> Optional[str]:
    """用子进程问这个解释器里的 torch 情况，返回 "版本 cuda版本 是否可用" 或 None。"""
    code = "import torch;print(torch.__version__, torch.version.cuda or 'cpu', torch.cuda.is_available())"
    try:
        result = subprocess.run([str(interpreter), "-c", code], capture_output=True, text=True, timeout=90)
    except Exception:
        return None
    output = (result.stdout or "").strip()
    if result.returncode != 0 or not output:
        return None
    return output


def find_interpreters_with_torch(require_cuda: bool = False, limit: int = 3) -> list[tuple[Path, str]]:
    """找出机器上已经装好（可用 CUDA 的）torch 的其它解释器。"""
    found: list[tuple[Path, str]] = []
    for interpreter in candidate_interpreters():
        info = probe_torch(interpreter)
        if not info:
            continue
        parts = info.split()
        if require_cuda and not (len(parts) >= 3 and parts[2].lower() == "true"):
            continue
        found.append((interpreter, info))
        if len(found) >= limit:
            break
    return found


def suggest_other_interpreters(require_cuda: bool) -> None:
    """提示"换个解释器就能用"——本机上往往已经装好了，只是 python 指错了地方。"""
    others = find_interpreters_with_torch(require_cuda=require_cuda)
    if not others:
        return
    print("  这台机器上这些解释器已经可以直接用：")
    for interpreter, info in others:
        print(f"      {interpreter}")
        print(f"        -> torch {info}")
    print("  用其中一个重跑（PowerShell 里带上完整路径即可，不用管当前是哪个环境）：")
    print(f'      & "{others[0][0]}" {Path(__file__).name} --check-gpu')


def report_device(requested: Optional[str], workers: int = 0) -> dict[str, Any]:
    """检查 torch / CUDA / 显卡状态并打印，返回结论字典。

    requested 来自 --device：None 或 auto 表示交给 ultralytics 自动选择，
    cpu 表示强制 CPU，其它值（0 / 0,1 / cuda:0）表示要求用显卡。
    """
    report: dict[str, Any] = {"available": False, "target": "cpu", "free_gb": 0.0}

    print("\n设备检查")
    print(f"  python     : {sys.executable}（{sys.version.split()[0]}）")
    try:
        import torch
    except OSError as exc:
        if is_pagefile_error(exc):
            raise TrainingError(
                f"导入 torch 失败：{exc}\n\n{PAGEFILE_HELP}\n"
                f"  验证：python {Path(__file__).name} --check-gpu"
            ) from exc
        raise TrainingError(f"导入 torch 失败：{exc}") from exc
    except ImportError:
        print("  torch      : 未安装")
        print("  常见原因   : 当前 python 指向了别的环境（例如 conda base），而不是装了 torch 的那个")
        suggest_other_interpreters(require_cuda=False)
        if requested and str(requested).strip().lower() not in ("", "auto", "cpu"):
            raise TrainingError(f"--device {requested} 要求用 GPU，但没有安装 torch") from None
        return report

    cuda_build = getattr(torch.version, "cuda", None)
    available = bool(torch.cuda.is_available())
    report["available"] = available
    report["torch"] = torch.__version__
    print(f"  torch      : {torch.__version__}（CUDA 编译版本：{cuda_build or '无，这是 CPU 版'}）")
    print(f"  CUDA 可用  : {'是' if available else '否'}")

    requested_text = str(requested or "").strip().lower()
    wants_gpu = requested_text not in ("", "auto", "cpu")

    if available:
        for index in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(index)
            free, total = torch.cuda.mem_get_info(index)
            report["gpu_name"] = props.name
            if index == 0:
                report["free_gb"] = free / 1024**3
            print(
                f"  显卡 [{index}]  : {props.name}  显存 {total / 1024**3:.1f} GB"
                f"（空闲 {free / 1024**3:.1f} GB）  算力 {props.major}.{props.minor}"
            )
        free_gb = report["free_gb"]
        if free_gb < 3:
            hint = "batch 8 / imgsz 512"
        elif free_gb < 5:
            hint = "batch 16 / imgsz 640"
        elif free_gb < 7:
            hint = "batch 32 / imgsz 640"
        else:
            hint = "batch 64 / imgsz 640"
        print(f"  显存建议   : 当前空闲 {free_gb:.1f} GB，yolov8n 可先用 {hint}；显存不够就降 batch 或 imgsz")
        print("  注意       : 桌面程序（浏览器/微信等）会占显存，训练前关掉能省下 1~2 GB")
    else:
        print("  原因       : 装的是 CPU 版 torch，或显卡驱动与 CUDA 版本不匹配")
        print("  修复（Windows + NVIDIA，cu126 对应驱动支持 CUDA 12.6；老驱动换 cu124/cu121）：")
        print("    python -m pip uninstall -y torch torchvision")
        print("    python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126")
        print("  验证：python train.py --check-gpu")
        print("  若报『THESE PACKAGES DO NOT MATCH THE HASHES』：pytorch 的轮子有 2.6 GB，")
        print("    连接中途断流会让文件损坏（pip 会用索引页上的 #sha256 校验并报错）。")
        print("    改成手动下载 + 本地安装（本地文件不经索引哈希校验）：")
        print("      curl.exe -L -C - -o torch.whl <索引页里该 .whl 的完整链接>   # -C - 支持断点续传")
        print("      Get-FileHash torch.whl -Algorithm SHA256                     # 与索引页 #sha256= 对比")
        print("      python -m pip install <本地 torch.whl> <本地 torchvision.whl>  # 文件名必须原样保留")
        suggest_other_interpreters(require_cuda=True)

    if wants_gpu and not available:
        raise TrainingError(
            f"--device {requested} 要求用 GPU，但 torch.cuda.is_available() 为 False。"
            "请先按上面的命令换成 CUDA 版 torch，或用 --device cpu 显式在 CPU 上训练。"
        )

    # Windows 提交内存检查：这是 "WinError 1455 页面文件太小" 的根因所在
    commit = windows_commit_status()
    if commit:
        print(
            f"  物理内存   : {commit['physical_total_gb']:.1f} GB"
            f"（空闲 {commit['physical_free_gb']:.1f} GB）"
        )
        print(
            f"  提交内存   : 已用 {commit['used_gb']:.1f} / 上限 {commit['limit_gb']:.1f} GB"
            f"（可用 {commit['free_gb']:.1f} GB）"
        )
        need = BASE_COMMIT_GB + max(workers, 0) * WORKER_COMMIT_GB
        if commit["free_gb"] < need:
            print(
                f"  ! 内存风险 : --workers {workers} 预计需要约 {need:.1f} GB 提交内存，"
                f"而当前只剩 {commit['free_gb']:.1f} GB"
            )
            print("    每个 dataloader worker 都会再加载一份 torch+CUDA，这正是")
            print("    『OSError: [WinError 1455] 页面文件太小』的典型原因。处理办法：")
            print("      - 降低 --workers（配 --cache 时可直接用 --workers 0，速度影响很小）")
            print("      - 关掉浏览器 / 微信 / VS Code 等占内存的程序")
            print("      - 结束残留的 python 进程（上次失败的训练可能还占着几个 GB）")
            print("      - 增大页面文件（装在剩余空间大的盘，设 32~64 GB 后重启）")
            report["workers_recommended"] = 0
        if commit["free_gb"] < BASE_COMMIT_GB:
            raise TrainingError(
                f"可用提交内存只剩 {commit['free_gb']:.1f} GB，连主进程加载 torch+CUDA 都不够。\n\n"
                f"{PAGEFILE_HELP}"
            )

    # 环境版本提醒（不阻断，只提示）
    try:
        major, minor = (int(part) for part in str(report.get("torch", "")).split("+")[0].split(".")[:2])
        if (major, minor) < (1, 13):
            print(f"  ! torch {report['torch']} 偏旧，ultralytics 新版可能用到较新的 API，建议升到 2.x")
    except (TypeError, ValueError):
        pass
    if sys.version_info < (3, 9):
        print(f"  ! Python {sys.version.split()[0]} 偏旧，建议 3.10+")

    if requested_text == "cpu":
        report["target"] = "cpu（按 --device cpu 强制）"
    elif wants_gpu:
        report["target"] = f"cuda:{requested}"
    elif available:
        report["target"] = "cuda:0（自动，未指定 --device）"
    else:
        report["target"] = "cpu（自动，未检测到可用 CUDA）"
    print(f"  本次使用   : {report['target']}")
    return report


def train(args: argparse.Namespace, data_yaml: Path, project_dir: Path) -> Path:
    """执行训练，返回 best.pt 路径。"""
    YOLO = import_yolo()

    print(f"\n加载模型：{args.model}")
    try:
        model = YOLO(args.model)
    except Exception as exc:
        raise TrainingError(
            f"加载 {args.model} 失败：{exc}\n"
            f"提示：{args.model} 若不在本地，ultralytics 会联网下载，请确认网络可用；"
            f"也可以换成已有的 .pt 路径。"
        ) from exc

    train_kwargs: dict[str, Any] = {
        "data": str(data_yaml),
        "epochs": args.epochs,
        "imgsz": args.imgsz,
        "batch": args.batch,
        "project": str(project_dir),
        "name": args.name,
        "exist_ok": args.exist_ok,
        "patience": args.patience,
        "seed": args.seed,
        "workers": args.workers,
        "optimizer": args.optimizer,
        "cache": args.cache,
        "val": not args.no_val,
        "plots": not args.no_plots,
        "resume": args.resume,
        "deterministic": True,
    }
    if args.device:
        train_kwargs["device"] = args.device
    if args.lr0 is not None:
        train_kwargs["lr0"] = args.lr0
    if 0 < args.fraction < 1:
        train_kwargs["fraction"] = float(args.fraction)

    if args.workers == 0 and not args.cache:
        print("提示：--workers 0 时建议同时加 --cache，把数据集缓存进内存（本项目数据集仅几十 MB）")
    print(
        f"开始训练：epochs={args.epochs} imgsz={args.imgsz} batch={args.batch} "
        f"device={args.device or 'auto'} workers={args.workers}"
    )
    try:
        model.train(**train_kwargs)
    except OSError as exc:
        if is_pagefile_error(exc):
            raise TrainingError(f"训练中断：{exc}\n\n{PAGEFILE_HELP}") from exc
        raise
    except KeyboardInterrupt:  # 让用户能安全中断，权重仍可能已保存
        print("\n已中断训练。部分权重可能已保存在输出目录中，可用 --resume 继续。")
        raise

    save_dir = Path(getattr(getattr(model, "trainer", None), "save_dir", project_dir / args.name))
    return save_dir / "weights" / "best.pt"


def export_model(best: Path, fmt: str) -> None:
    if fmt == "none":
        return
    if not best.is_file():
        print(f"跳过导出：找不到 {best}")
        return
    YOLO = import_yolo()
    print(f"\n导出 {fmt}：{best}")
    try:
        YOLO(str(best)).export(format=fmt)
    except Exception as exc:
        print(f"导出失败（不影响 .pt 权重）：{exc}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.check_gpu:
            report_device(args.device, args.workers)
            return 0

        data_yaml = resolve_path(args.data, "数据集配置 --data")
        project_dir = resolve_path(args.project, "输出目录 --project", must_exist=False)
        classes_json = resolve_path(args.classes_json, "类别映射 --classes-json", must_exist=False)

        data = read_yaml(data_yaml)
        names = normalize_names(data.get("names"))
        nc = data.get("nc")
        if nc is not None and int(nc) != len(names):
            raise TrainingError(f"data.yaml 里 nc={nc} 与 names 的类别数 {len(names)} 不一致")

        # --model 只写了文件名时，优先用 training/ 下的同名权重，
        # 避免在别的目录执行时又联网下载一份到当前目录
        if not Path(args.model).is_absolute():
            local_model = SCRIPT_DIR / Path(args.model).name
            if local_model.is_file():
                print(f"使用本地权重：{local_model}")
                args.model = str(local_model)

        print("=" * 66)
        print("YOLO 垃圾识别模型训练")
        print("=" * 66)
        print(f"  数据集配置 : {data_yaml}")
        print(f"  模型       : {args.model}")
        print(f"  轮数/尺寸  : {args.epochs} epochs / imgsz {args.imgsz}")
        print(f"  输出目录   : {project_dir / args.name}")
        print(f"  类别数     : {len(names)} -> {', '.join(names[index] for index in sorted(names))}")
        if args.epochs <= QUICK_TEST_EPOCHS:
            print(
                f"  ! 快速验证模式：本次只训练 {args.epochs} 轮，精度会明显偏低，"
                f"用来确认流程能跑通；正式训练加 --epochs 100（或 200/300）"
            )
        if 0 < args.fraction < 1:
            print(f"  ! 只使用 {args.fraction:.0%} 的训练数据（--fraction）")

        device_report = report_device(args.device, args.workers)

        # 类别映射不一致（例如拿旧体系数据集配新体系 classes.json）属于致命问题
        mapping_problems = check_class_mapping(names, classes_json)

        if args.skip_dataset_check:
            print("\n数据集检查：已按 --skip-dataset-check 跳过")
            problems: list[str] = list(mapping_problems)
        else:
            # dry-run 只报告不中断，便于一次性看到所有问题
            problems = check_dataset(data, data_yaml, names, strict=not args.dry_run, model_name=args.model)
            problems = list(mapping_problems) + list(problems)

        if args.dry_run:
            print("\n--dry-run：检查完成，未开始训练。")
            if problems:
                print(f"（共 {len(problems)} 项待修复）")
            print("正式训练命令：")
            print(
                f"  python {Path(__file__).name} --data {args.data} --model {args.model}"
                f" --epochs {args.epochs} --imgsz {args.imgsz} --project {args.project} --name {args.name}"
            )
            return 1 if problems else 0

        project_dir.mkdir(parents=True, exist_ok=True)
        best = train(args, data_yaml, project_dir)
        export_model(best, args.export)

        print("\n" + "=" * 66)
        print("训练结束")
        print("=" * 66)
        if best.is_file():
            size_mb = best.stat().st_size / (1024 * 1024)
            print(f"  最佳权重 : {best}  ({size_mb:.1f} MB)")
            print(f"  训练设备 : {device_report.get('target')}")
            print("\n下一步：")
            print(f"  1) 把权重放到后端模型目录：")
            print(f"       copy \"{best}\" \"{REPO_ROOT / 'backend' / 'models'}\\trash.pt\"")
            print(f"  2) 启动后端，在「模型」面板点「刷新」并加载 trash.pt")
        else:
            print(f"  未找到 best.pt，请检查输出目录：{project_dir / args.name}")
        return 0

    except TrainingError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
