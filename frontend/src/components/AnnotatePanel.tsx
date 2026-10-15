import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  App as AntApp,
  Alert,
  Button,
  Card,
  Col,
  Empty,
  InputNumber,
  List,
  Row,
  Segmented,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
} from 'antd';
import {
  CameraOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  PictureOutlined,
  EyeInvisibleOutlined,
  ReloadOutlined,
  SaveOutlined,
  StepBackwardOutlined,
  StepForwardOutlined,
  TagsOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';

import {
  annotateImageUrl,
  API_BASE,
  applyAugment,
  autoAnnotate,
  buildAnnotateDataset,
  captureFrame,
  claimAnnotateImage,
  cleanAugment,
  clearReference,
  deleteAnnotateImage,
  errorText,
  getAnnotateLabels,
  getReference,
  listAnnotateImages,
  listAugmentOps,
  referenceImageUrl,
  releaseAnnotateImage,
  saveAnnotateLabels,
  setReference,
  type AnnotateBox,
  type AnnotateClass,
  type AnnotateImageItem,
  type AugmentOp,
  type AutoAnnotateInfo,
  type CameraState,
  type ReferenceInfo,
} from '../api';

import CameraPreview from './CameraPreview';

type Filter = 'all' | 'unlabeled' | 'labeled';

/** 画框时的归一化矩形（0~1） */
interface DraftBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/** 8 个缩放控制点 */
type HandleType = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w';
const HANDLES: HandleType[] = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];

/** 当前鼠标操作：画新框 / 拖动已有框 / 拖控制点缩放 */
interface Interaction {
  mode: 'draw' | 'move' | 'resize';
  index: number;
  handle?: HandleType;
  startX: number;
  startY: number;
  origin: AnnotateBox;
}

const clamp01 = (value: number) => Math.min(Math.max(value, 0), 1);
const MIN_BOX = 0.01;

/** 列表自动刷新的可选间隔（秒）；0 = 关闭 */
const AUTO_REFRESH_OPTIONS = [
  { value: 0, label: '关闭' },
  { value: 5, label: '5 秒' },
  { value: 10, label: '10 秒' },
  { value: 30, label: '30 秒' },
  { value: 60, label: '1 分钟' },
];
const AUTO_REFRESH_VALUES = AUTO_REFRESH_OPTIONS.map((item) => item.value);
const AUTO_REFRESH_KEY = 'trash-sorter-autorefresh';

/** 平移：保持宽高不变，且不越出画面 */
function shiftBox(box: AnnotateBox, dx: number, dy: number): AnnotateBox {
  const width = box.x2 - box.x1;
  const height = box.y2 - box.y1;
  const x1 = Math.min(Math.max(box.x1 + dx, 0), 1 - width);
  const y1 = Math.min(Math.max(box.y1 + dy, 0), 1 - height);
  return { ...box, x1, y1, x2: x1 + width, y2: y1 + height };
}

/** 按控制点缩放：只动被拖的那条边/角 */
function resizeBox(box: AnnotateBox, handle: HandleType, px: number, py: number): AnnotateBox {
  let { x1, y1, x2, y2 } = box;
  if (handle.includes('w')) x1 = Math.min(clamp01(px), x2 - MIN_BOX);
  if (handle.includes('e')) x2 = Math.max(clamp01(px), x1 + MIN_BOX);
  if (handle.includes('n')) y1 = Math.min(clamp01(py), y2 - MIN_BOX);
  if (handle.includes('s')) y2 = Math.max(clamp01(py), y1 + MIN_BOX);
  return { ...box, x1, y1, x2, y2 };
}

const BOX_COLORS = ['#12b76a', '#2e90fa', '#f79009', '#f04438', '#7a5af8', '#0e9384', '#ee46bc', '#6172f3'];
// 小于该尺寸（归一化）的拖动视为误点，不生成框
const MIN_DRAW = 0.008;

export interface AnnotatePanelProps {
  cameraState: CameraState | null;
  onCameraState: (state: CameraState) => void;
  /** 切换摄像头后让 App 重新拉一次全量状态 */
  onStateRefresh: () => void;
}

/**
 * AnnotatePanel：给抓拍的图片画框标注，并一键导出成 YOLO 训练集。
 *
 * 坐标前端用归一化 0~1 存，后端写 YOLO 标签时再乘类别索引，
 * 与 training/data.yaml 的 names 顺序保持一致。
 */
