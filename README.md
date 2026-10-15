# YOLO 垃圾自动识别分类系统

基于 **YOLO 目标检测 + ESP32 双舵机托盘** 的垃圾自动分拣系统：摄像头看到托盘上的垃圾 → 模型识别类别 → 后端映射到垃圾大类 → 串口下发指令 → ESP32 控制舵机把垃圾倒进对应的桶。

分类体系采用中国生活垃圾分类**四大类**：

| 物理桶 (slot) | 大类 | 识别类别 |
| :---: | --- | --- |
| 0 | 可回收物 | `paper` 纸张、`plastic` 塑料 |
| 1 | 有害垃圾 | `battery` 电池、`medicine` 药品 |
| 2 | 厨余垃圾（湿垃圾） | `peel` 果皮 |
| 3 | 其他垃圾（干垃圾） | `mask` 口罩、`tableware` 一次性餐具 |

---

## 目录

- [功能特性](#功能特性)
- [系统架构](#系统架构)
- [目录结构](#目录结构)
- [硬件清单](#硬件清单)
- [快速开始](#快速开始)
- [使用流程](#使用流程)
- [两台电脑协同（一人抓拍、一人标注）](#两台电脑协同一人抓拍一人标注)
- [串口协议](#串口协议)
- [配置说明](#配置说明)
- [后端接口](#后端接口)
- [模型训练](#模型训练)
- [常见问题（避坑）](#常见问题避坑)
- [已知限制](#已知限制)

---

## 功能特性

**后端**（Python / FastAPI）

- 摄像头由后端 OpenCV 统一管理：枚举 0~10 号设备、dshow/msmf 多后端回退、真实读帧校验
- YOLO 推理（ultralytics），模型热切换（扫描 `backend/models/*.pt`，无需重启）
- MJPEG 预览流（`multipart/x-mixed-replace`），检测框直接画在流上
- WebSocket 实时推送检测结果、分类下发、串口报文与全量状态
- 两层类别映射：识别类别 → 物理桶（改配置即可扩展，主流程不动）
- **多目标决策策略**：一帧里出现多个不同类别的垃圾时，可选 优先级 / 最大件 / 置信度 / 暂停
- **自愈看门狗**：摄像头抓帧线程、检测线程、事件泵任一挂掉都会自动重启
- 内置**数据标注服务**与**背景差分自动标注**，可直接从网页生成 YOLO 数据集
- **局域网协同**：后端可直接托管前端，另一台电脑打开一个网址即可标注（无需装环境）；
  带**图片认领**机制，两人并行标注不会互相覆盖

**前端**（React + Vite + TypeScript + Ant Design）

- 左侧导航两个页面：**控制台**（摄像头 / 模型 / 串口 / 识别结果）与**数据标注**
- 控制台：实时预览、模型与置信度切换、串口连接与手动下发、识别结果与统计、多目标策略切换
- 数据标注：内嵌摄像头画面、一键抓拍、鼠标画框 / 拖动 / 缩放、背景差分自动标注、一键导出数据集

**ESP32 固件**（PlatformIO + Arduino）

- 非阻塞状态机（`millis()`），倾倒过程分阶段推进，不阻塞串口接收
- 一行一个 JSON，`busy` / `done` / `error` / `pong` 状态回报
- 4 个倾倒方向由固件内的角度表按 `slot` 决定，支持 `h` / `v` 字段临时覆盖

---

## 系统架构

```
┌──────────────────── 浏览器（React + Ant Design）─────────────────────┐
│  控制台：摄像头预览 / 模型 / 串口 / 识别结果 / 多目标策略              │
│  数据标注：抓拍 / 画框 / 背景差分自动标注 / 生成数据集                 │
└──────────┬───────────────────────────────────────┬──────────────────┘
           │ REST（/api/**）                        │ MJPEG 预览流
           │ WebSocket（/ws，实时推送）              │
┌──────────▼───────────────────────────────────────▼──────────────────┐
│                        后端 FastAPI（Python）                        │
│                                                                     │
│   CameraService ──帧──► Pipeline ──► DetectorService（YOLO）          │
│        │                   │                                        │
│        │                   ├─► classes.json 映射：类别 → 物理桶       │
│        │                   └─► SerialService ──一行JSON──► ESP32     │
│        │                                                            │
│        └──抓拍──► Annotator（参考图 / 背景差分 / 标注读写 / 导出数据集）│
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ USB 串口 115200
                        ┌──────────▼───────────┐
                        │  ESP32 + 2× 舵机      │──► 4 个物理桶
                        │  水平 0~270° / 竖直    │
                        └──────────────────────┘
```

---

## 目录结构

```
trash-sorter/
├── backend/                       # 后端（Python / FastAPI）
│   ├── main.py                    # 应用入口：接口、WebSocket、看门狗
│   ├── requirements.txt
│   ├── config/
│   │   ├── settings.yaml          # 全局配置（摄像头/模型/串口/检测策略）
│   │   └── classes.json           # 两层映射表：识别类别 → 物理桶（含优先级）
│   ├── models/                    # 放 .pt 权重（不入库，支持热切换）
│   └── services/
│       ├── camera.py              # 枚举/选择/抓帧/MJPEG/设备锁
│       ├── detector.py            # ultralytics 封装、模型热切换
│       ├── serial_esp32.py        # 串口扫描/连接/收发
│       ├── pipeline.py            # 检测主循环、冷却、多目标决策
│       ├── annotator.py           # 标注读写、背景差分、数据集导出、数据增强调度
│       └── augmenter.py           # 旋转/翻转/明暗变换与标注换算（纯函数，可单独复用）
│
├── frontend/                      # 前端（React + Vite + TS + AntD）
│   ├── vite.config.ts             # /api 与 /ws 反向代理配置
│   └── src/
│       ├── App.tsx                # 布局与页面切换、WS 事件分发
│       ├── api.ts                 # 类型化 REST 客户端
│       ├── styles.css
│       └── components/
│           ├── CameraPanel.tsx    # 摄像头面板（含抓拍采集）
│           ├── CameraPreview.tsx  # 内嵌实时预览（标注页复用）
│           ├── ModelPanel.tsx     # 模型选择与置信度
│           ├── SerialPanel.tsx    # 串口连接与手动下发
│           ├── ResultPanel.tsx    # 识别结果与多目标策略
│           └── AnnotatePanel.tsx  # 数据标注页
│
├── esp32/                         # ESP32 固件
│   ├── platformio.ini
│   └── src/main.cpp               # 舵机状态机 + 串口协议
│
├── training/                      # 训练与数据集工具
│   ├── train.py                   # 训练脚本（数据集体检 + 类别校验 + 显存预检）
│   ├── data.yaml                  # 类别定义（7 类）+ 数据集路径（唯一口径）
│   └── prepare_dataset.py         # 旧版：多边形标注 → 检测框（保留备用）
│
└── sketch_sep26b/                 # 早期参考草图（原始舵机动作）
```

> **不入库的内容**：模型权重、`training/runs/`（训练产物）、`training/captures/`（抓拍图片）、
> `training/datasets/`、`trashnet/`、`node_modules/`、`dist/`、`.pio/`。详见 `.gitignore`。

---

## 硬件清单

| 部件 | 说明 |
| --- | --- |
| ESP32 开发板 | 常见 `esp32dev`（ESP32-WROOM-32），USB 转串口为 CP210x / CH340 |
| 舵机 ×2 | 水平舵机（本文档按 0~270° 标定）+ 竖直舵机（30~180°），实际以你的标定为准 |
| USB 摄像头 | 建议 720p（本文档用 1280×720 @30fps） |
| 托盘 | 固定在竖直舵机上，倾倒时把垃圾滑出 |
| 收集桶 ×4 | 对应四大类，摆在水平舵机的 4 个方向上 |
| 供电 | 舵机用**独立 5V 电源**，别从 ESP32 取电（会复位） |

---

## 快速开始

### 0. 环境要求

| 组件 | 版本 |
| --- | --- |
| Python | 3.9+（本文档在 3.10 验证） |
| Node.js | 18+ |
| PlatformIO | 仅烧录 ESP32 时需要 |
| GPU（可选） | 训练建议 NVIDIA + CUDA；推理 CPU 也可运行 |

### 1. 后端

```bash
# 安装依赖（GPU 版 torch 请先按 https://pytorch.org 装好，再装本文件其余依赖）
pip install -r backend/requirements.txt

# 启动（默认 0.0.0.0:8000）
python -m backend.main
```

启动后访问 <http://127.0.0.1:8000/docs> 可看到自动生成的接口文档。

把训练好的权重放进 `backend/models/`（例如 `best.pt`），后端启动会自动加载；也可以在前端「模型」面板里热切换。

### 2. 前端

```bash
cd frontend
npm install
npm run dev          # http://127.0.0.1:5173
```

开发服务器会把 `/api` 与 `/ws` 代理到 `127.0.0.1:8000`（见 `vite.config.ts`）。若后端在别的机器上：

```bash
# frontend/.env.local
VITE_API_BASE=http://192.168.1.20:8000
VITE_WS_URL=ws://192.168.1.20:8000/ws
```

生产构建：`npm run build` → 产物在 `frontend/dist/`。

### 3. ESP32

```bash
cd esp32
pio run                 # 编译
pio run -t upload       # 烧录
pio device monitor      # 串口监视（115200）
```

`platformio.ini` 里已启用 `-DCORE_DEBUG_LEVEL=0`，保证串口协议通道干净；调试时打开 `-DTRASH_SORTER_DEBUG=1`。

---

## 使用流程

### 分拣（控制台页）

1. 「摄像头」面板 → 选择设备 → 打开（保持预览）
2. 「串口」面板 → 扫描 → 连接 ESP32（会自动优先 CP210x / CH340 / USB-SERIAL）
3. 把垃圾放到托盘上，按下「开始检测」
4. 识别到且置信度达标 → 冷却判定通过 → 串口下发 → 舵机倾倒 → 回位

### 多目标策略

托盘只有"往一个方向倾斜"一个动作，**倾一次会把托盘上的东西全部倒进同一个桶**。
所以当两个不同大类的垃圾同时在托盘上时，必然有一件会被分错，策略只决定"牺牲哪一个"：

| 策略 | 行为 |
| --- | --- |
| `priority`（默认） | 按桶优先级倒，优先级在 `classes.json` 的 `groups[].priority` 里配（默认 有害 0 > 厨余 1 > 可回收 2 > 其他 3） |
| `largest` | 倒画面里面积最大的那件 |
| `confidence` | 倒置信度最高的那件 |
| `hold` | 不下发，界面提示"一次只能放一件"（**零错分**，需要人工介入） |

在**控制台 →「识别结果」面板顶部**可以随时切换，切换会立即生效并写回 `settings.yaml`。

### 数据采集 → 标注 → 训练（数据标注页）

自带完整闭环，不需要第三方标注工具：

1. **抓拍**：标注页上方有实时画面，选类别（`background` 或 7 个识别类别）→ 点「抓拍」→ 图片落到 `training/captures/<类别>/` 并自动选中
   - 空托盘图请选 `background`，它作为**背景样本**（空标签）参与训练，是解决"托盘被误识别"的关键数据
2. **标注**：在图上拖拽画框；框内拖动 = 移动；选中后拖 8 个控制点 = 改大小；`Delete` 删框；`←/→` 切图；数字键 `1~9` 选类别
3. **自动标注**（可选，强烈推荐）：先清空托盘，点「连拍 8 帧取中值设为参考图」，
   之后点「自动标注当前图」会用**背景差分**（当前图 − 空托盘参考图）自动找出物体并填框；
   参数满意后点「批量自动标注未标注图」
4. **数据增强（可选）**：右栏「数据增强」可以给**已标注**的图批量生成变换后的副本，标注同步换算
   - 选类别（例如只选 `paper` / `plastic`）+ 选变换：旋转 90°/180°/270°、水平/垂直翻转、提亮/压暗/加噪点
   - 「**增强当前图**」生成后会自动打开结果，方便肉眼核对框有没有跟着转过去
   - 「**批量增强**」先弹确认（含预估张数：源图 N × 变换 M），产物与原图**同目录**、文件名加后缀
     （`paper_x.jpg` → `paper_x_r90.jpg`），所以会直接出现在列表里，「生成数据集」时自动带上
   - 「**清理增强图**」只删文件名带这些后缀的图（含标签），**原图和手工标注绝不受影响**
   - 为什么只做 90° 的整数倍和翻转：检测框是**轴对齐矩形**，这些变换下标注能**精确换算**、
     框里不会混入背景；任意角度旋转会把框撑大，等于教模型学错
   - 增强产物不会再次被增强（否则 `r90_r90…` 会组合爆炸）；重复执行是幂等的
5. **生成数据集**：点「生成数据集」→ 输出 `training/datasets/captures/` 与 `training/data-captures.yaml`
6. **训练**：见下一节

---

## 两台电脑协同（一人抓拍、一人标注）

一个人守着设备抓拍、另一个人在另一台电脑上标注，**不需要同步数据、不需要装任何东西在 B 机上**：
B 直接用浏览器打开 A 的地址，看到的就是同一份图片库，标注结果直接写在 A 的磁盘上（训练机）。

```
PC-A（相机 + 托盘 + ESP32 + 后端 + 训练）        PC-B（只需一个浏览器）
  python -m backend.main                         打开 http://<A的局域网IP>:8000
  training/captures/  ◄── 标注结果直接落盘 ────   同一套界面，协同标注
```

### 一次性准备（在 A 机上）

```bash
cd frontend
npm run build          # 构建一次，之后 A/B 都直接开网址，不用再跑 npm run dev
```

### 每台机器的操作

| | 操作 |
| --- | --- |
| **A 机** | `python -m backend.main` —— 启动横幅会直接打印局域网地址，例如 `http://172.16.17.120:8000   <- 发给另一位标注员（推荐）` |
| **A 机** | 首次需要放行防火墙：控制面板 → Windows Defender 防火墙 → 允许应用通过防火墙（或 `New-NetFirewallRule -DisplayName "trash-sorter" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow`，需管理员） |
| **B 机** | 浏览器打开上面那个地址即可。**不用装 Python、Node，也不用配 CORS**（同源访问） |

界面右上角有一个紫色「**共享 x.x.x.x:8000**」标签，点一下即可复制地址发给同事；
悬停能看到全部网卡（虚拟机的网卡也在里面，选能 ping 通的那条）。

B 机上的「摄像头画面」看到的就是 **A 的摄像头**，所以 B 也可以远程点「抓拍」——
于是流程可以是：**A 摆件 → B 看着实时画面抓拍 + 立刻标注**。

### 两个人不会撞车：图片认领

两个人共用同一份待标注列表时，最容易发生"同时点开同一张、各自画一半、后保存的覆盖先保存的"。
系统用**认领（claim）+ 自动过期**解决：

- 打开一张图会**先认领**；被别人占用时会提示"正在被另一位标注员处理（约 N 秒后自动释放）"并拒绝打开
- 列表里被占用的那行会**变灰、名字加删除线、显示蓝色「标注中」标签**
- 「保存并下一张」会**跳过**被别人占用的图
- 打开期间每 45 秒自动续租；浏览器直接关掉/断网时，靠 `app.claim_ttl_seconds`（默认 120 秒）自动释放，不会把图永久锁死
- 切图、离开标注页、关页面时都会尽量释放

> 认领只存在后端内存里，**重启后端即全部释放**；也不记录"是谁标的"，只区分浏览器标签页。
> 同一个浏览器开两个标签页会被当成两个人，方便自己一个人测协作流程。

### 如果 B 想跑开发模式（可选）

B 机上也可以 `npm run dev`，只要在 `frontend/.env.local` 里指向 A：

```bash
VITE_API_BASE=http://172.16.17.120:8000
VITE_WS_URL=ws://172.16.17.120:8000/ws
```

（这种方式浏览器是从 B 自己的 `127.0.0.1:5173` 发起请求，正好在默认 CORS 白名单里；
若用局域网 IP 打开 dev server，需要把该来源加进 `app.cors_origins`。）

### 注意

- **A 机必须开着**：B 只是"带屏幕的标注终端"，A 关机 B 就没法标
- 两台机器需要在**同一局域网**（同一路由器 / 同一 WiFi）；跨网络请用 Tailscale 之类的私有网络
- 系统**没有登录鉴权**，不要直接把 8000 端口暴露到公网（否则任何人都能看摄像头、点抓拍、手动下发舵机指令）
- 数据集构建与训练仍然在 **A 机**上做（图片和标签都在它那儿）

---

## 串口协议

PC → ESP32（一行一个 JSON，`\n` 结尾）：

```json
{"cmd":"sort","class":"battery","slot":1,"angle":90}
{"cmd":"sort","slot":2,"angle":90,"h":30,"v":145}
{"cmd":"ping"}
```

| 字段 | 说明 |
| --- | --- |
| `cmd` | `sort` / `ping` |
| `class` | 识别类别名（仅用于日志，固件不参与决策） |
| `slot` | **决定倾倒方向**（0~3），固件按下标查角度表 |
| `angle` | 兼容字段，**当前双舵机固件不使用**（单舵机机型可用） |
| `h` / `v` | 可选：显式覆盖水平/竖直角度（标定微调用），固件优先使用 |

ESP32 → PC：

```json
{"status":"ready"}
{"status":"done","slot":1}
{"status":"busy","slot":1}
{"status":"error","msg":"slot out of range"}
{"status":"pong"}
```

---

## 配置说明

### `backend/config/settings.yaml`

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `app.host` / `app.port` | `0.0.0.0` / `8000` | 后端监听地址 |
| `app.cors_origins` | 5173 / 4173 | 允许的前端来源（后端托管前端时同源访问，不走 CORS） |
| `app.claim_ttl_seconds` | `120` | 多人协作时"图片认领"的有效期（秒），超时自动释放 |
| `camera.autostart_index` | `0` | 启动时自动打开的摄像头序号，`null` = 不自动打开 |
| `camera.backend` | `auto` | `dshow` / `msmf` / `v4l2` / `any` |
| `camera.stale_frame_seconds` | `6.0` | 超过该秒数没新帧就判定摄像头失效并自动重开 |
| `serial.autoconnect` | `false` | 启动时自动连接串口 |
| `serial.priority_keywords` | CP210x / CH340 / ESP32 … | 自动选口时的优先级关键词 |
| `detection.min_confidence` | `0.35` | 低于该置信度不参与分类 |
| `detection.cooldown_seconds` | `3.0` | 同一物理桶两次下发的冷却（可按桶/按类覆盖） |
| `detection.max_box_area` | `0.6` | 面积超过画面该比例的框直接丢弃（治"托盘被整块误识别"） |
| `detection.multi_object` | `priority` | 多目标策略（见上文） |
| `detection.multi_min_area` | `0.01` | 面积占比 ≥ 该值才参与多目标判定（挡噪点） |
| `detection.stable_frames` | `3` | 连续多少帧选中同一目标才下发（防抖动倒错） |
| `app.watchdog_interval` | `5.0` | 看门狗体检间隔 |

### `backend/config/classes.json`

两层映射表，**扩展类别只需要改这个文件和 `training/data.yaml`，主流程一行都不用动**：

```json
{
  "groups": {
    "1": { "slot": 1, "label_zh": "有害垃圾", "priority": 0, "cooldown": 4.0 }
  },
  "classes": {
    "battery": { "slot": 1, "angle": 90, "label_zh": "电池", "cooldown": null }
  }
}
```

- `groups[].priority` —— 多目标策略 `priority` 用的优先级，数字越小越优先
- `classes` 的**键名必须与 `training/data.yaml` 的 `names` 完全一致**（`train.py` 会校验）

---

## 后端接口

启动后可在 `/docs` 交互式查看。主要接口：

| 分组 | 接口 |
| --- | --- |
| 摄像头 | `GET /api/cameras`、`POST /api/camera/select` |
| 视频流 | `GET /api/video/feed`（MJPEG） |
| 模型 | `GET /api/models`、`POST /api/models/select`、`POST /api/models/upload` |
| 串口 | `GET /api/serial/ports`、`POST /api/serial/connect`、`POST /api/serial/disconnect` |
| 检测 | `POST /api/detection/start`、`POST /api/detection/stop`、`POST /api/detection/multi-object` |
| 手动分拣 | `POST /api/sort/manual` |
| 抓拍 | `POST /api/capture`、`GET /api/captures` |
| 标注 | `GET /api/annotate/images`、`GET /api/annotate/image`、`GET\|POST /api/annotate/labels`、`DELETE /api/annotate/image` |
| 协作 | `POST /api/annotate/claim`、`POST /api/annotate/release`、`GET /api/annotate/claims`、`GET /api/network` |
| 自动标注 | `GET\|POST\|DELETE /api/annotate/reference`、`GET /api/annotate/reference/image`、`POST /api/annotate/auto` |
| 数据增强 | `GET /api/augment/ops`、`POST /api/augment/apply`、`POST /api/augment/clean` |
| 数据集 | `POST /api/annotate/build` |
| WebSocket | `/ws`（命令：`ping` / `state` / `detection.start` / `detection.stop` / `sort.manual` / `camera.select`） |

---

## 模型训练

`training/data.yaml` 是**类别索引的唯一口径**（7 类，索引 0~6），标注页写标签、训练、推理都按它对齐。

```bash
cd training

# 0) 先体检：GPU / 内存 / 数据集体检（推荐第一步就跑）
python train.py --check-gpu
python train.py --dry-run --data data.yaml

# 1) 快速验证（默认 20 轮，几分钟）
python train.py --data data.yaml --model yolov8n.pt --batch 32 --device 0 --workers 0 --cache

# 2) 正式训练
python train.py --data data.yaml --model yolov8n.pt --epochs 100 --imgsz 640 \
    --batch 32 --device 0 --workers 0 --cache
```

常用参数：`--epochs`（轮数）、`--imgsz`、`--batch`、`--device`、`--workers`、`--fraction`（只用一部分数据）、
`--resume`（续训）、`--export`（导出格式）、`--dry-run`（只体检）、`--check-gpu`。

训练脚本会做三件防坑的事：

1. 校验 `data.yaml` 的 `nc` / `names` 自洽、索引连续
2. 校验数据集目录、图片与标签配套、标签索引不越界
3. **校验 `data.yaml` 的类别集合与 `classes.json` 一致** —— 不一致会**直接拒绝**（否则推理时这些类别会全部落到默认桶）

训练完成后把 `runs/trash/weights/best.pt` 复制到 `backend/models/`，前端「模型」面板刷新即可热切换。

---

## 常见问题（避坑）

**1. `OSError: [WinError 1455] 页面文件太小`**

Windows 上每个 dataloader worker 会各自再加载一份 torch + CUDA（约 1.6 GB 提交内存）。
用 `--workers 0`（Windows 默认就是 0），或增大页面文件；脚本启动时会预检提交内存并给出建议。

**2. 后端跑一段时间"突然退出"**

Windows 上两个线程同时操作同一个 `VideoCapture` 会从 OpenCV 原生层崩溃，**Python 的 try/except 拦不住**。
本项目已用**设备锁**把所有原生设备调用（探测/打开/释放/读帧/重开）串行化，并且枚举时**跳过正在使用的设备**。
若仍遇到进程退出，控制台会通过 `faulthandler` 打印崩溃时的 Python 栈，方便定位。

**3. 托盘/桌面被识别成某个类别**

根因通常是训练数据里有"整图标注"（等于告诉模型整个画面就是该类）。
对策：① 训练数据剔除这类标注；② 拍空托盘作为 `background` 背景样本；
③ 托盘换成深色哑光材质；④ 用 `detection.max_box_area` 丢弃过大的框。

**4. 背景差分自动标注出现很多不存在的框**

- 先点「**连拍 8 帧取中值设为参考图**」——单帧参考图带着传感器噪声，差分必然满屏小点
- 把摄像头**自动曝光 / 自动白平衡关掉**（光照整体漂移是背景差分的天敌）
- 界面上调「灵敏度」（实际阈值 = 灵敏度 × 实测噪声 σ），出现多余框就调大

**5. 手动下发时改了 `angle` 没反应**

当前双舵机固件**按 `slot` 查表决定角度**，协议里的 `angle` 只是兼容字段。
要临时改角度请填界面上的 **H / V** 覆盖（对应协议里的 `h` / `v`）。

**6. 同一个桶里的两个类别动作完全一样**

这是设计如此：`paper` 和 `plastic` 都映射到 slot 0，固件只认 `slot`。
换类别只有在**跨桶**时才会看到区别。

**7. MJPEG 预览帧率偏低（约 2~3 fps）**

在部分 USB 摄像头的 dshow 后端上实测如此（与请求的 `CAP_PROP_FPS` 不一致）。
本机预览够用；如需更流畅可降低分辨率或改用 msmf 后端。

---

## 已知限制

- 本机是**单件分拣**结构：一次倾倒只走一个方向，两件不同大类的垃圾必然有一件被分错（见「多目标策略」）
- 背景差分自动标注依赖**机位、托盘、光照固定**；换了位置或光照需要重拍参考图
- 透明物体（玻璃杯）与背景差异小，差分可能框不到，需要人工补标
- 串口协议是"一行一个 JSON"，没有应答重传机制；丢包场景未做可靠性保证
- 未做用户鉴权，只适合本机 / 内网使用

---

## 许可

本项目暂未指定开源许可证。如需开源发布，请自行添加 `LICENSE` 文件。
