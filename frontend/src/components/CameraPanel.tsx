import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { App as AntApp, Badge, Button, Card, Empty, Select, Space, Tag, Tooltip } from 'antd';
import {
  CameraOutlined,
  DownOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  ReloadOutlined,
  UpOutlined,
  VideoCameraAddOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons';

import {
  captureFrame,
  errorText,
  listCameras,
  listCaptures,
  selectCamera,
  videoFeedUrl,
  type CameraInfo,
  type CameraState,
  type ClassEntry,
} from '../api';

/** 连拍张数与间隔（连拍用于拍空托盘：点一次，边轻微变换光照/挪动物件边自动拍） */
const BURST_COUNT = 8;
const BURST_INTERVAL_MS = 1500;

export interface CameraPanelProps {
  cameraState: CameraState | null;
  /** 摄像头状态变化时回传给 App（用于顶部状态栏） */
  onCameraState: (state: CameraState) => void;
  /** 打开摄像头后让 App 重新拉取一次全量状态 */
  onStateRefresh: () => void;
  /** classes.json 的类别表，用于抓拍时选类别 */
  classes: Record<string, ClassEntry>;
}

/**
 * CameraPanel：摄像头列表 / 选择 / 预览开关 / 抓拍采集训练图。
 * 画面来自后端 GET /api/video/feed（MJPEG），检测框由后端画在流上。
 */
export default function CameraPanel({
  cameraState,
  onCameraState,
  onStateRefresh,
  classes,
}: CameraPanelProps) {
  const { message } = AntApp.useApp();
  const [cameras, setCameras] = useState<CameraInfo[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [opening, setOpening] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [nonce, setNonce] = useState(() => Date.now());
  const [feedFailed, setFeedFailed] = useState(false);
  const autoPreviewed = useRef(false);

  const [captureClass, setCaptureClass] = useState<string>('background');
  const [captureCounts, setCaptureCounts] = useState<Record<string, number>>({});
  const [captureBase, setCaptureBase] = useState('');
  const [capturing, setCapturing] = useState(false);
  const [burstLeft, setBurstLeft] = useState(0);
  // 抓拍区默认收起，点标题右侧的按钮展开/收起
  const [captureOpen, setCaptureOpen] = useState(false);
  const burstTimer = useRef<number | null>(null);

  const current = cameraState?.camera ?? null;
  const opened = Boolean(cameraState?.opened);

  const captureOptions = useMemo(
    () => [
      { value: 'background', label: 'background（空托盘背景图）' },
      ...Object.entries(classes ?? {}).map(([name, entry]) => ({
        value: name,
        label: `${name}（${entry.label_zh}）`,
      })),
    ],
    [classes],
  );

  const loadCaptureCounts = useCallback(async () => {
    try {
      const data = await listCaptures();
      setCaptureCounts(data.counts ?? {});
      setCaptureBase(data.base);
    } catch {
      /* 抓拍统计不影响主流程，失败就忽略 */
    }
  }, []);

  useEffect(() => {
    void loadCaptureCounts();
  }, [loadCaptureCounts]);

  // 离开页面时清掉连拍定时器
  useEffect(
    () => () => {
      if (burstTimer.current !== null) window.clearTimeout(burstTimer.current);
    },
    [],
  );

  const refresh = useCallback(
    async (silent = false, force = false) => {
      setLoading(true);
      try {
        const data = await listCameras(force);
        setCameras(data.cameras ?? []);
        setSelected((previous) => previous ?? data.current ?? data.cameras?.[0]?.index ?? null);
        if (!silent) {
          message.success(`发现 ${data.count} 个可用摄像头`);
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

  // 后端开机自启了摄像头时，自动开始预览
  useEffect(() => {
    if (opened && !autoPreviewed.current) {
      autoPreviewed.current = true;
      setPreviewing(true);
      setNonce(Date.now());
    }
  }, [opened]);

  // 预览真正建立连接后（MJPEG 客户端计数、编码开关会变化）再同步一次状态
  useEffect(() => {
    if (!previewing) return;
    const timer = window.setTimeout(() => onStateRefresh(), 1500);
    return () => window.clearTimeout(timer);
  }, [onStateRefresh, previewing, nonce]);

  const handleOpen = useCallback(async () => {
    if (selected === null) {
      message.warning('请先选择一个摄像头');
      return;
    }
    setOpening(true);
    try {
      const data = await selectCamera(selected);
      onCameraState(data.state);
      setFeedFailed(false);
      setPreviewing(true);
      setNonce(Date.now());
      message.success(`已打开 Camera ${data.camera.index}（${data.camera.width}×${data.camera.height}）`);
      onStateRefresh();
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setOpening(false);
    }
  }, [message, onCameraState, onStateRefresh, selected]);

  const togglePreview = useCallback(() => {
    setFeedFailed(false);
    setNonce(Date.now());
    setPreviewing((value) => !value);
  }, []);

  const captureOnce = useCallback(
    async (name: string) => {
      try {
        const data = await captureFrame(name);
        setCaptureCounts(data.counts ?? {});
        return true;
      } catch (error) {
        message.error(errorText(error));
        return false;
      }
    },
    [message],
  );

  const stopBurst = useCallback(() => {
    if (burstTimer.current !== null) {
      window.clearTimeout(burstTimer.current);
      burstTimer.current = null;
    }
    setBurstLeft(0);
  }, []);

  const handleCapture = useCallback(async () => {
    if (!opened) {
      message.warning('请先打开摄像头');
      return;
    }
    setCapturing(true);
    const ok = await captureOnce(captureClass);
    setCapturing(false);
    if (ok) message.success(`已抓拍 → ${captureClass}`);
  }, [captureClass, captureOnce, message, opened]);

  const toggleBurst = useCallback(() => {
    if (burstLeft > 0) {
      stopBurst();
      message.info('已停止连拍');
      return;
    }
    if (!opened) {
      message.warning('请先打开摄像头');
      return;
    }
    let remaining = BURST_COUNT;
    setBurstLeft(remaining);
    const tick = async () => {
      if (remaining <= 0) {
        stopBurst();
        return;
      }
      remaining -= 1;
      setBurstLeft(remaining);
      await captureOnce(captureClass);
      if (remaining > 0) {
        burstTimer.current = window.setTimeout(() => void tick(), BURST_INTERVAL_MS);
      } else {
        stopBurst();
      }
    };
    void tick();
  }, [burstLeft, captureClass, captureOnce, message, opened, stopBurst]);

  const feedUrl = useMemo(() => videoFeedUrl(nonce), [nonce]);

  const options = cameras.map((camera) => ({
    value: camera.index,
    label: `Camera ${camera.index} · ${camera.width}×${camera.height} · ${camera.backend}`,
  }));

  return (
    <Card
      className="panel-card"
      title={
        <Space size={8}>
          <span className="panel-title"><VideoCameraOutlined />摄像头</span>
          <Badge
            status={opened ? 'success' : 'default'}
            text={opened ? `Camera ${current?.index ?? '-'}` : '未打开'}
            className="panel-badge"
          />
        </Space>
      }
      extra={
        <Space size={8}>
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => void refresh(false, true)}
          >
            刷新列表
          </Button>
          <Button
            size="small"
            type={previewing ? 'default' : 'primary'}
            danger={previewing}
            icon={previewing ? <EyeInvisibleOutlined /> : <EyeOutlined />}
            onClick={togglePreview}
          >
            {previewing ? '停止预览' : '开始预览'}
          </Button>
        </Space>
      }
    >
      <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
        <Select
          style={{ flex: 1 }}
          placeholder="选择摄像头（0-10 自动扫描，能读帧才列出）"
          value={selected ?? undefined}
          options={options}
          onChange={(value: number) => setSelected(value)}
          notFoundContent={loading ? '扫描中…' : '未发现可用摄像头'}
        />
        <Button
          type="primary"
          icon={<VideoCameraAddOutlined />}
          loading={opening}
          onClick={() => void handleOpen()}
        >
          打开
        </Button>
      </Space.Compact>

      <div className={`preview-frame${previewing ? '' : ' preview-idle'}`}>
        {previewing ? (
          <>
            <img
              key={nonce}
              src={feedUrl}
              alt="摄像头预览"
              onError={() => setFeedFailed(true)}
              onLoad={() => setFeedFailed(false)}
            />
            <div className="preview-overlay">
              <Tag color="green">MJPEG</Tag>
              {opened && <Tag color="blue">{`${current?.width}×${current?.height}`}</Tag>}
              <Tag>{current?.backend ?? '-'}</Tag>
              {cameraState?.overlay_attached && <Tag color="gold">检测框叠加</Tag>}
            </div>
          </>
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={opened ? '预览已暂停（摄像头保持打开）' : '请选择并打开摄像头'}
          />
        )}
        {previewing && feedFailed && (
          <div className="preview-error">视频流中断，请确认后端已启动且摄像头未被占用</div>
        )}
      </div>

      <div className="stat-row">
        <Tooltip title="后端抓帧线程累计读取的帧数">
          <div className="stat-item">
            <span className="stat-label">帧数</span>
            <span className="stat-value">{cameraState?.frame_count ?? 0}</span>
          </div>
        </Tooltip>
        <Tooltip title="当前正在观看 MJPEG 预览流的客户端数量">
          <div className="stat-item">
            <span className="stat-label">预览客户端</span>
            <span className="stat-value">{cameraState?.stream_clients ?? 0}</span>
          </div>
        </Tooltip>
        <Tooltip title="有预览客户端时才编码 JPEG，空闲不占 CPU">
          <div className="stat-item">
            <span className="stat-label">编码</span>
            <span className="stat-value">{cameraState?.encoding ? '开' : '关'}</span>
          </div>
        </Tooltip>
        <div className="stat-item">
          <span className="stat-label">设备帧率</span>
          <span className="stat-value">{current ? `${current.fps.toFixed(0)} fps` : '-'}</span>
        </div>
        <Tooltip title="看门狗会自动重开失效的摄像头（笔记本休眠唤醒 / USB 掉线后最容易失效）">
          <div className="stat-item">
            <span className="stat-label">自动重连</span>
            <span className="stat-value">
              {cameraState?.restarts ?? 0} 次
              {(cameraState?.frame_age ?? 0) > 5 ? ' ⚠' : ''}
            </span>
          </div>
        </Tooltip>
      </div>
      {(cameraState?.frame_age ?? 0) > 5 && (
        <div className="error-line">
          画面已 {cameraState?.frame_age?.toFixed(0)} 秒未更新，看门狗正在重开摄像头
          {cameraState?.last_error ? `（${cameraState.last_error}）` : ''}
        </div>
      )}

      <div className="section-title">
        <span>采集训练图（抓拍）</span>
        <Space size={6}>
          <span className="muted">
            已抓拍 {Object.values(captureCounts).reduce((sum, value) => sum + value, 0)} 张
          </span>
          <Button
            size="small"
            type="link"
            icon={captureOpen ? <UpOutlined /> : <DownOutlined />}
            onClick={() => setCaptureOpen((value) => !value)}
          >
            {captureOpen ? '收起' : '展开'}
          </Button>
        </Space>
      </div>
      {captureOpen && (
        <>
          <Space.Compact style={{ width: '100%' }}>
            <Select
              style={{ flex: 1 }}
              value={captureClass}
              onChange={(value: string) => setCaptureClass(value)}
              options={captureOptions}
            />
            <Button
              type="primary"
              icon={<CameraOutlined />}
              loading={capturing}
              disabled={!opened}
              onClick={() => void handleCapture()}
            >
              抓拍
            </Button>
            <Button danger={burstLeft > 0} disabled={!opened} onClick={toggleBurst}>
              {burstLeft > 0 ? `停止连拍（剩 ${burstLeft}）` : `连拍 ${BURST_COUNT} 张`}
            </Button>
          </Space.Compact>
          <div className="capture-hint">
            已抓拍 <b>{captureCounts[captureClass] ?? 0}</b> 张 {captureClass}。
            拍空托盘选 <span className="mono">background</span>，连拍时轻微变换光照或挪动物件；
            拍垃圾时每次只放一个物体。抓的是**原始帧**，不带检测框。
            <div className="mono" style={{ marginTop: 4 }}>
              {captureBase || 'training/captures'}
            </div>
          </div>
        </>
      )}
    </Card>
  );
}
