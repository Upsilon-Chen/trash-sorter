/**
 * api.ts —— 后端接口与 WebSocket 的统一封装。
 *
 * 覆盖第 1 步约定的全部接口：
 *   GET  /api/cameras            POST /api/camera/select
 *   GET  /api/models             POST /api/models/select     POST /api/models/upload
 *   GET  /api/serial/ports       POST /api/serial/connect    POST /api/serial/disconnect
 *   POST /api/detection/start    POST /api/detection/stop
 *   POST /api/sort/manual        GET  /api/video/feed        WS   /ws
 *
 * 地址策略：默认走 Vite dev 代理（同源），可用 .env.local 覆盖：
 *   VITE_API_BASE=http://192.168.1.20:8000
 *   VITE_WS_URL=ws://192.168.1.20:8000/ws
 */

/* ------------------------------------------------------------------ 基础 */

export const API_BASE: string = String(import.meta.env.VITE_API_BASE ?? '').replace(/\/+$/, '');

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

/** 把后端返回的错误体（FastAPI 的 detail / 本后端的 error）拍平成一句人话。 */
function extractDetail(body: unknown, fallback: string): string {
  if (typeof body === 'string' && body.trim()) return body.trim();
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>;
    const detail = record.detail ?? record.error ?? record.message;
    if (typeof detail === 'string' && detail.trim()) return detail.trim();
    if (Array.isArray(detail)) {
      const parts = detail.map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const entry = item as Record<string, unknown>;
          const loc = Array.isArray(entry.loc) ? entry.loc.join('.') : '';
          const msg = typeof entry.msg === 'string' ? entry.msg : JSON.stringify(entry);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(item);
      });
      if (parts.length) return parts.join('；');
    }
  }
  return fallback;
}

/** 统一的 fetch 包装：JSON 序列化、错误归一化、网络异常兜底。 */
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (typeof init.body === 'string' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch (error) {
    throw new ApiError(`无法连接后端（${API_BASE || '同源代理'}）：${(error as Error).message}`, 0, null);
  }

  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    throw new ApiError(extractDetail(body, `请求失败（HTTP ${response.status}）`), response.status, body);
  }
  return body as T;
}

/** 把任意异常转成可展示的文案。 */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

/* ------------------------------------------------------------------ 类型 */

export interface CameraInfo {
  index: number;
  name: string;
  width: number;
  height: number;
  backend: string;
  fps: number;
}

export interface CameraState {
  opened: boolean;
  camera: CameraInfo | null;
  frame_count: number;
  last_frame_ts: number | null;
  /** 距最近一帧的秒数；看门狗靠它判断摄像头是否失效 */
  frame_age?: number | null;
  grab_thread_alive?: boolean;
  /** 看门狗自动重开摄像头的累计次数 */
  restarts?: number;
  last_restart_ts?: number | null;
  last_error?: string | null;
  stream_clients: number;
  encoding: boolean;
  overlay_attached: boolean;
}

export interface ModelFile {
  name: string;
  size_mb: number;
  modified: string;
  current: boolean;
}

export interface ModelState {
  current: string | null;
  loaded: boolean;
  class_count: number;
  classes: string[];
  device: string | null;
  loaded_at: string | null;
  inference_count: number;
  last_inference_ms: number;
}

/** 串口端口：preferred / likely_esp 表示命中 CP210x、CH340、ESP32 等关键字。 */
export interface SerialPortInfo {
  device: string;
  description: string;
  hwid: string;
  manufacturer: string;
  score: number;
  preferred: boolean;
  likely_esp: boolean;
}

export interface SerialConnection {
  port: string;
  baudrate: number;
  connected_at: string | null;
  ready: boolean;
  last_status: string | null;
}

export interface SerialState {
  connected: boolean;
  connection: SerialConnection | null;
  tx_count: number;
  rx_count: number;
  last_tx: { payload: Record<string, unknown>; ts: number } | null;
  last_rx: Record<string, unknown> | null;
  last_error: string | null;
}

export type BBox = [number, number, number, number];

export interface DetectionResult {
  class_id: number;
  class_name: string;
  confidence: number;
  bbox: BBox;
  serial_sent: boolean;
}