function AnnotatePanel({
  cameraState,
  onCameraState,
  onStateRefresh,
}: AnnotatePanelProps) {
  const { message, modal } = AntApp.useApp();

  const [classes, setClasses] = useState<AnnotateClass[]>([]);
  const [images, setImages] = useState<AnnotateImageItem[]>([]);
  const [base, setBase] = useState('');
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<Filter>('all');

  const [current, setCurrent] = useState<string | null>(null);
  const [boxes, setBoxes] = useState<AnnotateBox[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [draft, setDraft] = useState<DraftBox | null>(null);
  const [newClass, setNewClass] = useState<string>('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [building, setBuilding] = useState(false);
  const [valRatio, setValRatio] = useState(0.2);
  const [withBackground, setWithBackground] = useState(true);
  const [imgNonce, setImgNonce] = useState(0);
  // 保存后自动跳到下一张未标注的图
  const [autoNext, setAutoNext] = useState(true);
  // 背景差分自动标注
  const [reference, setReferenceState] = useState<ReferenceInfo | null>(null);
  const [refNonce, setRefNonce] = useState(() => Date.now());
  // 灵敏度 = 阈值倍数 k（实际阈值 = k × 实测噪声σ）。越大越严格，越不容易出现多余的框。
  const [autoSigma, setAutoSigma] = useState(8);
  const [autoMinArea, setAutoMinArea] = useState(0.5); // 界面单位是百分比
  const [autoBusy, setAutoBusy] = useState(false);
  const [autoInfo, setAutoInfo] = useState<AutoAnnotateInfo | null>(null);
  // 在标注页直接抓拍
  const [captureClass, setCaptureClass] = useState('background');
  const [capturing, setCapturing] = useState(false);
  // 抓拍列表自动刷新：间隔（秒，0=关闭）、设置条是否显示、用于倒计时的秒表
  const [autoRefresh, setAutoRefresh] = useState<number>(() => {
    const saved = Number(window.localStorage.getItem(AUTO_REFRESH_KEY) ?? 0);
    return AUTO_REFRESH_VALUES.includes(saved) ? saved : 0;
  });
  const [autoRefreshBar, setAutoRefreshBar] = useState(true);
  const [autoTick, setAutoTick] = useState(0);
  // 数据增强：选的类别（空=全部）、选的变换、只处理已标注、是否含背景图
  const [augClasses, setAugClasses] = useState<string[]>([]);
  const [augOps, setAugOps] = useState<string[]>(['rot90']);
  const [augOpList, setAugOpList] = useState<AugmentOp[]>([]);
  const [augOnlyLabeled, setAugOnlyLabeled] = useState(true);
  const [augBackground, setAugBackground] = useState(false);
  const [augBusy, setAugBusy] = useState(false);
  // 列表一次最多渲染多少条：1974 张全渲染会产生约 2 万个 DOM 节点，页面会明显卡顿
  const [listLimit, setListLimit] = useState(80);

  const stageRef = useRef<HTMLDivElement | null>(null);
  const interactionRef = useRef<Interaction | null>(null);
  const draftRef = useRef<DraftBox | null>(null);
  // 当前打开的图片（用 ref 是为了在异步回调 / 定时器里拿到最新值）
  const currentRef = useRef<string | null>(null);

  /**
   * 本浏览器标签页的协作 ID：只用来区分"谁在标哪张"，不涉及账号。
   * 放 sessionStorage —— 同一个浏览器开两个标签页会被当成两个人（方便自己测协作）。
   */
  const clientId = useMemo(() => {
    const key = 'trash-sorter-client-id';
    let value = window.sessionStorage.getItem(key);
    if (!value) {
      value = Math.random().toString(36).slice(2, 10);
      window.sessionStorage.setItem(key, value);
    }
    return value;
  }, []);

  /** 这张图是否被"另一个人"占着 */
  const isClaimedByOther = useCallback(
    (item: AnnotateImageItem) => Boolean(item.claimed_by && item.claimed_by !== clientId),
    [clientId],
  );

  const colorOf = useCallback(
    (className: string) => {
      const index = classes.findIndex((item) => item.name === className);
      return BOX_COLORS[(index < 0 ? 0 : index) % BOX_COLORS.length];
    },
    [classes],
  );

  const labelOf = useCallback(
    (className: string) => classes.find((item) => item.name === className)?.label_zh ?? className,
    [classes],
  );

  /** 抓拍类别下拉：background（空托盘/背景）+ 7 个识别类别 */
  const captureOptions = useMemo(
    () => [
      { value: 'background', label: 'background（空托盘 / 背景）' },
      ...classes.map((item) => ({ value: item.name, label: `${item.label_zh}（${item.name}）` })),
    ],
    [classes],
  );

  // ------------------------------------------------------------ 列表/加载
  const refresh = useCallback(
    async (silent = false) => {
      setLoading(true);
      try {
        const data = await listAnnotateImages();
        setImages(data.images ?? []);
        setClasses(data.classes ?? []);
        setBase(data.base);
        setNewClass((previous) => previous || data.classes?.[0]?.name || '');
        if (!silent) {
          message.success(`共 ${data.total} 张，已标注 ${data.labeled} 张`);
        }
      } catch (error) {
        message.error(errorText(error));
      } finally {
        setLoading(false);
      }
    },
    [message],
  );

  useEffect(() => {
    void refresh(true);
  }, [refresh]);

  // 自动刷新：1 秒一跳的秒表，每满一个间隔就静默重扫一次列表
  useEffect(() => {
    if (!autoRefresh) {
      setAutoTick(0);
      return;
    }
    const timer = window.setInterval(() => setAutoTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [autoRefresh]);

  useEffect(() => {
    if (!autoRefresh || autoTick === 0) return;
    if (autoTick % autoRefresh !== 0) return;
    // 正在保存/抓拍/自动标注时不打扰，等下一个周期
    if (saving || capturing || autoBusy) return;
    void refresh(true);
  }, [autoBusy, autoRefresh, autoTick, capturing, refresh, saving]);

  // 记住选择（下次打开还是这个间隔）
  useEffect(() => {
    window.localStorage.setItem(AUTO_REFRESH_KEY, String(autoRefresh));
  }, [autoRefresh]);

  const autoSecondsLeft = autoRefresh ? autoRefresh - (autoTick % autoRefresh) : 0;

  // 增强方式清单来自后端（augmenter.py 里的表），加一种变换不用改前端
  useEffect(() => {
    listAugmentOps()
      .then((data) => setAugOpList(data.ops ?? []))
      .catch(() => undefined);
  }, []);

  /** 批量增强会处理多少张（与后端同一套过滤规则，仅用于界面预估） */
  const augEstimate = useMemo(() => {
    const suffixes = augOpList.map((item) => item.suffix);
    const sources = images.filter((item) => {
      if (augClasses.length > 0 && !augClasses.includes(item.category)) return false;
      if (item.category === 'background' && !augBackground) return false;
      if (augOnlyLabeled && !item.labeled) return false;
      const stem = item.name.replace(/\.[^.]+$/, '');
      // 不对增强产物再增强（否则 r90_r90… 组合爆炸）
      if (suffixes.some((suffix) => stem.endsWith(suffix))) return false;
      return true;
    }).length;
    return { sources, total: sources * augOps.length };
  }, [augBackground, augClasses, augOnlyLabeled, augOpList, augOps.length, images]);

  const opLabel = useCallback(
    (key: string) => augOpList.find((item) => item.key === key)?.label_zh ?? key,
    [augOpList],
  );

  /** 静默刷新列表并返回最新数据（保存后用来找下一张未标注） */
  const refreshImages = useCallback(async (): Promise<AnnotateImageItem[]> => {
    try {
      const data = await listAnnotateImages();
      setImages(data.images ?? []);
      return data.images ?? [];
    } catch {
      return [];
    }
  }, []);

  const loadImage = useCallback(
    async (name: string): Promise<boolean> => {
      // 先认领：两个标注员同时点同一张时，后点的会被挡住
      try {
        const claim = await claimAnnotateImage(name, clientId);
        if (!claim.ok) {
          message.warning(
            `这张图正在被另一位标注员处理（约 ${Math.round(claim.expires_in)} 秒后自动释放），先标别的吧`,
          );
          void refreshImages();
          return false;
        }
      } catch (error) {
        message.error(errorText(error));
        return false;
      }

      // 换图时释放上一张
      const previous = currentRef.current;
      if (previous && previous !== name) {
        void releaseAnnotateImage(previous, clientId).catch(() => undefined);
      }

      try {
        const data = await getAnnotateLabels(name);
        currentRef.current = name;
        setCurrent(name);
        setBoxes(data.boxes ?? []);
        setSelected(null);
        setDirty(false);
        setImgNonce((value) => value + 1);
        // 抓拍时按类别分了目录：新图默认就用该目录对应的类别，省一次点击
        if ((data.boxes ?? []).length === 0) {
          const category = name.split('/')[0];
          if (classes.some((item) => item.name === category)) setNewClass(category);
        }
        return true;
      } catch (error) {
        message.error(errorText(error));
        return false;
      }
    },
    [classes, clientId, message, refreshImages],
  );

  // 打开期间定期续租，浏览器异常关闭时靠服务端 TTL 自动释放
  useEffect(() => {
    if (!current) return;
    const timer = window.setInterval(() => {
      void claimAnnotateImage(current, clientId).catch(() => undefined);
    }, 45000);
    return () => window.clearInterval(timer);
  }, [clientId, current]);

  // 离开标注页 / 关闭页面时尽量释放认领（同源时 sendBeacon 能发出去；失败也有 TTL 兜底）
  useEffect(() => {
    const release = () => {
      const name = currentRef.current;
      if (!name) return;
      currentRef.current = null;
      try {
        const blob = new Blob([JSON.stringify({ name, client_id: clientId })], {
          type: 'application/json',
        });
        navigator.sendBeacon(`${API_BASE}/api/annotate/release`, blob);
      } catch {
        /* 忽略：TTL 会兜底 */
      }
    };
    window.addEventListener('pagehide', release);
    return () => {
      window.removeEventListener('pagehide', release);
      release();
    };
  }, [clientId]);

  const filtered = useMemo(() => {
    if (filter === 'labeled') return images.filter((item) => item.labeled);
    if (filter === 'unlabeled') return images.filter((item) => !item.labeled);
    return images;
  }, [filter, images]);

  const index = useMemo(
    () => (current ? filtered.findIndex((item) => item.name === current) : -1),
    [current, filtered],
  );

  // ------------------------------------------------------------ 保存
  const save = useCallback(
    async (silent = false) => {
      if (!current) return true;
      setSaving(true);
      try {
        const data = await saveAnnotateLabels(current, boxes);
        setDirty(false);
        if (data.skipped?.length) {
          message.warning(`保存 ${data.count} 个框，忽略 ${data.skipped.length} 个（${data.skipped[0]}）`);
        } else if (!silent) {
          message.success(data.count ? `已保存 ${data.count} 个框` : '已清空该图标注');
        }
        if (data.large > 0) {
          message.warning(
            `有 ${data.large} 个框面积超过画面 60%，实机推理时会被 max_box_area 过滤掉，建议画紧一点`,
            6,
          );
        }
        await refresh(true);
        return true;
      } catch (error) {
        message.error(errorText(error));
        return false;
      } finally {
        setSaving(false);
      }
    },
    [boxes, current, message, refresh],
  );

  /** 保存并自动跳到下一张未标注的图（跳过被另一位标注员占用的） */
  const saveAndNext = useCallback(async () => {
    const ok = await save();
    if (!ok || !autoNext) return;
    const fresh = await refreshImages();
    const next = fresh.find(
      (item) => !item.labeled && item.name !== current && !isClaimedByOther(item),
    );
    if (next) {
      if (current) void releaseAnnotateImage(current, clientId).catch(() => undefined);
      await loadImage(next.name);
    } else if (fresh.some((item) => !item.labeled)) {
      message.info('剩下的未标注图片都被另一位标注员占用了');
    } else if (fresh.length > 0) {
      message.success('所有图片都标注完了');
    }
  }, [autoNext, clientId, current, isClaimedByOther, loadImage, message, refreshImages, save]);

  const goto = useCallback(
    async (offset: number) => {
      if (filtered.length === 0) return;
      const nextIndex = index < 0 ? 0 : (index + offset + filtered.length) % filtered.length;
      if (dirty && current) await save(true); // 切图自动保存，避免丢标注
      await loadImage(filtered[nextIndex].name);
    },
    [current, dirty, filtered, index, loadImage, save],
  );

  // ------------------------------------------------- 画框 / 拖动 / 缩放
  const toNormalized = useCallback((clientX: number, clientY: number): DraftBox | null => {
    const stage = stageRef.current;
    if (!stage) return null;
    const rect = stage.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    const x = clamp01((clientX - rect.left) / rect.width);
    const y = clamp01((clientY - rect.top) / rect.height);
    return { x1: x, y1: y, x2: x, y2: y };
  }, []);

  const updateDraft = useCallback((next: DraftBox | null) => {
    draftRef.current = next;
    setDraft(next);
  }, []);

  const commitDraft = useCallback(() => {
    const box = draftRef.current;
    updateDraft(null);
    if (!box) return;
    const x1 = Math.min(box.x1, box.x2);
    const y1 = Math.min(box.y1, box.y2);
    const x2 = Math.max(box.x1, box.x2);
    const y2 = Math.max(box.y1, box.y2);
    if (x2 - x1 < MIN_DRAW || y2 - y1 < MIN_DRAW) return;
    const className = newClass || classes[0]?.name;
    if (!className) {
      message.warning('还没有可用类别');
      return;
    }
    setBoxes((previous) => [...previous, { class_name: className, x1, y1, x2, y2 }]);
    setSelected(boxes.length);
    setDirty(true);
  }, [boxes.length, classes, message, newClass, updateDraft]);

  useEffect(() => {
    const onMove = (event: MouseEvent) => {
      const inter = interactionRef.current;
      if (!inter) return;
      const point = toNormalized(event.clientX, event.clientY);
      if (!point) return;
      const px = point.x2;
      const py = point.y2;
      if (inter.mode === 'draw') {
        updateDraft({ x1: inter.startX, y1: inter.startY, x2: px, y2: py });
        return;
      }
      setBoxes((previous) =>
        previous.map((item, i) => {
          if (i !== inter.index) return item;
          if (inter.mode === 'move') {
            return shiftBox(inter.origin, px - inter.startX, py - inter.startY);
          }
          return resizeBox(inter.origin, inter.handle ?? 'se', px, py);
        }),
      );
    };
    const onUp = () => {
      const inter = interactionRef.current;
      if (!inter) return;
      interactionRef.current = null;
      if (inter.mode === 'draw') commitDraft();
      else setDirty(true);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [commitDraft, toNormalized, updateDraft]);

  const removeBox = useCallback((boxIndex: number) => {
    setBoxes((previous) => previous.filter((_item, i) => i !== boxIndex));
    setSelected(null);
    setDirty(true);
  }, []);

  /** 空白处按下：开始画新框 */
  const startDraw = useCallback(
    (event: React.MouseEvent) => {
      if (!current || event.button !== 0) return;
      const point = toNormalized(event.clientX, event.clientY);
      if (!point) return;
      interactionRef.current = {
        mode: 'draw',
        index: -1,
        startX: point.x1,
        startY: point.y1,
        origin: { class_name: newClass, ...point },
      };
      setSelected(null);
      updateDraft(point);
    },
    [current, newClass, toNormalized, updateDraft],
  );

  /** 框内按下：选中并准备拖动 */
  const startMove = useCallback(
    (event: React.MouseEvent, boxIndex: number) => {
      if (event.button !== 0) return;
      event.stopPropagation();
      const point = toNormalized(event.clientX, event.clientY);
      if (!point) return;
      setSelected(boxIndex);
      interactionRef.current = {
        mode: 'move',
        index: boxIndex,
        startX: point.x1,
        startY: point.y1,
        origin: { ...boxes[boxIndex] },
      };
    },
    [boxes, toNormalized],
  );

  /** 控制点上按下：准备缩放 */
  const startResize = useCallback(
    (event: React.MouseEvent, boxIndex: number, handle: HandleType) => {
      if (event.button !== 0) return;
      event.stopPropagation();
      const point = toNormalized(event.clientX, event.clientY);
      if (!point) return;
      setSelected(boxIndex);
      interactionRef.current = {
        mode: 'resize',
        index: boxIndex,
        handle,
        startX: point.x1,
        startY: point.y1,
        origin: { ...boxes[boxIndex] },
      };
    },
    [boxes, toNormalized],
  );

  /** 设定类别：作用于"新框默认类别"，同时如果有选中的框也一起改 */
  const applyClass = useCallback(
    (value: string) => {
      setNewClass(value);
      if (selected !== null) {
        setBoxes((previous) =>
          previous.map((item, i) => (i === selected ? { ...item, class_name: value } : item)),
        );
        setDirty(true);
      }
    },
    [selected],
  );

  // 快捷键：Delete 删框，左右方向键切图，数字键 1~9 选类别
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && ['INPUT', 'TEXTAREA'].includes(target.tagName)) return;
      const digit = Number(event.key);
      if (Number.isInteger(digit) && digit >= 1 && digit <= 9 && classes[digit - 1]) {
        event.preventDefault();
        applyClass(classes[digit - 1].name);
        return;
      }
      if ((event.key === 'Delete' || event.key === 'Backspace') && selected !== null) {
        event.preventDefault();
        removeBox(selected);
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        void goto(1);
      } else if (event.key === 'ArrowLeft') {
        event.preventDefault();
        void goto(-1);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [applyClass, classes, goto, removeBox, selected]);

  // ------------------------------------------------------------ 生成数据集
  const build = useCallback(() => {
    modal.confirm({
      title: '生成训练数据集',
      content: `把已标注的图片导出成 YOLO 数据集（验证集比例 ${Math.round(valRatio * 100)}%）？${
        withBackground ? '背景图会作为空标签样本一并加入。' : ''
      }未保存的标注会先保存。`,
      okText: '生成',
      cancelText: '取消',
      onOk: async () => {
        if (dirty) await save(true);
        setBuilding(true);
        try {
          const data = await buildAnnotateDataset(valRatio, withBackground);
          const info = data.dataset;
          modal.success({
            title: '数据集已生成',
            content: (
              <div style={{ fontSize: 13, lineHeight: 1.8 }}>
                <div>
                  训练集 <b>{info.train_images}</b> 张 / 验证集 <b>{info.val_images}</b> 张
                  （背景图 {info.backgrounds} 张）
                </div>
                <div>
                  标注框 <b>{info.boxes}</b> 个，类别 {info.classes} 个
                </div>
                {info.large_boxes > 0 && (
                  <div style={{ color: '#b42318' }}>
                    ⚠ 其中 {info.large_boxes} 个框面积超过画面 60%，实机会被 max_box_area 过滤
                  </div>
                )}
                <div className="mono" style={{ marginTop: 6 }}>
                  配置文件：{info.yaml}
                </div>
                <div style={{ marginTop: 8, color: '#475467' }}>
                  下一步在 training 目录执行：
                  <br />
                  <span className="mono">
                    python train.py --data data-captures.yaml --model yolov8n.pt --batch 32 --device 0
                    --workers 0 --cache
                  </span>
                </div>
              </div>
            ),
          });
          await refresh(true);
        } catch (error) {
          message.error(errorText(error));
        } finally {
          setBuilding(false);
        }
      },
    });
  }, [dirty, message, modal, refresh, save, valRatio, withBackground]);

  // ------------------------------------------------------ 背景差分自动标注
  const loadReference = useCallback(async () => {
    try {
      const data = await getReference();
      setReferenceState(data.reference ?? null);
    } catch {
      /* 参考图状态不影响主流程 */
    }
  }, []);

  useEffect(() => {
    void loadReference();
  }, [loadReference]);

  const handleSetReference = useCallback(
    async (useSelected: boolean, frames = 8) => {
      try {
        const data = useSelected
          ? await setReference(current ?? undefined)
          : await setReference(undefined, frames);
        setReferenceState(data.reference ?? null);
        setRefNonce(Date.now());
        message.success(
          useSelected
            ? '已用选中的抓拍图作为参考图'
            : frames > 1
              ? `已连拍 ${frames} 帧取中值合成参考图（噪声更小，差分更稳）`
              : '已把当前摄像头画面设为参考图',
        );
      } catch (error) {
        message.error(errorText(error));
      }
    },
    [current, message],
  );

  const handleClearReference = useCallback(async () => {
    try {
      await clearReference();
      setReferenceState(null);
      message.success('参考图已清除');
    } catch (error) {
      message.error(errorText(error));
    }
  }, [message]);

  /** 在标注页直接抓拍：抓完自动选中新图，可以马上标注 */
  const handleCapture = useCallback(async () => {
    setCapturing(true);
    try {
      const data = await captureFrame(captureClass);
      await refresh(true);
      if (data.name) await loadImage(data.name);
      message.success(`已抓拍 → ${data.name || captureClass}`);
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setCapturing(false);
    }
  }, [captureClass, loadImage, message, refresh]);

  /** 删除一张抓拍图（连同它的标签），删完自动跳到下一张 */
  const handleDeleteImage = useCallback(
    (name: string) => {
      modal.confirm({
        title: '删除这张图片？',
        content: (
          <div style={{ fontSize: 13, lineHeight: 1.7 }}>
            <div className="mono">{name}</div>
            <div style={{ color: '#b42318' }}>图片和它的标注文件都会被删除，不可恢复。</div>
          </div>
        ),
        okText: '删除',
        okButtonProps: { danger: true },
        cancelText: '取消',
        onOk: async () => {
          try {
            await deleteAnnotateImage(name);
            const fresh = await refreshImages();
            message.success('已删除');
            if (current === name) {
              const next = fresh.find((item) => item.name !== name);
              if (next) {
                await loadImage(next.name);
              } else {
                setCurrent(null);
                setBoxes([]);
                setSelected(null);
                setDirty(false);
              }
            }
          } catch (error) {
            message.error(errorText(error));
          }
        },
      });
    },
    [current, loadImage, message, modal, refreshImages],
  );

  const handleAutoOne = useCallback(async () => {
    if (!current) return;
    setAutoBusy(true);
    try {
      const data = await autoAnnotate({
        name: current,
        class_name: newClass || undefined,
        sigma_k: autoSigma,
        min_area: autoMinArea / 100,
      });
      setAutoInfo(data.info ?? null);
      const found = data.boxes ?? [];
      if (found.length === 0) {
        message.warning('没找到明显物体：可以调低灵敏度，或把最小面积调小一点');
        return;
      }
      setBoxes(found);
      setSelected(null);
      setDirty(true);
      message.success(`找到 ${found.length} 个区域，已填入待确认——看过没问题点「保存」`);
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setAutoBusy(false);
    }
  }, [autoMinArea, autoSigma, current, message, newClass]);

  const handleAutoAll = useCallback(() => {
    modal.confirm({
      title: '批量自动标注',
      content: `对 background 以外、尚未标注的抓拍图做背景差分，并**直接写入标签**（之后可逐张修正）。当前灵敏度 ${autoSigma}（阈值 = ${autoSigma} × 实测噪声σ），最小面积 ${autoMinArea}%。`,
      okText: '开始',
      cancelText: '取消',
      onOk: async () => {
        setAutoBusy(true);
        try {
          const data = await autoAnnotate({
            all: true,
            only_unlabeled: true,
            sigma_k: autoSigma,
            min_area: autoMinArea / 100,
          });
          const summary = data.summary ?? { total: 0, written: 0, skipped: 0, failed: 0 };
          const failedRows = (data.results ?? []).filter((row) => row.status !== 'ok').slice(0, 5);
          modal.info({
            title: '批量自动标注完成',
            width: 520,
            content: (
              <div style={{ fontSize: 13, lineHeight: 1.8 }}>
                <div>
                  成功 <b>{summary.written}</b> 张 / 跳过 {summary.skipped} 张 / 未标注成功{' '}
                  <b>{summary.failed}</b> 张
                </div>
                {failedRows.length > 0 && (
                  <div style={{ marginTop: 6, color: '#b42318' }}>
                    {failedRows.map((row) => (
                      <div key={row.name} className="mono" style={{ fontSize: 12 }}>
                        {row.name}：{row.reason ?? row.status}
                      </div>
                    ))}
                  </div>
                )}
                <div style={{ marginTop: 8, color: '#475467' }}>
                  建议翻一遍结果，把框错/漏的图手动修正后再生成数据集。
                </div>
              </div>
            ),
          });
          await refresh(true);
          if (current) await loadImage(current);
        } catch (error) {
          message.error(errorText(error));
        } finally {
          setAutoBusy(false);
        }
      },
    });
  }, [autoMinArea, autoSigma, current, loadImage, modal, refresh]);

  // ------------------------------------------------------------ 数据增强
  /** 增强当前打开的这张，并自动打开结果——一眼就能看出标注有没有跟着转对 */
  const handleAugmentCurrent = useCallback(async () => {
    if (!current || augOps.length === 0) return;
    setAugBusy(true);
    try {
      const data = await applyAugment({
        names: [current],
        ops: augOps,
        // 单张增强：不管有没有标注、是不是背景，都照做（用户是主动点的）
        only_labeled: false,
        include_background: true,
        overwrite: true,
      });
      if (data.created === 0) {
        message.warning('没有生成新图：这张可能本身就是增强产物（不能对增强图再增强）');
        return;
      }
      message.success(`已生成 ${data.created} 张增强图，正在打开第一张核对`);
      await refresh(true);
      if (data.created_names[0]) await loadImage(data.created_names[0]);
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setAugBusy(false);
    }
  }, [augOps, current, loadImage, message, refresh]);

  /** 批量增强：先弹确认（含预估张数），再执行 */
  const handleAugmentBatch = useCallback(() => {
    if (augOps.length === 0) {
      message.warning('先选至少一种增强方式');
      return;
    }
    if (augEstimate.sources === 0) {
      message.warning('当前条件下没有可增强的图片');
      return;
    }
    modal.confirm({
      title: '批量生成增强图',
      width: 520,
      content: (
        <div style={{ fontSize: 13, lineHeight: 1.9 }}>
          <div>
            类别：<b>{augClasses.length ? augClasses.map(labelOf).join('、') : '全部类别'}</b>
          </div>
          <div>增强方式：{augOps.map(opLabel).join('、')}</div>
          <div>
            源图 <b>{augEstimate.sources}</b> 张 × {augOps.length} 种 ={' '}
            <b>{augEstimate.total}</b> 张新图
          </div>
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
            原图不会被修改；新图与原图同目录、文件名加后缀，会直接出现在列表里，
            「生成数据集」时也会自动带上。张数多时请耐心等一会儿。
          </div>
        </div>
      ),
      okText: '开始生成',
      cancelText: '取消',
      onOk: async () => {
        setAugBusy(true);
        try {
          const data = await applyAugment({
            ops: augOps,
            classes: augClasses.length ? augClasses : undefined,
            only_labeled: augOnlyLabeled,
            include_background: augBackground,
          });
          const skipped = Object.entries(data.skipped ?? {})
            .filter(([, value]) => value > 0)
            .map(([key, value]) => `${key} ${value}`)
            .join('，');
          modal.info({
            title: '增强完成',
            width: 520,
            content: (
              <div style={{ fontSize: 13, lineHeight: 1.9 }}>
                <div>
                  处理源图 <b>{data.sources}</b> 张，新建 <b>{data.created}</b> 张
                </div>
                {skipped && <div className="muted">跳过：{skipped}</div>}
                {data.failed > 0 && <div style={{ color: '#b42318' }}>失败 {data.failed} 张</div>}
                {(data.failed_examples ?? []).slice(0, 3).map((item) => (
                  <div key={item.name} className="mono" style={{ fontSize: 12, color: '#b42318' }}>
                    {item.name}：{item.reason}
                  </div>
                ))}
                <div style={{ marginTop: 6 }} className="muted">
                  建议点几张增强图核对一下标注框，再点「生成数据集」。
                </div>
              </div>
            ),
          });
          await refresh(true);
        } catch (error) {
          message.error(errorText(error));
        } finally {
          setAugBusy(false);
        }
      },
    });
  }, [
    augBackground,
    augClasses,
    augEstimate.sources,
    augEstimate.total,
    augOnlyLabeled,
    augOps,
    labelOf,
    message,
    modal,
    opLabel,
    refresh,
  ]);

  /** 清理增强产物（先 dry-run 报数，再确认删除） */
  const handleAugmentClean = useCallback(async () => {
    const classes = augClasses.length ? augClasses : undefined;
    try {
      const preview = await cleanAugment({ classes, dry_run: true });
      if (preview.matched === 0) {
        message.info('没有找到增强产物');
        return;
      }
      modal.confirm({
        title: '清理增强图',
        content: (
          <div style={{ fontSize: 13, lineHeight: 1.8 }}>
            <div>
              将删除 <b>{preview.matched}</b> 个增强产物（连同它们的标签文件）。
            </div>
            <div style={{ color: '#b42318' }}>只删文件名带增强后缀的图，原图和手工标注不受影响。</div>
          </div>
        ),
        okText: '删除',
        okButtonProps: { danger: true },
        cancelText: '取消',
        onOk: async () => {
          try {
            const result = await cleanAugment({ classes });
            message.success(`已删除 ${result.removed} 个增强产物`);
            await refresh(true);
          } catch (error) {
            message.error(errorText(error));
          }
        },
      });
    } catch (error) {
      message.error(errorText(error));
    }
  }, [augClasses, message, modal, refresh]);

  // ------------------------------------------------------------ 渲染
  const currentBoxes = useMemo(() => {
    const list = [...boxes];
    if (draft) {
      list.push({
        class_name: newClass,
        x1: Math.min(draft.x1, draft.x2),
        y1: Math.min(draft.y1, draft.y2),
        x2: Math.max(draft.x1, draft.x2),
        y2: Math.max(draft.y1, draft.y2),
      });
    }
    return list;
  }, [boxes, draft, newClass]);

  const unlabeled = images.filter((item) => !item.labeled).length;

  return (
    <Row gutter={[16, 16]}>
      {/* ---------------------------------------------------------- 左：图片列表 */}
      <Col xs={24} xl={6}>
        <Card
          className="panel-card"
          title={
            <Space size={8}>
              <span className="panel-title"><PictureOutlined />抓拍图片</span>
              <Tag color={unlabeled ? 'orange' : 'green'}>待标注 {unlabeled}</Tag>
            </Space>
          }
          extra={
            <Space size={4}>
              {/* 自动刷新开着时，即使把设置条隐藏了，这里也能看到当前间隔 */}
              {autoRefresh > 0 && (
                <Tooltip title={`每 ${autoRefresh} 秒自动重新扫描一次，还有 ${autoSecondsLeft} 秒`}>
                  <Tag color="blue" className="mono">
                    自动 {autoRefresh}s
                  </Tag>
                </Tooltip>
              )}
              <Tooltip
                title={
                  autoRefreshBar
                    ? '隐藏自动刷新设置（设置仍在生效）'
                    : '显示自动刷新设置'
                }
              >
                <Button
                  size="small"
                  type={autoRefresh > 0 ? 'primary' : 'default'}
                  icon={<ClockCircleOutlined />}
                  onClick={() => setAutoRefreshBar((value) => !value)}
                />
              </Tooltip>
              <Tooltip title="重新扫描 training/captures">
                <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void refresh()} />
              </Tooltip>
            </Space>
          }
        >
          {autoRefreshBar && (
            <div className="auto-refresh-bar">
              <Space size={6}>
                <Tooltip title="按设定的间隔自动重新扫描抓拍目录：另一位标注员刚抓拍的图会自动出现在列表里">
                  <span className="muted">自动刷新</span>
                </Tooltip>
                <Select
                  size="small"
                  style={{ width: 92 }}
                  value={autoRefresh}
                  onChange={(value: number) => setAutoRefresh(value)}
                  options={AUTO_REFRESH_OPTIONS}
                />
                {autoRefresh > 0 && (
                  <span className="muted mono auto-refresh-count" title="下次刷新倒计时">
                    {autoSecondsLeft}s
                  </span>
                )}
              </Space>
              <Tooltip title="隐藏这条设置（自动刷新继续生效；点标题栏的时钟图标可再显示）">
                <Button
                  size="small"
                  type="text"
                  icon={<EyeInvisibleOutlined />}
                  onClick={() => setAutoRefreshBar(false)}
                />
              </Tooltip>
            </div>
          )}
          <Segmented
            block
            value={filter}
            onChange={(value) => setFilter(value as Filter)}
            options={[
              { label: `全部 ${images.length}`, value: 'all' },
              { label: `未标注 ${unlabeled}`, value: 'unlabeled' },
              { label: `已标注 ${images.length - unlabeled}`, value: 'labeled' },
            ]}
            style={{ marginBottom: 10 }}
          />
          {filtered.length > listLimit && (
            <Button
              block
              size="small"
              onClick={() => setListLimit((value) => value + 200)}
              style={{ marginBottom: 8 }}
            >
              显示更多（还有 {filtered.length - listLimit} 张，已显示 {listLimit}）
            </Button>
          )}
          <div className="annotate-list">
            {filtered.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  images.length === 0 ? '还没有抓拍图，去「摄像头」页抓拍' : '当前筛选下没有图片'
                }
              />
            ) : (
              <List
                size="small"
                dataSource={filtered.slice(0, listLimit)}
                renderItem={(item, itemIndex) => (
                  <List.Item
                    className={`annotate-item${item.name === current ? ' active' : ''}${
                      isClaimedByOther(item) ? ' claimed' : ''
                    }`}
                    onClick={() => {
                      if (isClaimedByOther(item)) {
                        message.warning('这张图正在被另一位标注员处理，先标别的吧');
                        return;
                      }
                      if (dirty && current) void save(true);
                      void loadImage(item.name);
                    }}
                  >
                    <Space size={4} style={{ width: '100%', justifyContent: 'space-between' }}>
                      <span className="mono annotate-name" title={item.name}>
                        {itemIndex + 1}. {item.name.split('/').pop()}
                      </span>
                      <Space size={2}>
                        {isClaimedByOther(item) && (
                          <Tooltip title="另一位标注员正在处理这张图">
                            <Tag color="blue">标注中</Tag>
                          </Tooltip>
                        )}
                        {item.labeled ? <Tag color="green">{item.boxes} 框</Tag> : <Tag>未标注</Tag>}
                        <Tooltip title="删除这张图片及其标注（不可恢复）">
                          <Button
                            type="text"
                            size="small"
                            danger
                            icon={<DeleteOutlined />}
                            onClick={(event) => {
                              event.stopPropagation();
                              handleDeleteImage(item.name);
                            }}
                          />
                        </Tooltip>
                      </Space>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </div>
          <div className="muted annotate-base mono" title={base}>
            {base || 'training/captures'}
          </div>
        </Card>
      </Col>

      {/* ---------------------------------------------------------- 中：画面 + 画框 */}
      <Col xs={24} xl={12}>
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <CameraPreview
            cameraState={cameraState}
            onCameraState={onCameraState}
            onStateRefresh={onStateRefresh}
          >
            <Space.Compact style={{ width: '100%', marginTop: 8 }}>
              <Select
                size="small"
                style={{ flex: 1 }}
                value={captureClass}
                onChange={(value: string) => setCaptureClass(value)}
                options={captureOptions}
              />
              <Button
                size="small"
                type="primary"
                icon={<CameraOutlined />}
                loading={capturing}
                onClick={() => void handleCapture()}
              >
                抓拍
              </Button>
            </Space.Compact>
            <div className="muted" style={{ fontSize: 12, marginTop: 4, lineHeight: 1.6 }}>
              拍完会自动出现在左侧列表并选中，可以直接标注。
            </div>
          </CameraPreview>

          <Card
          className="panel-card"
          title={
            <Space size={8}>
              <span className="panel-title"><EditOutlined />标注</span>
              {current && <span className="mono muted">{current}</span>}
              {dirty && <Tag color="orange">未保存</Tag>}
            </Space>
          }
          extra={
            <Space size={6}>
              <Button size="small" icon={<StepBackwardOutlined />} onClick={() => void goto(-1)}>
                上一张
              </Button>
              <Button size="small" icon={<StepForwardOutlined />} onClick={() => void goto(1)}>
                下一张
              </Button>
              <Button
                size="small"
                type="primary"
                icon={<SaveOutlined />}
                loading={saving}
                disabled={!current}
                onClick={() => void save()}
              >
                保存
              </Button>
              <Button size="small" disabled={!current} loading={saving} onClick={() => void saveAndNext()}>
                保存并下一张
              </Button>
              <Tooltip title="删除当前这张图片及其标注（不可恢复）">
                <Button
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  disabled={!current}
                  onClick={() => current && handleDeleteImage(current)}
                />
              </Tooltip>
            </Space>
          }
        >
          {current ? (
            <>
              <div
                className="anno-stage"
                ref={stageRef}
                onMouseDown={startDraw}
                style={{ cursor: 'crosshair' }}
              >
                <img src={`${annotateImageUrl(current)}&t=${imgNonce}`} alt={current} draggable={false} />
                {currentBoxes.map((box, boxIndex) => {
                  const isDraft = draft !== null && boxIndex === currentBoxes.length - 1;
                  const isSelected = selected === boxIndex && !isDraft;
                  const color = colorOf(box.class_name);
                  return (
                    <div
                      key={boxIndex}
                      className={`anno-box${isSelected ? ' selected' : ''}${isDraft ? ' draft' : ''}`}
                      style={{
                        left: `${box.x1 * 100}%`,
                        top: `${box.y1 * 100}%`,
                        width: `${(box.x2 - box.x1) * 100}%`,
                        height: `${(box.y2 - box.y1) * 100}%`,
                        borderColor: color,
                      }}
                      onMouseDown={(event) => {
                        if (isDraft) return;
                        startMove(event, boxIndex);
                      }}
                    >
                      <span className="anno-box-label" style={{ background: color }}>
                        {labelOf(box.class_name)}
                      </span>
                      {isSelected &&
                        HANDLES.map((handle) => (
                          <span
                            key={handle}
                            className={`anno-handle anno-handle-${handle}`}
                            style={{ borderColor: color }}
                            onMouseDown={(event) => startResize(event, boxIndex, handle)}
                          />
                        ))}
                    </div>
                  );
                })}
              </div>
              <Alert
                type="info"
                showIcon
                style={{ marginTop: 10 }}
                message="空白处按住左键拖动 = 画新框；框内拖动 = 移动位置；点框选中后拖动 8 个控制点 = 改大小；Delete 删框，←/→ 切图，数字键 1~9 选类别。"
              />
            </>
          ) : (
            <Empty description="从左侧选一张图片开始标注" />
          )}
        </Card>
        </Space>
      </Col>

      {/* ---------------------------------------------------------- 右：类别/框列表/导出 */}
      <Col xs={24} xl={6}>
        <Card className="panel-card" title={<span className="panel-title"><TagsOutlined />类别与标注框</span>}>
          <div className="section-title" style={{ marginTop: 0, paddingTop: 0, borderTop: 'none' }}>
            <span>新框使用的类别</span>
          </div>
          <Select
            style={{ width: '100%' }}
            value={newClass || undefined}
            onChange={(value: string) => applyClass(value)}
            options={classes.map((item) => ({
              value: item.name,
              label: `${item.label_zh}（${item.name} → 索引 ${item.index}${
                item.slot != null ? ` / slot ${item.slot}` : ''
              }）`,
            }))}
          />

          <div className="section-title">
            <span>本图标注框（{boxes.length}）</span>
            {selected !== null && (
              <Button size="small" danger icon={<DeleteOutlined />} onClick={() => removeBox(selected)}>
                删除选中
              </Button>
            )}
          </div>
          <div className="annotate-boxes">
            {boxes.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有标注框" />
            ) : (
              <List
                size="small"
                dataSource={boxes}
                renderItem={(box, boxIndex) => (
                  <List.Item
                    className={`annotate-item${selected === boxIndex ? ' active' : ''}`}
                    onClick={() => setSelected(boxIndex)}
                  >
                    <Space size={6} style={{ width: '100%', justifyContent: 'space-between' }}>
                      <span style={{ color: colorOf(box.class_name), fontWeight: 600 }}>
                        {labelOf(box.class_name)}
                      </span>
                      <span className="mono muted">
                        {Math.round((box.x2 - box.x1) * 100)}%×{Math.round((box.y2 - box.y1) * 100)}%
                      </span>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </div>

          <Space size={8} style={{ marginTop: 8 }}>
            <Switch size="small" checked={autoNext} onChange={setAutoNext} />
            <span className="muted" style={{ fontSize: 12 }}>
              保存后自动跳下一张未标注
            </span>
          </Space>

          <div className="section-title">
            <span>自动标注（背景差分）</span>
            {reference ? <Tag color="green">参考图已设</Tag> : <Tag color="orange">缺参考图</Tag>}
          </div>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {reference ? (
              <Space size={8} align="start">
                <img
                  className="reference-thumb"
                  src={`${referenceImageUrl()}?t=${refNonce}`}
                  alt="空托盘参考图"
                />
                <Space direction="vertical" size={4}>
                  <span className="muted" style={{ fontSize: 12 }}>
                    空托盘参考图
                    {reference.size ? ` ${reference.size[0]}×${reference.size[1]}` : ''}
                  </span>
                  <Button size="small" onClick={() => void handleSetReference(false, 8)}>
                    连拍重拍（取中值）
                  </Button>
                  <Button size="small" danger onClick={() => void handleClearReference()}>
                    清除参考图
                  </Button>
                </Space>
              </Space>
            ) : (
              <div className="muted" style={{ fontSize: 12, lineHeight: 1.6 }}>
                把托盘清空，点下面按钮拍一张参考图；之后每张抓拍图都能用「当前图 − 空托盘」的差异找出物体。
              </div>
            )}
            <Space size={6} wrap>
              <Button size="small" type="primary" onClick={() => void handleSetReference(false, 8)}>
                连拍 8 帧取中值设为参考图
              </Button>
              <Button size="small" onClick={() => void handleSetReference(false, 1)}>
                单帧设为参考图
              </Button>
            </Space>
            <Space size={6} wrap>
              <Tooltip title="用左侧当前选中的这张抓拍图当参考图（适合补拍或离线处理）">
                <Button size="small" disabled={!current} onClick={() => void handleSetReference(true)}>
                  用选中的图
                </Button>
              </Tooltip>
            </Space>
            <Space size={8} wrap>
              <Tooltip title="实际阈值 = 灵敏度 × 实测噪声σ。出现不存在的碎框就调大，物体没框到就调小">
                <span className="muted">灵敏度</span>
              </Tooltip>
              <InputNumber
                size="small"
                min={2}
                max={15}
                step={0.5}
                value={autoSigma}
                onChange={(value) => setAutoSigma(Number(value ?? 6))}
                style={{ width: 72 }}
              />
              <span className="muted">最小面积%</span>
              <InputNumber
                size="small"
                min={0}
                max={50}
                step={0.1}
                value={autoMinArea}
                onChange={(value) => setAutoMinArea(Number(value ?? 0.5))}
                style={{ width: 72 }}
              />
            </Space>
            <Button
              block
              icon={<ThunderboltOutlined />}
              loading={autoBusy}
              disabled={!current || !reference}
              onClick={() => void handleAutoOne()}
            >
              自动标注当前图
            </Button>
            <Button block loading={autoBusy} disabled={!reference} onClick={handleAutoAll}>
              批量自动标注未标注图
            </Button>
            {autoInfo && (
              <div className="muted" style={{ fontSize: 12, lineHeight: 1.6 }}>
                找到 {autoInfo.regions} 个区域 ｜ 噪声σ {autoInfo.noise_sigma} ｜ 实际阈值{' '}
                {autoInfo.effective_threshold} ｜ 覆盖 {(autoInfo.coverage * 100).toFixed(1)}% ｜{' '}
                {autoInfo.elapsed_ms} ms
                {autoInfo.rejected && (
                  <>
                    <br />
                    已滤掉：面积 {autoInfo.rejected.area} ｜ 实心度 {autoInfo.rejected.fill} ｜ 证据不足{' '}
                    {autoInfo.rejected.evidence}
                  </>
                )}
              </div>
            )}
          </Space>

          <div className="section-title">
            <span>数据增强（旋转 / 翻转 / 明暗）</span>
          </div>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Select
              mode="multiple"
              allowClear
              size="small"
              style={{ width: '100%' }}
              placeholder="类别（不选 = 全部）"
              value={augClasses}
              onChange={(value: string[]) => setAugClasses(value)}
              options={classes.map((item) => ({
                value: item.name,
                label: `${item.label_zh}（${item.name}）`,
              }))}
            />
            <Select
              mode="multiple"
              size="small"
              style={{ width: '100%' }}
              placeholder="增强方式"
              value={augOps}
              onChange={(value: string[]) => setAugOps(value)}
              options={augOpList.map((op) => ({
                value: op.key,
                label: `${op.label_zh}　${op.suffix}`,
              }))}
            />
            <Space size={10} wrap>
              <Space size={4}>
                <Switch size="small" checked={augOnlyLabeled} onChange={setAugOnlyLabeled} />
                <span className="muted" style={{ fontSize: 12 }}>
                  只处理已标注图
                </span>
              </Space>
              <Tooltip title="机位固定，旋转空托盘图会造出实际不会出现的方向；一般不勾">
                <Space size={4}>
                  <Switch size="small" checked={augBackground} onChange={setAugBackground} />
                  <span className="muted" style={{ fontSize: 12 }}>
                    含背景图
                  </span>
                </Space>
              </Tooltip>
            </Space>
            <div className="muted" style={{ fontSize: 12 }}>
              预计：源图 <b>{augEstimate.sources}</b> 张 × {augOps.length} 种 ={' '}
              <b>{augEstimate.total}</b> 张新图
            </div>
            <Button
              block
              icon={<ThunderboltOutlined />}
              loading={augBusy}
              disabled={!current || augOps.length === 0}
              onClick={() => void handleAugmentCurrent()}
            >
              增强当前图（自动打开结果）
            </Button>
            <Button
              block
              type="primary"
              loading={augBusy}
              disabled={augOps.length === 0}
              onClick={handleAugmentBatch}
            >
              批量增强…
            </Button>
            <Button block danger icon={<DeleteOutlined />} onClick={() => void handleAugmentClean()}>
              清理增强图…
            </Button>
            <div className="muted" style={{ fontSize: 12, lineHeight: 1.7 }}>
              只做 <b>90°/180°/270° 与翻转</b>：这些变换下标注框能<b>精确换算</b>；
              任意角度会把框撑大、混进背景，所以不做。
              产物与原图同目录（文件名加 <span className="mono">_r90</span> 这类后缀），
              生成数据集时自动带上。
            </div>
          </Space>

          <div className="section-title">
            <span>生成训练数据集</span>
          </div>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space size={8}>
              <span className="muted">验证集比例</span>
              <InputNumber
                min={0}
                max={0.5}
                step={0.05}
                value={valRatio}
                onChange={(value) => setValRatio(Number(value ?? 0.2))}
                style={{ width: 90 }}
              />
            </Space>
            <Space size={8}>
              <Switch size="small" checked={withBackground} onChange={setWithBackground} />
              <span className="muted">把 background 目录的空托盘图当背景样本</span>
            </Space>
            <Button
              type="primary"
              block
              icon={<ExperimentOutlined />}
              loading={building}
              onClick={build}
            >
              生成数据集
            </Button>
            <div className="muted" style={{ fontSize: 12, lineHeight: 1.6 }}>
              输出到 <span className="mono">training/datasets/captures</span> 和{' '}
              <span className="mono">training/data-captures.yaml</span>，只包含已标注的图片。
            </div>
          </Space>
        </Card>
      </Col>
    </Row>
  );
}

// 用 memo 包一层：控制台每 ~100ms 会推一次识别事件，不包的话这个页面会被反复重渲染
export default memo(AnnotatePanel);
