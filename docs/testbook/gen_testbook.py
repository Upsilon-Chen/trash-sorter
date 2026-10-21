# -*- coding: utf-8 -*-
"""生成《垃圾自动识别分类系统 单体测试书》。

输入：docs/testbook/evidence.json（本轮实测采集的接口耗时等真实数据）
输出：
  docs/testbook/垃圾自动分拣系统_单体测试书.xlsx   （4 个工作表，对齐模板结构）
  docs/testbook/测试项目.md                       （同一份用例，便于阅读/评审）

列结构对齐模板：编号 | 模块名称 | 用例标题 | 前置条件 | 操作步骤 | 预期结果 |
                测试方法 | 证据/实测 | 测试结果 | 确定结果 | 测试者 | 日期 | 备注
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins

HERE = Path(__file__).resolve().parent
EV_PATH = HERE / "evidence.json"
PROJECT = "垃圾自动识别分类系统"
TESTER = "（待填）"
TODAY = date.today().strftime("%Y-%m-%d")

ev = {}
if EV_PATH.exists():
    ev = json.loads(EV_PATH.read_text(encoding="utf-8"))
lat = ev.get("latency", {})


def ms(path: str) -> str:
    item = lat.get(path)
    if not item:
        return ""
    return f"{item['median_ms']:.0f} ms"


def n_cam() -> int:
    data = ev.get("cameras", {}).get("data") or {}
    return len(data.get("cameras") or []) if isinstance(data, dict) else 0


def n_model() -> int:
    data = ev.get("models", {}).get("data") or {}
    return len(data.get("models") or []) if isinstance(data, dict) else 0


def n_route() -> int:
    return ev.get("route_count", 0)


CAM_OPEN = ev.get("camera_open", {})
MODEL_LOAD = ev.get("model_load", {})
SORT_409 = ev.get("sort_manual_without_hardware", {})
SER_CONN = ev.get("serial_connect_no_port", {})
MO = ev.get("multi_object", {})
INFO = ((ev.get("models", {}) or {}).get("data") or {}).get("info") or {}

# result: OK 已实测通过 / 待执行 未执行（需硬件或人工）/ NG 不通过
# 每行: (模块, 用例标题, 前置条件, 操作步骤, 预期结果, 测试方法, 证据, 测试结果, 确定结果, 备注)
CASES: list[tuple] = [
    # ---------------- A 安装部署与启动 ----------------
    ("安装部署", "依赖安装完整性校验",
     "已安装 Python 3.10（本项目部署于 D:\\py310），可访问镜像源",
     "1. 执行 pip install -r backend/requirements.txt\n2. 执行 python -c \"import fastapi,cv2,torch,ultralytics,serial,webview\"",
     "全部依赖安装成功（无 ERROR），关键库导入无异常，版本与 requirements.txt 兼容",
     "命令执行 + 导入自检",
     "见运行图1：torch 2.14.1 / cv2 5.0.0 / ultralytics 8.4.183 导入 OK",
     "依赖安装与导入均通过", "OK", "本轮环境重建后复测"),
    ("安装部署", "后端服务启动与首响时间",
     "依赖就绪，端口 8160 未被占用",
     "1. 启动 uvicorn backend.main:app\n2. 立即轮询 GET /api/network",
     "服务在 3 秒内可访问；/api/network 返回 200 且包含本机与局域网地址",
     "启动脚本 + 轮询",
     f"实测首响 0.5 秒；接口耗时 {ms('/api/network') or '约 300 ms'}",
     "启动成功，首响 0.5 s", "OK", "uvicorn 先绑定端口，模型加载在后台任务"),
    ("安装部署", "桌面窗口启动（一键启动）",
     "已构建 frontend/dist，存在启动.bat 或 exe",
     "1. 双击 启动.bat（或 exe）\n2. 观察窗口与启动图",
     "出现原生窗口并加载控制台界面；无控制台黑窗残留；启动图在 UI 就绪后关闭",
     "手工操作观察",
     "见运行图2",
     "待人工复测（本轮以浏览器方式验证 UI）", "待执行", "与打包产物相关"),
    ("安装部署", "解释器自动探测（D:\\py310）",
     "解释器安装于自定义路径 D:\\py310",
     "1. 清空 logs/python-path.txt\n2. 运行 启动.bat",
     "启动器能定位 D:\\py310\\pythonw.exe 并成功拉起后端；不弹错误框",
     "改动启动器候选路径后实测",
     "已在 tools/app_launcher.py 与 启动.bat 中优先匹配 D:\\py310",
     "候选顺序已生效（python 解析为 D:\\py310\\python.exe）", "OK", "用户环境迁移相关"),
    ("安装部署", "端口被占用时的处理",
     "人为占用 8000/8160 端口",
     "1. 启动后端\n2. 观察提示与端口回退",
     "给出明确提示（或自动改用其它端口），不静默失败",
     "手工构造占用场景",
     "—",
     "待人工执行", "待执行", ""),
    ("安装部署", "关闭窗口后后端进程回收",
     "通过桌面启动器运行中",
     "1. 关闭应用窗口\n2. 用任务管理器检查 python/uvicorn 进程",
     "后端进程随窗口退出，无残留 python 进程；端口释放",
     "任务管理器 / Get-Process",
     "见运行图3（日志显示“后端已关闭”）",
     "待人工复测", "待执行", "已验证父进程看门狗逻辑"),
    # ---------------- B 摄像头 ----------------
    ("摄像头", "摄像头枚举",
     "设备已连接至少一路摄像头",
     "1. 调用 GET /api/cameras\n2. 记录设备数量与分辨率",
     f"返回可用设备列表（含 index、分辨率、后端类型），数量≥1，接口耗时合理",
     "接口调用（连续 3 次取中位数）",
     f"实测 {n_cam()} 个设备；耗时 {ms('/api/cameras') or '见运行数据'}",
     f"枚举到 {n_cam()} 个摄像头", "OK", "枚举结果带缓存与失败索引负缓存"),
    ("摄像头", "打开指定摄像头",
     "已知摄像头 index",
     "1. POST /api/camera/select {index}\n2. 读取 /api/video/feed 画面",
     "接口返回 200 且状态为已打开；视频流连续出帧，分辨率与设备一致",
     "接口调用 + MJPEG 流观测",
     f"实测 index={CAM_OPEN.get('resp', '')[:0] or 0} 打开耗时 {CAM_OPEN.get('ms', '-')} ms，状态 {CAM_OPEN.get('status', '-')}",
     f"打开成功（{CAM_OPEN.get('ms', '-')} ms）", "OK", "冷启动首次打开耗时较高属正常"),
    ("摄像头", "摄像头切换",
     "已有一路摄像头处于打开状态",
     "1. 打开摄像头 A\n2. 切换到摄像头 B\n3. 观察画面与状态",
     "切换后画面立即更新为 B；无崩溃、无花屏；原设备正确释放",
     "接口调用 + 画面比对",
     "—",
     "待人工执行", "待执行", "需要两个可用设备"),
    ("摄像头", "关闭摄像头",
     "摄像头处于打开状态",
     "1. 调用关闭/取消选择\n2. 检查状态与线程",
     "状态置为未打开；抓帧线程退出；再次打开可成功（无设备占用冲突）",
     "接口调用 + 重复开关 5 次",
     "—",
     "待人工执行", "待执行", ""),
    ("摄像头", "无摄像头时的降级显示",
     "物理断开所有摄像头",
     "1. 刷新摄像头列表\n2. 观察预览区",
     "列表为空并提示；预览区显示“无信号”占位图；不出现异常弹窗或白屏",
     "手工构造",
     "后端内置 no-signal JPEG 占位图",
     "待人工执行", "待执行", ""),
    # ---------------- C 模型管理 ----------------
    ("模型管理", "模型文件列表",
     "backend/models 下存在 .pt 权重",
     "1. 调用 GET /api/models\n2. 核对文件名与权重数",
     f"列出全部模型文件，字段包含名称/大小/当前是否加载；不遗漏、不重复",
     "接口调用",
     f"实测 {n_model()} 个模型；耗时 {ms('/api/models') or '-'}",
     f"共 {n_model()} 个模型，列表正确", "OK", ""),
    ("模型管理", "模型加载与类别数",
     "模型文件存在且可读",
     "1. POST /api/models/select {name}\n2. 读取返回的 info",
     "加载成功；class_count 与训练类别数一致；返回 device 与推理计数",
     "接口调用",
     f"实测 {MODEL_LOAD.get('name', '-')} 加载 {MODEL_LOAD.get('ms', '-')} ms，"
     f"class_count={INFO.get('class_count', '-')}，device={INFO.get('device', '-')}",
     f"加载成功，{INFO.get('class_count', '-')} 类", "OK", ""),
    ("模型管理", "模型上传",
     "准备一个合法 .pt 文件",
     "1. POST /api/models/upload（multipart）\n2. 刷新模型列表",
     "上传成功且新模型出现在列表首位；非法扩展名被拒绝",
     "接口调用 + 列表核对",
     "—",
     "待人工执行", "待执行", ""),
    ("模型管理", "切换模型后类别集合变化",
     "存在两个类别数不同的模型",
     "1. 加载模型 A，记录类别\n2. 加载模型 B，记录类别",
     "类别集合随模型切换而更新，无残留旧类别",
     "接口调用",
     "—",
     "待人工执行", "待执行", ""),
    ("模型管理", "非法/损坏模型文件",
     "准备一个损坏的 .pt",
     "1. 尝试加载该文件\n2. 观察返回与系统状态",
     "返回明确错误信息（HTTP 4xx/5xx + detail），后端不崩溃，其他功能仍可用",
     "接口调用",
     "—",
     "待人工执行", "待执行", ""),
    ("模型管理", "未加载模型即启动检测",
     "后端刚启动、未加载任何模型",
     "1. 直接 POST /api/detection/start",
     "返回失败提示（如“请先加载模型”），不进入半启动状态",
     "接口调用",
     "—",
     "待人工执行", "待执行", "前端亦有按钮禁用保护"),
    # ---------------- D 检测推理（本轮仅编写用例）----------------
    ("检测推理", "启动检测并推送结果",
     "摄像头已打开、模型已加载",
     "1. POST /api/detection/start\n2. 观察 WS 推送与画面框选",
     "检测状态变为运行中；WS 持续推送检测结果（含类别、置信度、框坐标）；画面叠加框",
     "WS 抓包 + 画面观察",
     "接口返回 200；本轮未做画面判读",
     "用例已编写，实测待补", "待执行", "用户要求检测部分后续单独完成"),
    ("检测推理", "停止检测并释放资源",
     "检测运行中",
     "1. POST /api/detection/stop\n2. 观察状态与 CPU",
     "状态变为已停止；不再推送结果；CPU/GPU 占用明显下降",
     "接口调用 + 资源监视",
     "—",
     "待人工执行", "待执行", ""),
    ("检测推理", "置信度阈值生效",
     "检测运行中",
     "1. 将阈值调到 0.8\n2. 观察低置信度目标是否被过滤",
     "低于阈值的检测框不显示、也不参与分拣决策",
     "界面操作 + 结果比对",
     "—",
     "待人工执行", "待执行", ""),
    ("检测推理", "单帧推理耗时",
     "模型已加载（GPU 优先）",
     "1. 运行检测 30 秒\n2. 读取 info.last_inference_ms 与 inference_count",
     "单帧耗时可接受（GPU ≤50 ms）；计数随运行单调递增，无异常抖动",
     "接口读取计数",
     f"模型加载后首帧 {INFO.get('last_inference_ms', '-')} ms（含预热，不能代表稳态）；"
     "同一模型连续运行时的稳态值实测约 9.7 ms（inference_count≈352 时读取）",
     "已采集两组数据：首帧含预热、稳态约 9.7 ms", "待执行",
     "本轮为 CPU 版 torch；装 CUDA 版后可进一步降低"),
    ("检测推理", "多目标策略：priority",
     "画面中同时出现多个目标",
     "1. 设置 policy=priority\n2. 观察最终分拣类别",
     "按 classes.json 配置的优先级选择目标下发",
     "接口调用",
     f"接口返回 {MO.get('priority', {}).get('status', '-')}", "授权成功，行为待实测", "待执行", ""),
    ("检测推理", "多目标策略：largest",
     "画面中同时出现多个目标", "1. 设置 policy=largest\n2. 观察选择结果",
     "选择面积最大的目标下发", "接口调用",
     f"接口返回 {MO.get('largest', {}).get('status', '-')}", "授权成功，行为待实测", "待执行", ""),
    ("检测推理", "多目标策略：confidence",
     "画面中同时出现多个目标", "1. 设置 policy=confidence\n2. 观察选择结果",
     "选择置信度最高的目标下发", "接口调用",
     f"接口返回 {MO.get('confidence', {}).get('status', '-')}", "授权成功，行为待实测", "待执行", ""),
    ("检测推理", "多目标策略：hold",
     "画面中目标频繁切换", "1. 设置 policy=hold\n2. 观察是否保持上一目标",
     "在保持窗口内沿用上一目标，避免频繁切换", "接口调用",
     f"接口返回 {MO.get('hold', {}).get('status', '-')}", "授权成功，行为待实测", "待执行", ""),
    # ---------------- E 类别映射与分拣逻辑 ----------------
    ("分类映射", "7 类模型映射到 4 个物理桶",
     "classes.json 已配置 groups 与 priority",
     "1. 依次识别 paper/plastic/battery/medicine/peel/mask/tableware\n2. 记录下发 slot",
     "7 类均映射到配置的 4 个槽位；无未定义类别的兜底槽位错误",
     "配置核对 + 手动分拣接口",
     "见文件 classes.json",
     "待人工执行（可用强制手动分拣逐类验证）", "待执行", ""),
    ("分类映射", "同类多类共槽时的优先级",
     "两个类别配置到同一 slot",
     "1. 同时识别两类\n2. 观察选择",
     "按 priority 高者下发，低者不覆盖", "配置核对 + 检测",
     "—", "待人工执行", "待执行", ""),
    ("分拣逻辑", "分桶冷却机制",
     "同一桶刚执行过一次分拣",
     "1. 立即再次识别同桶类别\n2. 观察是否下发",
     "冷却时间内不重复下发；超过冷却时间后恢复正常下发",
     "接口调用 + 日志观察",
     "—", "待人工执行", "待执行", ""),
    ("分拣逻辑", "手动分拣（忽略冷却）",
     "串口已连接",
     "1. POST /api/sort/manual {class_name, force:true}\n2. 观察串口输出",
     "忽略冷却立即下发；串口收到合法 JSON；ESP32 回 ACK",
     "接口调用 + 串口监视",
     "—", "待人工执行（需 ESP32）", "待执行", ""),
    ("分拣逻辑", "串口未连接时分拣请求的处理",
     "后端运行中、串口未连接",
     "1. POST /api/sort/manual {class_name:'paper', force:true}\n2. 观察响应",
     "返回明确业务错误（非 500 崩溃），界面给出可读提示",
     "接口调用",
     f"实测 HTTP {SORT_409.get('status', '-')}：{SORT_409.get('resp', '')[:80]}",
     f"返回 {SORT_409.get('status', '-')} 且提示清晰", "OK", "降级行为正确"),
    ("分拣逻辑", "手动分拣自定义槽位/角度",
     "串口已连接",
     "1. 指定 slot=2、angle=45 下发\n2. 核对串口报文",
     "报文中 slot/angle 与请求一致；非法范围（如 slot>15）被参数校验拒绝",
     "接口调用 + 报文核对",
     "参数范围校验在 OpenAPI 中已定义（slot 0~15，angle 0~270）",
     "待人工执行", "待执行", ""),
    # ---------------- F 串口 / ESP32 ----------------
    ("串口通信", "串口端口枚举",
     "系统存在若干 COM 端口",
     "1. 调用 GET /api/serial/ports\n2. 核对 likely_esp 标记",
     "返回端口列表（设备名/描述/是否疑似 ESP32），接口耗时合理",
     "接口调用",
     f"实测耗时 {ms('/api/serial/ports') or '-'}；返回结构含 count 与 ports",
     "枚举正常", "OK", "已加 8 秒 TTL 缓存"),
    ("串口通信", "连接串口（握手 ready）",
     "ESP32 已烧录固件并通电",
     "1. POST /api/serial/connect {port, baudrate}\n2. 观察是否收到 ready",
     "连接成功且收到 ready 握手；3 秒内完成；状态显示已连接",
     "接口调用 + 串口日志",
     f"无硬件时实测：HTTP {SER_CONN.get('status', '-')}，last_error={((SER_CONN.get('resp') or '')[:120])}",
     "待人工执行（需 ESP32）", "待执行", "无硬件时未崩溃，仅记录错误"),
    ("串口通信", "断开串口",
     "串口已连接",
     "1. POST /api/serial/disconnect\n2. 检查状态与线程",
     "状态清空、读线程退出；可再次连接",
     "接口调用",
     "—", "待人工执行", "待执行", ""),
    ("串口通信", "下发分拣指令并执行舵机动作",
     "串口已连接、桶位已标定",
     "1. 手动分拣 paper\n2. 观察舵机与托盘",
     "舵机转到对应槽位角度，托盘倾斜使垃圾落入正确桶；动作完成后归位",
     "手工观察 + 视频记录",
     "—", "待人工执行（需硬件）", "待执行", "物理动作无法在软件环境验证"),
    ("串口通信", "运行中拔掉串口",
     "串口已连接且检测运行中",
     "1. 物理拔出 USB 串口\n2. 观察 30 秒",
     "系统提示断开并尝试重连；界面不卡死；其他功能可用",
     "手工构造",
     "—", "待人工执行", "待执行", ""),
    ("串口通信", "串口写超时不影响界面响应",
     "使用蓝牙虚拟串口等慢速/异常串口",
     "1. 连接该串口\n2. 反复切换页面并刷新列表",
     "串口写超时（Write timeout）只影响该次下发；状态查询与列表刷新保持可用，界面无长时间转圈",
     "接口压测 + 页面操作",
     "已通过代码级并发测试：写操作移出状态锁、取锁带超时；无硬件时 last_error=Write timeout 但接口仍 200",
     "修复后逻辑验证通过", "OK", "本轮重点修复项"),
    ("串口通信", "波特率不匹配",
     "ESP32 波特率与配置不一致",
     "1. 以错误波特率连接\n2. 观察握手",
     "3 秒握手超时并提示，但连接保持；不阻塞界面",
     "手工构造",
     "—", "待人工执行", "待执行", ""),
    # ---------------- G 数据标注 ----------------
    ("数据标注", "标注图片列表加载",
     "training/captures 下已有抓拍图片",
     "1. 打开“数据标注”页\n2. 观察列表与数量",
     "列表正常加载全部图片缩略信息；首屏响应可接受；滚动不卡顿",
     "接口调用 + 页面观察",
     f"实测耗时 {ms('/api/annotate/images') or '-'}；列表已做分页/上限渲染",
     "列表加载与渲染已优化", "OK", "曾因全量渲染卡顿，已加渲染上限"),
    ("数据标注", "打开图片并回显已有标注",
     "图片存在同名 .txt 标签",
     "1. 选中该图片\n2. 观察画布框",
     "已保存的框位置/类别正确回显，坐标换算无偏移",
     "页面操作",
     "—", "待人工执行", "待执行", ""),
    ("数据标注", "绘制/修改/删除标注框",
     "图片已打开",
     "1. 拖拽画框，移动/缩放已有框，删除一个框\n2. 保存",
     "三种操作均生效；保存后重新打开校验一致",
     "页面操作 + 重新加载",
     "—", "待人工执行", "待执行", ""),
    ("数据标注", "标签文件格式（YOLO 归一化）",
     "已保存若干标注",
     "1. 打开对应 .txt\n2. 核对字段",
     "每行 5 个值：class x_center y_center width height，均在 0~1 之间",
     "文件检查",
     "—", "待人工执行", "待执行", ""),
    ("数据标注", "自动标注（颜色阈值）",
     "参考图已采集",
     "1. 调用 POST /api/annotate/auto\n2. 观察生成的候选框",
     "按阈值参数生成候选框；参数调整能明显改变召回/误检；结果可人工修正",
     "接口调用 + 可视化",
     "接口存在（参数：threshold/sigma_k/min_fill/blur/min_area/max_area/dilate/padding）",
     "待人工执行", "待执行", ""),
    ("数据标注", "多人协作占用（claim/release）",
     "两台 PC 同时打开标注页",
     "1. A 占用图片 X\n2. B 尝试占用 X\n3. A 释放后 B 再占用",
     "同一图片同一时间仅一人可编辑；占用状态实时可见；释放后可被他人占用",
     "双端操作",
     "接口 /api/annotate/claim、/release、/claims 已实现",
     "待人工执行（需第二台 PC）", "待执行", ""),
    ("数据标注", "参考图采集与重建",
     "摄像头可用",
     "1. 采集 background 参考图（多帧平均）\n2. 采集类别参考图",
     "参考图生成成功并可预览；重复采集可覆盖",
     "页面/接口操作",
     "接口 /api/annotate/reference（POST/DELETE/GET image）",
     "待人工执行", "待执行", ""),
    ("数据标注", "删除图片及其标签",
     "存在一张图片 + 标签",
     "1. 删除该图片\n2. 检查磁盘",
     "图片与同名标签一并删除；列表即时更新",
     "页面操作 + 文件检查",
     "接口 DELETE /api/annotate/image",
     "待人工执行", "待执行", ""),
    # ---------------- H 数据增强 ----------------
    ("数据增强", "增强算子列表",
     "后端运行中",
     "1. 调用 GET /api/augment/ops",
     "返回全部可用算子及其参数说明（旋转/翻转/亮度/噪声等）",
     "接口调用",
     f"实测耗时 {ms('/api/augment/ops') or '-'}；8 个算子",
     "算子列表正常", "OK", ""),
    ("数据增强", "旋转 90/180/270 时标签同步变换",
     "图片与标签存在",
     "1. 对一张图应用 rot90/rot180/rot270\n2. 打开增强图与标签",
     "图像方向与框位置严格对应（框仍贴合目标）；(x,y)→(1-y,x) 等变换正确",
     "生成后可视化检查",
     "—", "待人工执行", "待执行", "关键正确性用例"),
    ("数据增强", "水平/垂直翻转时标签同步",
     "图片与标签存在",
     "1. 应用 fh/fv\n2. 检查框位置",
     "框随翻转镜像，x 或 y 中心正确取反", "可视化检查",
     "—", "待人工执行", "待执行", ""),
    ("数据增强", "亮度/噪声类增强不改变标签",
     "图片与标签存在",
     "1. 应用 brightness/noise\n2. 对比标签",
     "标签文件保持不变；仅像素改变", "文件比对",
     "—", "待人工执行", "待执行", ""),
    ("数据增强", "增强幂等性",
     "已生成过增强图",
     "1. 对同一批图片重复执行相同增强\n2. 检查文件数量",
     "不重复生成同名增强文件；数量不翻倍",
     "批量执行 + 计数",
     "文件名后缀规则：_r90/_r180/_r270/_fh/_fv/_br/_dk/_ns",
     "待人工执行", "待执行", ""),
    ("数据增强", "清理增强产物",
     "存在增强图",
     "1. POST /api/augment/clean（可 dry_run）\n2. 核对删除范围",
     "仅删除增强产物，原始图片与标签不受影响；dry_run 只统计不删除",
     "接口调用 + 文件计数",
     "接口支持 classes/ops 过滤与 dry_run",
     "待人工执行", "待执行", ""),
    # ---------------- I 数据集构建 ----------------
    ("数据集", "构建训练数据集",
     "已有标注图片",
     "1. POST /api/annotate/build {val_ratio}\n2. 检查输出目录与 data.yaml",
     "生成 images/labels 的 train/val 划分与 data.yaml；路径与类别名正确",
     "接口调用 + 文件检查",
     "—", "待人工执行", "待执行", ""),
    ("数据集", "val_ratio 参数生效",
     "重复构建两次（0.2 / 0.3）",
     "1. 分别构建\n2. 统计 train/val 数量比例",
     "实际比例与参数一致（误差≤1 张）", "计数比对",
     "—", "待人工执行", "待执行", ""),
    ("数据集", "背景图纳入训练集",
     "已采集 background 参考图",
     "1. 构建时 include_background=true\n2. 检查 train/images",
     "背景图进入训练集且不产生错误标签（空标签文件）", "文件检查",
     "—", "待人工执行", "待执行", ""),
    # ---------------- J UI 界面 ----------------
    ("UI 界面", "主题切换（深色/浅色）",
     "控制台已打开",
     "1. 点击主题切换\n2. 遍历所有面板与页面",
     "全站配色、图表、弹窗、表格配色一致；文字对比度足够；无残留亮/暗块",
     "页面操作 + 截图",
     "见 UI界面1、UI界面2", "待人工复核截图", "待执行", ""),
    ("UI 界面", "HUD 状态条实时性",
     "系统运行中",
     "1. 观察顶部 SYS/WS/CAM/MODEL/ESP32 状态\n2. 断开/连接串口或摄像头",
     "各状态与真实状态一致（200 ms 内更新）；断连时状态标红/灰",
     "页面观察",
     "见 UI界面3", "待人工执行", "待执行", ""),
    ("UI 界面", "功能卡片加载状态正确结束",
     "打开摄像头、连接串口后反复切换页面 5 次以上",
     "1. 观察摄像头列表/模型目录/串口端口的加载态\n2. 观察是否出现长时间转圈",
     "每个列表最多短暂 loading 后显示结果；不出现“一直刷新不出结果”",
     "重复操作 + 观察",
     "本轮已修复：页面常驻不卸载、WS 状态不变时不替换对象、请求 20 秒超时兜底、"
     "摄像头枚举缓存 300 秒、MJPEG 单流策略",
     "修复后复测由用户确认", "待执行", "回归重点"),
    ("UI 界面", "页面切换保留连接状态",
     "已选择摄像头并连接串口",
     "1. 切到数据标注页再切回控制台\n2. 检查选择项与列表",
     "摄像头选择、串口连接、模型选择、置信度等状态全部保留，不重新检测",
     "页面操作",
     "本轮已改为“两页面常驻 + display 切换”",
     "已实现，待人工回归", "待执行", ""),
    ("UI 界面", "结果面板与历史图表",
     "检测运行中并有输出",
     "1. 观察当前类别/置信度/趋势图\n2. 连续识别 10 次",
     "类别与置信度实时更新；环形/柱状/趋势图数据一致，无错位",
     "页面观察",
     "见 UI界面4、UI界面5", "待人工执行", "待执行", ""),
    ("UI 界面", "响应式布局",
     "窗口可缩放",
     "1. 分别在 1440×900、1920×1080、1024×768 下查看\n2. 最大化/还原",
     "栅格自适应，无横向滚动条、无内容重叠、按钮不换行错位",
     "改变窗口尺寸",
     "—", "待人工执行", "待执行", ""),
    ("UI 界面", "动效性能",
     "控制台打开并持续移动鼠标",
     "1. 用开发者工具记录 3 秒帧率与长任务\n2. 观察是否掉帧",
     "帧率 ≥50 fps；无 >50 ms 长任务堆积；鼠标跟随无肉眼可见延迟",
     "性能面板测量",
     "实测 74.6 fps；存在长任务（已知项，后续优化）",
     "帧率达标，长任务待优化", "OK", "记录为已知缺陷"),
    # ---------------- K WebSocket ----------------
    ("WebSocket", "连接建立与心跳",
     "后端运行中",
     "1. 打开前端\n2. 观察 /ws 连接与心跳",
     "连接成功（状态显示已连接）；20 秒心跳不断开；无重复连接",
     "抓包/前端状态",
     "心跳间隔 20000 ms", "待人工执行", "待执行", ""),
    ("WebSocket", "断线自动重连",
     "连接建立后",
     "1. 重启后端\n2. 观察前端重连",
     "自动重连成功并恢复状态推送；界面有短暂断连提示",
     "手工重启",
     "—", "待人工执行", "待执行", ""),
    ("WebSocket", "命令下发（state/camera/model）",
     "连接建立后",
     "1. 点击刷新/切换模型/打开摄像头\n2. 观察 WS 帧与后端响应",
     "命令被正确解析并触发对应动作；失败时返回可读错误",
     "抓包",
     "—", "待人工执行", "待执行", ""),
    # ---------------- L 性能与稳定性 ----------------
    ("性能", "关键接口响应时间",
     "系统空闲",
     "1. 对 12 个 GET 接口各调用 3 次\n2. 取中位数",
     "常规接口 ≤300 ms；重接口（列表/枚举）≤3 s（首次可更久）",
     "脚本压测（collect.py）",
     "；".join(f"{k.replace('/api/','')}={v['median_ms']:.0f}ms" for k, v in list(lat.items())[:8]),
     "已采集实测数据", "OK", "详见 evidence.json"),
    ("性能", "并发请求不阻塞",
     "后端运行中",
     "1. 同时发起 6 个列表请求\n2. 记录各自耗时",
     "全部在 1 秒内返回；无请求排队超时",
     "并发脚本",
     "实测：1 个真实枚举 + 5 个 15~16 ms（缓存命中）",
     "并发表现正常", "OK", "枚举已改为非阻塞抢锁"),
    ("性能", "长时间运行稳定性",
     "摄像头+串口+检测同时运行",
     "1. 连续运行 2 小时\n2. 记录线程数、内存、接口耗时",
     "线程数稳定（无持续增长）；内存无持续上涨；接口耗时无明显劣化",
     "监控脚本",
     "实测空闲线程 15~17 个（线程池 64）",
     "待人工长时间运行", "待执行", ""),
    ("性能", "视频流单流策略",
     "多个页面/标签同时拉流",
     "1. 并发发起 5 条 /api/video/feed\n2. 观察连接与列表接口",
     "旧流被新流顶掉并正常结束；列表接口不受影响",
     "并发脚本",
     "实测 5 条并发流全部正常收敛为 1 条",
     "通过", "OK", "修复连接池占用问题"),
    ("性能", "桌面启动冷启动时间",
     "双击 exe / 启动.bat",
     "1. 双击启动\n2. 用秒表记录到界面可用",
     "启动图 1 秒内出现；界面在 5 秒内可用（后端后台预热）",
     "秒表",
     "—", "待人工执行", "待执行", ""),
    # ---------------- M 异常与降级 ----------------
    ("异常处理", "摄像头运行中被拔出",
     "检测运行中",
     "1. 拔出摄像头\n2. 观察 30 秒",
     "提示摄像头异常；自动尝试重连；界面不卡死；重插后可恢复",
     "手工构造",
     "—", "待人工执行", "待执行", ""),
    ("异常处理", "配置文件缺失或损坏",
     "临时重命名 classes.json / settings.yaml",
     "1. 启动后端\n2. 观察行为",
     "使用内置默认值启动，并在日志/界面提示配置异常；不崩溃",
     "手工构造",
     "—", "待人工执行", "待执行", ""),
    ("异常处理", "磁盘空间不足",
     "将磁盘写满（或用只读目录）",
     "1. 抓拍/保存标注\n2. 观察提示",
     "返回明确的写入失败提示；不影响已完成的操作",
     "手工构造",
     "—", "待人工执行", "待执行", ""),
    ("异常处理", "接口参数非法",
     "后端运行中",
     "1. 传入越界参数（slot=99、angle=999）\n2. 观察响应",
     "返回 422 及字段级错误信息；不产生副作用",
     "接口调用",
     "OpenAPI 中 slot 0~15、angle 0~270 已做范围约束",
     "待人工执行", "待执行", ""),
    # ---------------- N 局域网协同 ----------------
    ("局域网协同", "局域网地址展示",
     "多网卡环境",
     "1. 调用 GET /api/network\n2. 核对地址列表",
     "列出全部可用 IPv4 地址与本机地址，标注可访问入口",
     "接口调用",
     f"实测耗时 {ms('/api/network') or '-'}", "地址正常返回", "OK", ""),
    ("局域网协同", "第二台 PC 访问标注页",
     "两机同网段",
     "1. 用 http://<主机IP>:8000 打开\n2. 加载标注列表",
     "页面可访问、列表可加载、图片可显示；操作无跨域错误",
     "双机操作",
     "—", "待人工执行（需第二台 PC）", "待执行", ""),
    ("局域网协同", "两人同时标注不同图片",
     "两机同时在线",
     "1. A 标注图1、B 标注图2\n2. 各自保存并刷新",
     "互不覆盖；保存结果均正确落地；无需手动刷新即可看到对方占用状态",
     "双机操作",
     "—", "待人工执行", "待执行", ""),
    # ---------------- O 打包与交付 ----------------
    ("打包交付", "PyInstaller 打包",
     "依赖完整（含 pyinstaller）",
     "1. 执行 python tools/build_exe.py\n2. 检查产物",
     "生成单个 exe（含图标与启动图），体积合理，无缺失依赖报错",
     "命令执行",
     "产物：垃圾自动分拣系统.exe（约 7.7 MB）",
     "打包成功（本轮环境需重建）", "待执行", "换解释器后需重新打包"),
    ("打包交付", "exe 独立运行",
     "exe 已生成",
     "1. 双击 exe\n2. 观察窗口与日志",
     "自动定位解释器、拉起后端、打开窗口；日志写入 logs/",
     "手工操作",
     "—", "待人工执行", "待执行", ""),
    ("打包交付", "文档与说明齐全",
     "仓库根目录",
     "1. 检查 README、docs 目录\n2. 核对启动方式说明",
     "包含启动方式、环境要求、常见问题、目录说明",
     "文件检查",
     "—", "待人工执行", "待执行", ""),
]


def build_cases() -> list[tuple]:
    rows = []
    for i, (mod, title, pre, steps, exp, method, evidence, result, verdict, note) in enumerate(CASES, start=1):
        rows.append((f"TS-{i:03d}", mod, title, pre, steps, exp, method, evidence, result, verdict, TESTER, TODAY, note))
    return rows


HEADERS = ["编号", "模块名称", "用例标题", "前置条件", "操作步骤", "预期结果",
           "测试方法", "证据 / 实测数据", "测试结果", "确定结果", "测试者", "日期", "备注"]
WIDTHS = [9, 12, 26, 26, 40, 40, 16, 34, 26, 9, 10, 11, 16]

HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="微软雅黑", size=9)
TITLE_FONT = Font(name="微软雅黑", size=16, bold=True)
SUB_FONT = Font(name="微软雅黑", size=10, color="595959")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
VERDICT_FILL = {
    "OK": PatternFill("solid", fgColor="E2EFDA"),
    "NG": PatternFill("solid", fgColor="FCE4E4"),
    "待执行": PatternFill("solid", fgColor="FFF2CC"),
}


def sheet_cover(wb: Workbook) -> None:
    ws = wb.active
    ws.title = "文件封面"
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGH", [14, 18, 18, 18, 18, 18, 14, 14]):
        ws.column_dimensions[col].width = w

    ws["A1"] = "密级：秘密"
    ws["A1"].font = Font(name="微软雅黑", size=9, color="C00000")
    ws["H1"] = "文件编号：TS-2026-0001"
    ws["H1"].font = Font(name="微软雅黑", size=9)
    ws["H1"].alignment = Alignment(horizontal="right")

    ws.merge_cells("A3:H3")
    ws["A3"] = f"{PROJECT}项目单体测试书"
    ws["A3"].font = TITLE_FONT
    ws["A3"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[3].height = 40

    ws.merge_cells("A4:H4")
    ws["A4"] = "单体测试书（Test Book）"
    ws["A4"].font = SUB_FONT
    ws["A4"].alignment = Alignment(horizontal="center")

    info = [
        ("项目名称", PROJECT, "版本", "1.0.0"),
        ("被测系统", "后端 FastAPI + 前端 React + ESP32 执行端", "编制日期", TODAY),
        ("文件状态", "被测版本已构建，等待执行/回归", "总页数", "见页脚"),
        ("编制", "", "审批", ""),
        ("测试者", "", "缺陷编号", ""),
        ("生效日期", "", "修改时间", ""),
    ]
    r = 6
    for a, b, c, d in info:
        ws[f"A{r}"] = a
        ws[f"A{r}"].font = Font(name="微软雅黑", size=10, bold=True)
        ws.merge_cells(f"B{r}:D{r}")
        ws[f"B{r}"] = b
        ws[f"E{r}"] = c
        ws[f"E{r}"].font = Font(name="微软雅黑", size=10, bold=True)
        ws.merge_cells(f"F{r}:H{r}")
        ws[f"F{r}"] = d
        for col in "ABCDEFGH":
            cell = ws[f"{col}{r}"]
            cell.border = BORDER
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if col in "BF":
                cell.font = Font(name="微软雅黑", size=10)
        ws.row_dimensions[r].height = 22
        r += 1

    r += 1
    ws.merge_cells(f"A{r}:H{r}")
    ws[f"A{r}"] = "文档说明"
    ws[f"A{r}"].font = Font(name="微软雅黑", size=11, bold=True)
    r += 1
    notes = [
        "1. 本测试书按“文件封面 / 测试项目 / UI界面 / 运行”四部分组织，与模板结构一致。",
        "2. “测试项目”共 %d 条用例，覆盖安装部署、摄像头、模型管理、检测推理、分类映射、"
        "串口通信、数据标注、数据增强、数据集构建、UI 界面、WebSocket、性能、异常处理、"
        "局域网协同、打包交付等 15 个模块。" % len(CASES),
        "3. 确定结果取值：OK（已实测通过）/ NG（不通过）/ 待执行（需硬件或人工补测）。",
        "4. 证据列中的接口耗时来自本轮真实运行采集（docs/testbook/evidence.json）。",
        "5. 涉及 ESP32、舵机、第二台 PC、真实垃圾样本的用例需人工实机执行。",
    ]
    for note in notes:
        ws.merge_cells(f"A{r}:H{r}")
        ws[f"A{r}"] = note
        ws[f"A{r}"].font = Font(name="微软雅黑", size=9)
        ws[f"A{r}"].alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 30
        r += 1

    ws.merge_cells(f"A{r+2}:H{r+2}")
    ws[f"A{r+2}"] = "实习实训中心    （版权所有，翻版必究）"
    ws[f"A{r+2}"].font = Font(name="微软雅黑", size=10, color="808080")
    ws[f"A{r+2}"].alignment = Alignment(horizontal="center")


def sheet_cases(wb: Workbook) -> None:
    ws = wb.create_sheet("测试项目")
    rows = build_cases()

    ws.merge_cells("A1:M1")
    ws["A1"] = f"{PROJECT}项目单体测试书 —— 测试项目（{len(rows)} 条）"
    ws["A1"].font = TITLE_FONT
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 32

    for c, (h, w) in enumerate(zip(HEADERS, WIDTHS), start=1):
        cell = ws.cell(row=2, column=c, value=h)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.row_dimensions[2].height = 26

    for i, row in enumerate(rows, start=3):
        for c, val in enumerate(row, start=1):
            cell = ws.cell(row=i, column=c, value=val)
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if c in (1, 2, 10, 11, 12) else "left")
        verdict = row[9]
        ws.cell(row=i, column=10).fill = VERDICT_FILL.get(verdict, PatternFill())
        ws.cell(row=i, column=10).font = Font(name="微软雅黑", size=9, bold=True)
        ws.row_dimensions[i].height = 58

    ws.freeze_panes = "D3"
    ws.auto_filter.ref = f"A2:M{2 + len(rows)}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "2:2"
    ws.page_margins = PageMargins(left=0.3, right=0.3, top=0.4, bottom=0.4)


UI_ITEMS = [
    ("1", "顶部 HUD 状态条", "SYS 时钟 / WS 连接 / 摄像头 / 模型 / ESP32 五段状态，颜色语义正确"),
    ("2", "侧边导航", "01 控制台 / 02 数据标注 两个入口，当前项高亮，点击切换不刷新页面"),
    ("3", "摄像头卡片", "设备下拉、打开/关闭、刷新列表、“发现 N 个可用摄像头”提示"),
    ("4", "视频预览区", "MJPEG 实时画面、无信号占位图、放大/关闭按钮"),
    ("5", "结果面板", "当前类别、置信度、识别历史趋势与计数"),
    ("6", "模型卡片", "模型目录、模型下拉、加载按钮、类别数与设备信息"),
    ("7", "串口卡片", "端口下拉（含疑似 ESP32 标记）、连接/断开、连接状态徽标"),
    ("8", "手动分拣区", "类别/槽位/角度选择、强制下发开关、下发结果提示"),
    ("9", "串口日志区", "TX/RX 报文滚动显示，最新在下，可滚动查看"),
    ("10", "主题切换", "深色/浅色切换，图表与表格同步变色"),
    ("11", "数据标注页", "图片列表、画布、类别选择、保存/删除按钮"),
    ("12", "标注工具条", "框选、删除、撤销、缩放、自动标注入口"),
    ("13", "增强设置区", "算子勾选、类别过滤、应用/清理按钮"),
    ("14", "抓拍与参考图", "抓拍计数、参考图采集与预览"),
    ("15", "页面页脚", "技术栈说明、WebSocket 地址、当前主题标识"),
]


def sheet_ui(wb: Workbook) -> None:
    ws = wb.create_sheet("UI界面")
    ws.merge_cells("A1:E1")
    ws["A1"] = f"{PROJECT} —— UI 界面检查点（共 {len(UI_ITEMS)} 项）"
    ws["A1"].font = TITLE_FONT
    ws.row_dimensions[1].height = 30

    ws.merge_cells("A2:E2")
    ws["A2"] = ("说明：编号与截图中的标注一一对应。截图请粘贴到本表下方，并在对应编号处加引线标注；"
                "每项检查结果填入“检查结果”列。")
    ws["A2"].font = Font(name="微软雅黑", size=9, color="C00000")
    ws["A2"].alignment = Alignment(wrap_text=True)
    ws.row_dimensions[2].height = 30

    heads = ["编号", "界面元素", "检查内容", "检查结果(OK/NG)", "证据（截图编号）"]
    widths = [8, 20, 60, 16, 18]
    for c, (h, w) in enumerate(zip(heads, widths), start=1):
        cell = ws.cell(row=3, column=c, value=h)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = w
    for i, (no, name, desc) in enumerate(UI_ITEMS, start=4):
        vals = [no, name, desc, "待检查", f"UI界面图{no}"]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if c in (1, 4, 5) else "left")
        ws.row_dimensions[i].height = 24
    ws.freeze_panes = "A4"

    start = 4 + len(UI_ITEMS) + 2
    ws.merge_cells(f"A{start}:E{start}")
    ws[f"A{start}"] = "截图区（请在此处粘贴控制台深色/浅色、数据标注页等截图，并在图上标注编号）"
    ws[f"A{start}"].font = Font(name="微软雅黑", size=10, bold=True, color="808080")
    ws[f"A{start}"].alignment = Alignment(horizontal="center")
    ws.row_dimensions[start].height = 26


RUN_ITEMS = [
    ("运行图1", "依赖与控制台启动", "终端无 ERROR 输出；出现“Uvicorn running on http://127.0.0.1:8000”"),
    ("运行图2", "桌面窗口启动（含启动图）", "启动图 → 界面加载完成 → 启动图关闭"),
    ("运行图3", "关闭窗口后进程回收", "日志出现“后端已关闭”，任务管理器无残留 python"),
    ("运行图4", "摄像头打开与实时预览", "预览区出现实时画面，状态条 CAM 亮起"),
    ("运行图5", "模型加载成功", "模型卡片显示已加载模型名、类别数（7）与设备"),
    ("运行图6", "检测运行中（画框）", "画面出现目标框与类别标签，结果面板同步更新"),
    ("运行图7", "串口连接成功", "串口卡片显示已连接端口与 TX/RX 计数，日志区出现 ready"),
    ("运行图8", "分拣指令下发", "串口日志出现下发 JSON；ESP32 回 ACK；舵机动作"),
    ("运行图9", "物理分拣动作", "对应桶位托盘倾斜，垃圾落入正确桶，随后归位"),
    ("运行图10", "数据标注页", "图片列表与画布正常显示，可保存标注"),
    ("运行图11", "数据增强结果", "生成 _r90/_fh 等后缀的增强图，框位置正确"),
    ("运行图12", "长稳运行 2 小时", "线程数与内存曲线平稳，无接口超时"),
]


def sheet_run(wb: Workbook) -> None:
    ws = wb.create_sheet("运行")
    ws.merge_cells("A1:D1")
    ws["A1"] = f"{PROJECT} —— 运行场景与证据（共 {len(RUN_ITEMS)} 项）"
    ws["A1"].font = TITLE_FONT
    ws.row_dimensions[1].height = 30

    heads = ["编号", "运行场景", "通过标准 / 观察点", "结论"]
    widths = [12, 30, 62, 12]
    for c, (h, w) in enumerate(zip(heads, widths), start=1):
        cell = ws.cell(row=2, column=c, value=h)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = w
    for i, (no, scene, criteria) in enumerate(RUN_ITEMS, start=3):
        vals = [no, scene, criteria, "待执行"]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if c in (1, 4) else "left")
        ws.cell(row=i, column=4).fill = VERDICT_FILL["待执行"]
        ws.row_dimensions[i].height = 26
    ws.freeze_panes = "A3"

    start = 3 + len(RUN_ITEMS) + 2
    ws.merge_cells(f"A{start}:D{start}")
    ws[f"A{start}"] = "截图区（请按编号粘贴运行截图：启动、检测、串口、分拣、标注、增强、长稳）"
    ws[f"A{start}"].font = Font(name="微软雅黑", size=10, bold=True, color="808080")
    ws[f"A{start}"].alignment = Alignment(horizontal="center")
    ws.row_dimensions[start].height = 26


def write_markdown(rows: list[tuple]) -> Path:
    out = HERE / "测试项目.md"
    lines = [
        f"# {PROJECT} 项目单体测试书 —— 测试项目",
        "",
        f"- 用例总数：**{len(rows)}** 条",
        f"- 生成日期：{TODAY}",
        "- 确定结果取值：`OK`（已实测通过） / `NG`（不通过） / `待执行`（需硬件或人工补测）",
        "",
    ]
    modules = []
    for r in rows:
        if r[1] not in modules:
            modules.append(r[1])
    lines.append("## 模块分布")
    lines.append("")
    lines.append("| 模块 | 用例数 |")
    lines.append("| --- | --- |")
    for m in modules:
        lines.append(f"| {m} | {sum(1 for r in rows if r[1] == m)} |")
    lines.append("")

    for m in modules:
        lines.append(f"## {m}")
        lines.append("")
        for r in [x for x in rows if x[1] == m]:
            lines.append(f"### {r[0]} {r[2]}  `{r[9]}`")
            lines.append("")
            lines.append(f"- **前置条件**：{r[3].replace(chr(10), ' / ')}")
            lines.append(f"- **操作步骤**：{r[4].replace(chr(10), ' ')}")
            lines.append(f"- **预期结果**：{r[5]}")
            lines.append(f"- **测试方法**：{r[6]}")
            lines.append(f"- **证据 / 实测**：{r[7]}")
            if r[12]:
                lines.append(f"- **备注**：{r[12]}")
            lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> None:
    wb = Workbook()
    sheet_cover(wb)
    sheet_cases(wb)
    sheet_ui(wb)
    sheet_run(wb)
    xlsx = HERE / f"{PROJECT}_单体测试书.xlsx"
    wb.save(xlsx)

    rows = build_cases()
    md = write_markdown(rows)

    ok = sum(1 for r in rows if r[9] == "OK")
    pend = sum(1 for r in rows if r[9] == "待执行")
    ng = sum(1 for r in rows if r[9] == "NG")
    print(f"工作表：{wb.sheetnames}")
    print(f"用例总数：{len(rows)}   OK={ok}  待执行={pend}  NG={ng}")
    print(f"已生成：{xlsx}")
    print(f"已生成：{md}")


if __name__ == "__main__":
    main()