export interface MultiObjectItem {
  class_name: string;
  label_zh: string;
  slot: number;
  group_label_zh: string;
  confidence: number;
  /** 占整幅画面的面积比例 */
  area: number;
}

/** 一帧里同时出现多个不同类别的垃圾时的说明（没有多目标时该字段不存在） */
export interface MultiObjectInfo {
  count: number;
  /** priority / largest / confidence / hold */
  policy: string;
  objects: MultiObjectItem[];
  /** true = 按策略没有下发（hold） */
  held: boolean;
  /** 实际选中的类别（hold 时为 null） */
  chosen: string | null;
  chosen_group_zh?: string;
  reason?: string;
}

export interface DetectionEventData {
  results: DetectionResult[];
  count: number;
  fps: number;
  inference_ms: number;
  model: string | null;
  ts: number;
  multi?: MultiObjectInfo;
}

export interface SortEventData {
  ok: boolean;
  class_name: string;
  slot: number;
  angle: number;
  /** 本次实际下发的 H/V 覆盖值，null 表示用固件里 slot 的默认角度 */
  h?: number | null;
  v?: number | null;
  label_zh: string;
  /** 该识别类别所属物理桶的中文名（6 类识别 -> 4 个桶） */
  group_label_zh: string;
  serial_sent: boolean;
  reason: string;
  manual: boolean;
  ts: number;
  time: string;
}

export interface ClassEntry {
  slot: number;
  angle: number;
  label_zh: string;
  cooldown: number | null;
}

/** 物理槽位（桶）：多个识别类别可以指向同一个 slot */
export interface GroupEntry {
  slot: number;
  label_zh: string;
  cooldown: number;
}

export interface DetectionSnapshot {
  running: boolean;
  frames: number;
  fps: number;
  started_at: string | null;
  results: DetectionResult[];
  /** 识别类别 -> 下发次数 */
  counts: Record<string, number>;
  /** 物理槽位 -> 下发次数 */
  group_counts?: Record<string, number>;
  /** 物理槽位 -> 剩余冷却秒数（冷却按桶算） */
  cooldowns: Record<string, number>;
  last_outcome: SortEventData | null;
  last_error: string | null;
  sort_enabled: boolean;
  target_fps: number;
  min_confidence: number;
  /** 多目标策略：priority / largest / confidence / hold */
  multi_object?: string;
  multi_min_area?: number;
  stable_frames?: number;
  last_multi?: MultiObjectInfo | null;
  classes: Record<string, ClassEntry>;
  /** 物理槽位表，键是槽位号字符串 */
  groups?: Record<string, GroupEntry>;
  camera: CameraState;
  model: ModelState;
  serial: SerialState;
}

export interface BackendState {
  detection: DetectionSnapshot;
  camera: CameraState;
  model: ModelState;
  serial: SerialState;
}

export interface SerialEventData {
  payload?: Record<string, unknown>;
  raw?: string;
  message?: Record<string, unknown>;
}

export interface ServerEvent<T = unknown> {
  type: string;
  data?: T;
}

/** 事件总线里流动的消息：type 决定 data 的具体结构，这里按需在消费端收窄。 */
export interface IncomingEvent {
  type: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data?: any;
}

/* ------------------------------------------------------------ REST 接口 */

export interface CameraListResponse {
  ok: boolean;
  cameras: CameraInfo[];
  current: number | null;
  count: number;
}

export function listCameras(): Promise<CameraListResponse> {
  return request<CameraListResponse>('/api/cameras');
}

export interface CameraSelectResponse {
  ok: boolean;
  camera: CameraInfo;
  state: CameraState;
}

export function selectCamera(index: number): Promise<CameraSelectResponse> {
  return request<CameraSelectResponse>('/api/camera/select', {
    method: 'POST',
    body: JSON.stringify({ index }),
  });
}

export interface ModelListResponse {
  ok: boolean;
  models: ModelFile[];
  current: string | null;
  model_dir: string;
  info: ModelState;
}

export function listModels(): Promise<ModelListResponse> {
  return request<ModelListResponse>('/api/models');
}

export interface ModelSelectResponse {
  ok: boolean;
  model: ModelState;
}

