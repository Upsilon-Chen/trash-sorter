# 代码结构与运行说明

> 面向"接手/答辩"的简明说明：前后端各自负责什么、一次分拣的数据怎么流、
> 关键文件在哪、常见改动该改哪里。详细接口见 README。

---

## 一、整体结构

```
摄像头 ─┐
        ├─ backend（FastAPI，单进程）
ESP32 ◄─┘        │
                 ├─ /api/**       REST 接口
                 ├─ /ws          WebSocket 实时推送（检测结果、状态、分拣事件）
                 └─ /            托管 frontend/dist（打包后的前端）

MySQL ◄── backend/db（记录：一次下发一行 + 三张图）
浏览器/桌面窗口 ◄── React 前端（3 个页面）
```

技术栈：**Python 3.10 + FastAPI + OpenCV + ultralytics(YOLO) + pyserial + SQLAlchemy/MySQL**
／ **React 18 + TypeScript + Vite + Ant Design**（antd 只做交互行为，外观由手写 CSS 控制）。

---

## 二、后端（`backend/`）

| 文件 | 职责 | 关键点 |
| --- | --- | --- |
| `main.py` | 应用入口：生命周期、路由、WebSocket、静态托管 | 启动时**先绑端口**，模型加载/摄像头/串口放后台 `warm_up()`，所以 0.5 秒可用 |
| `services/camera.py` | 摄像头打开/抓帧/MJPEG 输出 | 设备锁串行化原生调用；枚举带缓存 + 失败索引负缓存；**MJPEG 单流策略** |
| `services/detector.py` | YOLO 模型加载与推理 | ultralytics/torch 延迟导入；返回类别、置信度、bbox |
| `services/pipeline.py` | 检测主循环：取帧→推理→查映射→冷却→下发→推送 | 独立线程；每次下发 `emit({"type":"sort", ...})` |
| `services/serial_esp32.py` | 串口连接与协议 | 一行一个 JSON；**写操作不持状态锁**、取锁带超时、去掉 `flush()` |
| `services/annotator.py` / `augmenter.py` | 标注读写、数据增强 | 旋转/翻转时**同步变换标签坐标** |
| `services/recorder.py` | 分拣记录采集（新增） | 队列+独立线程；抓帧→编码三图→写 MySQL；**零阻塞检测线程** |
| `db/models.py`、`db/session.py` | 表结构与连接（新增） | `sort_records` / `sort_images` / `dataset_images`；DB 挂了自动降级 |
| `records_api.py` | 记录/数据集接口 | 分页、筛选、图片三尺寸、人工判定、回流、CSV、回填、清理、训练集增删改查、**批量划分 `POST /api/dataset/split`** |
| `config/settings.yaml` | 公共配置 | 摄像头/检测/串口参数 |
| `config/settings.local.yaml` | **本机私有**（不入库） | 数据库密码、记录参数 |
| `config/classes.json` | 7 类 → 4 物理桶映射 | `groups` + `priority` + 每类 `cooldown` |

### 一次分拣的数据流

```
camera 抓帧 ──► detector 推理 ──► classes.json 查槽位 ──► 冷却判定
                                                          │
                                          ┌───────────────┴───────────────┐
                                    通过（下发）                    冷却中（跳过）
                                          │
                              serial.send_sort(JSON) ──► ESP32 舵机倾倒
                                          │
                          emit({"type":"sort"}) ──► WS 推送前端
                                          │
                          recorder 抓当前帧 ──► 3 张 JPEG ──► MySQL 一行
```

---

## 三、前端（`frontend/src/`）

| 文件 | 职责 |
| --- | --- |
| `App.tsx` | 骨架：HUD 状态条、侧边菜单、**三个常驻页面**（控制台 / 数据标注 / 记录中心）、WS 事件总线 |
| `api.ts` | 统一请求层 + WebSocket 客户端（心跳、断线重连、命令下发） |
| `theme.tsx`、`theme.css`、`rhine.css` | 主题（深/浅）与全部手工外观样式 |
| `components/CameraPanel.tsx` | 摄像头选择/预览/抓拍 |
| `components/ModelPanel.tsx` | 模型列表/加载/置信度阈值 |
| `components/SerialPanel.tsx` | 串口连接/手动分拣/报文日志 |
| `components/ResultPanel.tsx` | 识别结果、置信度表盘、统计图、下发历史 |
| `components/AnnotatePanel.tsx` | 数据标注（画框、保存、增强、参考图、**按类别筛选列表**） |
| `components/RecordsPanel.tsx` | **记录中心**：列表/筛选/缩略图/详情/判定/回流/统计/**删除** |
| `components/DatasetBrowser.tsx` | **训练集浏览器**：缩略图浏览 + 改类别/划分 + 删除（增删改查） |
| `components/Ambient.tsx`、`DotWave.tsx` | 背景动效（光晕 + canvas 点阵行波） |
| `hooks/`、`Charts.tsx`、`Hud.tsx` | 复用组件 |

**三个页面用 `display` 切换而不是卸载**——切页不重新枚举设备、不丢状态。

---

## 四、关键设计约定（改代码前必读）

1. **不阻塞**：任何 IO（串口/数据库/图片编码/设备探测）都不许在事件循环或持锁期间做；
   一律 `asyncio.to_thread` 或独立线程，且**取锁必须带超时**。
2. **降级优先**：数据库、串口、摄像头任意一个不可用，系统仍要能跑，界面给出可读提示。
3. **前端不缓存 HTML**：`index.html` 加 `no-store` + `Clear-Site-Data`，否则
   WebView2 会一直用旧 bundle（改完看不到变化就是这个原因）。
4. **路由注册位置**：`include_router` 必须紧跟 `app = FastAPI(...)`，晚于
   `app.mount("/", StaticFiles)` 注册的接口会被静态资源吃掉（返回 404）。
5. **接口路径冲突**：`/api/records/{id}` 会抢 `/api/records/xxx.csv`，所以导出走
   `/api/records-export.csv`。
6. **图片存库但拆表**：BLOB 放 `sort_images`，列表查询只查 `sort_records`，缩略图单独接口。

---

## 五、常见改动指引

| 想做 | 改哪里 |
| --- | --- |
| 加一个类别 / 改物理桶映射 | `backend/config/classes.json` |
| 调冷却时间、置信度阈值 | `settings.yaml` 的 `detection`，或 `classes.json` 里每类单独设 |
| 加一个接口 | `backend/main.py`（或 `records_api.py` 里的 router） |
| 加一个页面 | `App.tsx` 菜单 + `page-layer` + 新组件 |
| 改记录字段 | `backend/db/models.py`（建表自动补索引，老库需手动 ALTER） |
| 改数据库连接 | `backend/config/settings.local.yaml`（不入库） |
| 改外观 | `frontend/src/rhine.css`（外观全在这里，别动 antd 主题的语义色） |

---

## 六、启动方式

```powershell
# 一键（推荐）
双击 启动.bat            # 或 垃圾自动分拣系统.exe

# 手动
D:\py310\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
cd frontend; npm run build      # 前端有改动时
D:\py310\python.exe tools\build_exe.py   # 改了 tools/app_launcher.py 才需要重打包
```

**改了后端/前端**：重启 exe 即可，不必重打包（exe 只是启动器）。
**改了 `tools/app_launcher.py` 或解释器路径**：必须重打包。