export function selectModel(name: string): Promise<ModelSelectResponse> {
  return request<ModelSelectResponse>('/api/models/select', {
    method: 'POST',
    body: JSON.stringify({ name }),
  });
}

export interface ModelUploadResponse {
  ok: boolean;
  uploaded: ModelFile | null;
  models: ModelFile[];
  size_mb: number;
}

export function uploadModel(file: File, overwrite = false): Promise<ModelUploadResponse> {
  const form = new FormData();
  form.append('file', file, file.name);
  return request<ModelUploadResponse>(`/api/models/upload?overwrite=${overwrite ? 'true' : 'false'}`, {
    method: 'POST',
    body: form,
  });
}

export interface SerialPortsResponse {
  ok: boolean;
  ports: SerialPortInfo[];
  count: number;
  connected: boolean;
  connection: SerialConnection | null;
  state: SerialState;
}

/** 后端返回 preferred，这里补一个更直观的别名字段 likely_esp。 */
export async function listSerialPorts(): Promise<SerialPortsResponse> {
  const data = await request<Omit<SerialPortsResponse, 'ports'> & { ports: (Omit<SerialPortInfo, 'likely_esp'>)[] }>(
    '/api/serial/ports',
  );
  return {
    ...data,
    ports: (data.ports ?? []).map((port) => ({ ...port, likely_esp: Boolean(port.preferred) })),
  };
}

export interface SerialConnectResponse {
  ok: boolean;
  state: SerialState;
  connection: SerialConnection | null;
}

export function connectSerial(port?: string | null, baudrate?: number | null): Promise<SerialConnectResponse> {
  return request<SerialConnectResponse>('/api/serial/connect', {
    method: 'POST',
    body: JSON.stringify({ port: port ?? null, baudrate: baudrate ?? null }),
  });
}

export function disconnectSerial(): Promise<SerialConnectResponse> {
  return request<SerialConnectResponse>('/api/serial/disconnect', { method: 'POST' });
}

export interface DetectionStartPayload {
  /** 开始前先热切换到该模型（可选） */
  model?: string | null;
  reset_stats?: boolean;
  /** 置信度阈值：本次检测生效，覆盖 settings.yaml 的 detection.min_confidence */
  conf?: number;
}

export interface DetectionResponse {
  ok: boolean;
  detection: DetectionSnapshot;
}

export function startDetection(payload: DetectionStartPayload = {}): Promise<DetectionResponse> {
  return request<DetectionResponse>('/api/detection/start', {
    method: 'POST',
    body: JSON.stringify({ model: payload.model ?? null, reset_stats: payload.reset_stats ?? false, conf: payload.conf }),
  });
}

export function stopDetection(): Promise<DetectionResponse> {
  return request<DetectionResponse>('/api/detection/stop', { method: 'POST' });
}

export interface MultiObjectPolicyResponse {
  ok: boolean;
  policy: string;
  /** 是否成功写回 backend/config/settings.yaml（false = 只在本次运行生效） */
  persisted: boolean;
  detection: DetectionSnapshot;
}

/** 切换多目标策略（立即生效并写回配置文件） */
export function setMultiObjectPolicy(policy: string): Promise<MultiObjectPolicyResponse> {
  return request<MultiObjectPolicyResponse>('/api/detection/multi-object', {
    method: 'POST',
    body: JSON.stringify({ policy }),
  });
}

export interface ManualSortPayload {
  class_name?: string;
  slot?: number;
  /** 仅随协议下发做记录，固件不使用（固件按 slot 查表决定角度） */
  angle?: number;
  force?: boolean;
  /** 水平/竖直舵机角度覆盖，固件优先使用这两个字段 */
  h?: number;
  v?: number;
}

export interface ManualSortResponse {
  ok: boolean;
  result: SortEventData;
}

export function manualSort(payload: ManualSortPayload): Promise<ManualSortResponse> {
  return request<ManualSortResponse>('/api/sort/manual', {
    method: 'POST',
    body: JSON.stringify({ force: true, ...payload }),
  });
}

/** MJPEG 预览流地址，直接给 <img src> 使用。 */
export function videoFeedUrl(nonce?: number | string): string {
  return `${API_BASE}/api/video/feed${nonce === undefined ? '' : `?t=${nonce}`}`;
}

/* ------------------------------------------------- 抓拍（采集训练数据） */

export interface CaptureResult {
  ok: boolean;
  class_name: string;
  /** 相对路径，可直接用于标注页选图，如 paper/paper_20261014_193012.jpg */
  name: string;
  file: string;
  /** 该类别已抓拍数量 */
  count: number;
  total: number;
  counts: Record<string, number>;
}

export interface CaptureListResponse {
  ok: boolean;
  base: string;
  counts: Record<string, number>;
  total: number;
}

/** 抓拍一张：存到后端 training/captures/<class_name>/，class_name 传 "background" 表示空托盘 */
export function captureFrame(class_name: string, quality = 95): Promise<CaptureResult> {
  return request<CaptureResult>('/api/capture', {
    method: 'POST',
    body: JSON.stringify({ class_name, quality }),
  });
}

export function listCaptures(): Promise<CaptureListResponse> {
  return request<CaptureListResponse>('/api/captures');
}

/* ------------------------------------------------------------ 数据标注 */

export interface AnnotateClass {
  name: string;
  index: number;
  label_zh: string;
  slot?: number | null;
}

export interface AnnotateImageItem {
  /** 相对路径，如 paper/paper_20261014_193012.jpg */
  name: string;
  category: string;
  labeled: boolean;
  boxes: number;
  /** 被哪个客户端认领着（null/undefined = 没人占）；用于多人协作时避免撞车 */
  claimed_by?: string | null;
}

/** 归一化坐标（0~1），左上角 + 右下角 */
export interface AnnotateBox {
  class_name: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface AnnotateImagesResponse {
  ok: boolean;
  base: string;
  classes: AnnotateClass[];
  images: AnnotateImageItem[];
  total: number;
  labeled: number;
  unlabeled: number;
}

export function listAnnotateImages(): Promise<AnnotateImagesResponse> {
  return request<AnnotateImagesResponse>('/api/annotate/images');
}

/** 抓拍原图地址，直接给 <img src> 用 */
export function annotateImageUrl(name: string): string {
  return `${API_BASE}/api/annotate/image?name=${encodeURIComponent(name)}`;
}

export interface AnnotateLabelsResponse {
  ok: boolean;
  name: string;
  boxes: AnnotateBox[];
  count: number;
}

export function getAnnotateLabels(name: string): Promise<AnnotateLabelsResponse> {
  return request<AnnotateLabelsResponse>(`/api/annotate/labels?name=${encodeURIComponent(name)}`);
}

export interface AnnotateSaveResponse {
  ok: boolean;
  name: string;
  count: number;
  skipped: string[];
  large: number;
  labeled: boolean;
}

export function saveAnnotateLabels(name: string, boxes: AnnotateBox[]): Promise<AnnotateSaveResponse> {
  return request<AnnotateSaveResponse>('/api/annotate/labels', {
    method: 'POST',
    body: JSON.stringify({ name, boxes }),
  });
}

export interface AnnotateDeleteResponse {
  ok: boolean;
  name: string;
  removed_label: boolean;
  remaining: number;
}

/** 删除一张抓拍图及其标签（不可恢复） */
export function deleteAnnotateImage(name: string): Promise<AnnotateDeleteResponse> {
  return request<AnnotateDeleteResponse>(`/api/annotate/image?name=${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
}

/* ------------------------------------------- 多人协作：图片认领 + 共享地址 */

export interface ClaimResponse {
  ok: boolean;
  name: string;
  /** 当前持有者的 client_id（ok=false 时是别人） */
  holder: string;
  /** 该认领还有多少秒过期 */
  expires_in: number;
  /** 服务端的认领有效期 */
  ttl?: number;
}

/** 认领一张图（打开前调用）；被另一个人占用时 ok=false */
export function claimAnnotateImage(name: string, clientId: string): Promise<ClaimResponse> {
  return request<ClaimResponse>('/api/annotate/claim', {
    method: 'POST',
    body: JSON.stringify({ name, client_id: clientId }),
  });
}

export function releaseAnnotateImage(
  name: string,
  clientId: string,
): Promise<{ ok: boolean; name: string; released: boolean }> {
  return request<{ ok: boolean; name: string; released: boolean }>('/api/annotate/release', {
    method: 'POST',
    body: JSON.stringify({ name, client_id: clientId }),
  });
}

export interface NetworkInfo {
  ok: boolean;
  host: string;
  port: number;
  /** 局域网里其他人可以打开的地址（第一个是主网卡，推荐） */
  urls: string[];
  /** 后端是否已在托管前端构建产物（true 时别人直接开网址就能用） */
  frontend_served: boolean;
  claim_ttl_seconds: number;
}

export function getNetworkInfo(): Promise<NetworkInfo> {
  return request<NetworkInfo>('/api/network');
}

export interface BuildDatasetResult {
  output: string;
  yaml: string;
  classes: number;
  train_images: number;
  val_images: number;
  boxes: number;
  backgrounds: number;
  large_boxes: number;
}

export function buildAnnotateDataset(
  val_ratio = 0.2,
  include_background = true,
): Promise<{ ok: boolean; dataset: BuildDatasetResult }> {
  return request<{ ok: boolean; dataset: BuildDatasetResult }>('/api/annotate/build', {
    method: 'POST',
    body: JSON.stringify({ val_ratio, include_background }),
  });
}

/* --------------------------------------------- 背景差分自动标注 */

export interface ReferenceInfo {
  name: string;
  size: [number, number] | null;
  mtime: number;
}

export interface ReferenceResponse {
  ok: boolean;
  reference: ReferenceInfo | null;
  url?: string | null;
}

export function getReference(): Promise<ReferenceResponse> {
  return request<ReferenceResponse>('/api/annotate/reference');
}

/** 设置参考图：传 name 用已有抓拍图，不传则抓当前摄像头画面（frames 帧取中值） */
export function setReference(name?: string, frames?: number): Promise<ReferenceResponse> {
  return request<ReferenceResponse>('/api/annotate/reference', {
    method: 'POST',
    body: JSON.stringify({ ...(name ? { name } : {}), ...(frames ? { frames } : {}) }),
  });
}

export function clearReference(): Promise<{ ok: boolean; removed: boolean }> {
  return request<{ ok: boolean; removed: boolean }>('/api/annotate/reference', { method: 'DELETE' });
}

export function referenceImageUrl(): string {
  return `${API_BASE}/api/annotate/reference/image`;
}

export interface AutoAnnotateOptions {
  name?: string;
  all?: boolean;
  class_name?: string;
  /** 噪声下限（绝对灰度差） */
  threshold?: number;
  /** 自适应灵敏度：实际阈值 = k × 实测噪声σ，越大越严格 */
  sigma_k?: number;
  /** 轮廓实心度下限 */
  min_fill?: number;
  /** 差分前高斯模糊核 */
  blur?: number;
  min_area?: number;
  max_area?: number;
  dilate?: number;
  padding?: number;
  only_unlabeled?: boolean;
  overwrite?: boolean;
}

export interface AutoAnnotateInfo {
  method: string;
  reference: string;
  threshold: number;
  /** 实际生效的阈值 = max(threshold, sigma_k × noise_sigma) */
  effective_threshold: number;
  /** 实测噪声水平（MAD 估计） */
  noise_sigma: number;
  sigma_k: number;
  min_area: number;
  min_fill: number;
  /** 差异区域占整图比例，过高说明参考图和当前画面对不上 */
  coverage: number;
  contours: number;
  regions: number;
  /** 被三关滤掉的候选数：面积 / 实心度 / 证据强度 */
  rejected?: { area: number; fill: number; evidence: number };
  elapsed_ms: number;
}

export interface AutoAnnotateSummary {
  total: number;
  written: number;
  skipped: number;
  failed: number;
}

export interface AutoAnnotateResultRow {
  name: string;
  status: 'ok' | 'skipped' | 'failed' | 'empty';
  reason?: string;
  boxes?: number;
  coverage?: number;
}

export interface AutoAnnotateResponse {
  ok: boolean;
  name?: string;
  /** all=false 时返回的建议框（未写盘） */
  boxes?: AnnotateBox[];
  info?: AutoAnnotateInfo;
  /** all=true 时的汇总 */
  summary?: AutoAnnotateSummary;
  results?: AutoAnnotateResultRow[];
}

export function autoAnnotate(payload: AutoAnnotateOptions): Promise<AutoAnnotateResponse> {
  return request<AutoAnnotateResponse>('/api/annotate/auto', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/* --------------------------------------------------------- WebSocket */

export type SocketStatus = 'idle' | 'connecting' | 'open' | 'closed';

/** 未显式配置 VITE_WS_URL 时，按 API_BASE / 当前页面地址推导。 */
export function defaultSocketUrl(): string {
  const explicit = import.meta.env.VITE_WS_URL;
  if (explicit) return explicit;

  const base = API_BASE || window.location.origin;
  const url = new URL(base, window.location.href);
  const protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${url.host}/ws`;
}

type EventHandler = (event: IncomingEvent) => void;
type StatusHandler = (status: SocketStatus) => void;

/**
 * 带自动重连与心跳的 WebSocket 客户端。
 * 后端会推送 hello / state / detection / sort / camera_state / model_state /
 * serial_* / ack / error / pong 等消息，详见 README。
 */
export class TrashSocket {
  readonly url: string;

  private socket: WebSocket | null = null;
  private readonly eventHandlers = new Set<EventHandler>();
  private readonly statusHandlers = new Set<StatusHandler>();
  private reconnectAttempts = 0;
  private reconnectTimer: number | null = null;
  private heartbeatTimer: number | null = null;
  private manuallyClosed = false;
  private status: SocketStatus = 'idle';

  constructor(url: string = defaultSocketUrl()) {
    this.url = url;
  }

  getStatus(): SocketStatus {
    return this.status;
  }

  /** 订阅后端推送；返回取消订阅函数。 */
  onEvent(handler: EventHandler): () => void {
    this.eventHandlers.add(handler);
    return () => {
      this.eventHandlers.delete(handler);
    };
  }

  /** 订阅连接状态；会立即回调一次当前状态。 */
  onStatus(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler);
    handler(this.status);
    return () => {
      this.statusHandlers.delete(handler);
    };
  }

  connect(): void {
    if (
      this.socket &&
      (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    this.manuallyClosed = false;
    this.setStatus('connecting');

    let socket: WebSocket;
    try {
      socket = new WebSocket(this.url);
    } catch {
      this.setStatus('closed');
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.reconnectAttempts = 0;
      this.setStatus('open');
      this.startHeartbeat();
      this.send('state');
    };
    socket.onmessage = (message: MessageEvent<string>) => {
      let parsed: IncomingEvent;
      try {
        parsed = JSON.parse(message.data) as IncomingEvent;
      } catch {
        return;
      }
      this.eventHandlers.forEach((handler) => {
        try {
          handler(parsed);
        } catch (error) {
          console.error('[ws] 事件处理失败', error);
        }
      });
    };
    socket.onerror = () => {
      /* onclose 会负责重连 */
    };
    socket.onclose = () => {
      this.stopHeartbeat();
      this.socket = null;
      this.setStatus('closed');
      if (!this.manuallyClosed) this.scheduleReconnect();
    };
  }

  close(): void {
    this.manuallyClosed = true;
    this.stopHeartbeat();
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.socket?.close();
    this.socket = null;
    this.setStatus('closed');
  }

  /** 发送控制指令：{cmd, data}。 */
  send(cmd: string, data?: Record<string, unknown>): boolean {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
    try {
      this.socket.send(JSON.stringify({ cmd, data }));
      return true;
    } catch {
      return false;
    }
  }

  private setStatus(status: SocketStatus): void {
    this.status = status;
    this.statusHandlers.forEach((handler) => handler(status));
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = window.setInterval(() => this.send('ping'), 20000);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      window.clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    const delay = Math.min(500 * 2 ** this.reconnectAttempts, 8000);
    this.reconnectAttempts += 1;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }
}

/** 全局单例：整个应用共用一条 WS 连接。 */
export const trashSocket = new TrashSocket();

/** 请求后端回推一次完整状态（等价于 WS 指令 state）。 */
export function requestState(): boolean {
  return trashSocket.send('state');
}
