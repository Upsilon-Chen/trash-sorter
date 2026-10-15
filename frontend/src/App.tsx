import { useCallback, useEffect, useMemo, useState } from 'react';
import { App as AntApp, Button, Col, Menu, Row, Space, Tag, Tooltip } from 'antd';
import {
  CaretRightOutlined,
  DashboardOutlined,
  EditOutlined,
  PauseOutlined,
  SyncOutlined,
} from '@ant-design/icons';

import AnnotatePanel from './components/AnnotatePanel';
import CameraPanel from './components/CameraPanel';
import ModelPanel from './components/ModelPanel';
import ResultPanel from './components/ResultPanel';
import SerialPanel, { type SerialLogEntry } from './components/SerialPanel';
import {
  errorText,
  requestState,
  startDetection,
  stopDetection,
  trashSocket,
  type BackendState,
  type CameraState,
  type DetectionEventData,
  type DetectionSnapshot,
  type IncomingEvent,
  type ModelState,
  type SerialState,
  type SocketStatus,
  type SortEventData,
} from './api';

const HISTORY_LIMIT = 30;
const LOG_LIMIT = 50;

/** 左侧侧边栏的页面 */
type PageKey = 'dashboard' | 'annotate';

export default function App() {
  const { message } = AntApp.useApp();

  const [page, setPage] = useState<PageKey>('dashboard');
  const [wsStatus, setWsStatus] = useState<SocketStatus>(trashSocket.getStatus());
  const [snapshot, setSnapshot] = useState<DetectionSnapshot | null>(null);
  const [cameraState, setCameraState] = useState<CameraState | null>(null);
  const [modelState, setModelState] = useState<ModelState | null>(null);
  const [serialState, setSerialState] = useState<SerialState | null>(null);
  const [detection, setDetection] = useState<DetectionEventData | null>(null);
  const [history, setHistory] = useState<SortEventData[]>([]);
  const [serialLog, setSerialLog] = useState<SerialLogEntry[]>([]);
  const [confidence, setConfidence] = useState(0.35);
  const [busy, setBusy] = useState(false);

  const pushLog = useCallback((dir: SerialLogEntry['dir'], text: string) => {
    setSerialLog((previous) => [{ dir, text, ts: Date.now() / 1000 }, ...previous].slice(0, LOG_LIMIT));
  }, []);

  /** 兼容两种载荷：hello 的 {detection, camera, model, serial} 与 pipeline.snapshot() 本体。 */
  const applyState = useCallback((raw: unknown) => {
    if (!raw || typeof raw !== 'object') return;
    const state = raw as Partial<BackendState> & Partial<DetectionSnapshot>;
    const det = (state.detection ?? state) as DetectionSnapshot | undefined;
    if (!det || typeof det !== 'object') return;
    setSnapshot(det);
    if (det.camera) setCameraState(det.camera);
    if (det.model) setModelState(det.model);
    if (det.serial) setSerialState(det.serial);
    if (Array.isArray(det.results) && det.results.length > 0) {
      setDetection({
        results: det.results,
        count: det.results.length,
        fps: det.fps ?? 0,
        inference_ms: det.model?.last_inference_ms ?? 0,
        model: det.model?.current ?? null,
        ts: Date.now() / 1000,
      });
    }
  }, []);

  const handleEvent = useCallback(
    (event: IncomingEvent) => {
      const { type } = event;
      const data = event.data;
      switch (type) {
        case 'hello':
          applyState(data?.state);
          break;
        case 'state':
        case 'detection_state':
          applyState(data);
          break;
        case 'detection':
          setDetection(data as DetectionEventData);
          break;
        case 'sort':
          setHistory((previous) => [data as SortEventData, ...previous].slice(0, HISTORY_LIMIT));
          break;
        case 'camera_state':
          setCameraState(data as CameraState);
          break;
        case 'model_state':
          setModelState(data as ModelState);
          break;
        case 'serial_connected':
          pushLog('log', `已连接串口 ${data?.port ?? ''} @ ${data?.baudrate ?? ''}`.trim());
          requestState();
          break;
        case 'serial_disconnected':
          pushLog('log', `串口 ${data?.port ?? ''} 已断开（${data?.reason ?? ''}）`);
          requestState();
          break;
        case 'serial_tx':
          pushLog('tx', String(data?.raw ?? JSON.stringify(data?.payload ?? {})));
          break;
        case 'serial_message':
          pushLog('rx', String(data?.raw ?? JSON.stringify(data?.message ?? {})));
          break;
        case 'serial_log':
          pushLog('log', String(data?.raw ?? ''));
          break;
        case 'error':
          message.error(`${data?.scope ?? 'backend'}：${data?.message ?? '未知错误'}`);
          break;
        default:
          break;
      }
    },
    [applyState, message, pushLog],
  );

  useEffect(() => {
    const offEvent = trashSocket.onEvent(handleEvent);
    const offStatus = trashSocket.onStatus(setWsStatus);
    trashSocket.connect();
    return () => {
      offEvent();
      offStatus();
    };
  }, [handleEvent]);

  const running = Boolean(snapshot?.running);

  const handleStart = useCallback(async () => {
    setBusy(true);
    try {
      const data = await startDetection({ conf: confidence, reset_stats: false });
      applyState(data.detection);
      message.success('检测已开始');
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setBusy(false);
    }
  }, [applyState, confidence, message]);

  const handleStop = useCallback(async () => {
    setBusy(true);
    try {
      const data = await stopDetection();
      applyState(data.detection);
      message.success('检测已停止');
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setBusy(false);
    }
  }, [applyState, message]);

  const classes = useMemo(() => snapshot?.classes ?? {}, [snapshot]);

  const wsTag = useMemo(() => {
    if (wsStatus === 'open') return <Tag color="green">实时通道已连接</Tag>;
    if (wsStatus === 'connecting') return <Tag color="gold">连接中…</Tag>;
    return <Tag color="red">实时通道断开</Tag>;
  }, [wsStatus]);

  const canStart = Boolean(cameraState?.opened) && Boolean(modelState?.loaded);

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <div className="brand-logo">♻️</div>
          <div className="brand-text">
            <h1>YOLO 垃圾自动识别分类系统</h1>
            <p>摄像头采集 → YOLO 推理 → classes.json 映射 → ESP32 舵机分类</p>
          </div>
        </div>

        <div className="header-right">
          <Space size={6} wrap className="header-tags">
            <Tooltip title={trashSocket.url}>{wsTag}</Tooltip>
            <Tag color={cameraState?.opened ? 'blue' : 'default'}>
              {cameraState?.opened ? `Camera ${cameraState.camera?.index ?? '-'}` : '未选摄像头'}
            </Tag>
            <Tag color={modelState?.loaded ? 'geekblue' : 'default'}>
              {modelState?.loaded ? modelState.current : '模型未加载'}
            </Tag>
            <Tag color={serialState?.connected ? 'cyan' : 'default'}>
              {serialState?.connected ? `串口 ${serialState.connection?.port}` : '串口未连接'}
            </Tag>
            <Button size="small" icon={<SyncOutlined />} onClick={() => requestState()}>
              同步状态
            </Button>
          </Space>

          <Space size={8}>
            {running ? (
              <Button type="primary" danger size="large" icon={<PauseOutlined />} loading={busy} onClick={() => void handleStop()}>
                停止检测
              </Button>
            ) : (
              <Tooltip title={canStart ? '开始后台推理并推送结果' : '请先打开摄像头并加载模型'}>
                <Button
                  type="primary"
                  size="large"
                  className="start-button"
                  icon={<CaretRightOutlined />}
                  disabled={!canStart}
                  loading={busy}
                  onClick={() => void handleStart()}
                >
                  开始检测
                </Button>
              </Tooltip>
            )}
          </Space>
        </div>
      </header>

      <div className="app-body">
        <aside className="app-sider">
          <Menu
            mode="inline"
            selectedKeys={[page]}
            onClick={(info) => setPage(info.key as PageKey)}
            items={[
              { key: 'dashboard', icon: <DashboardOutlined />, label: '控制台' },
              { key: 'annotate', icon: <EditOutlined />, label: '数据标注' },
            ]}
          />
          <div className="sider-hint">
            {page === 'dashboard'
              ? '摄像头 / 模型 / 串口 / 识别结果'
              : '给抓拍图打框，导出成可训练数据集'}
          </div>
        </aside>

        <main className="app-main">
          {page === 'dashboard' ? (
            <Row gutter={[16, 16]}>
              <Col xs={24} xl={14}>
                <Space direction="vertical" size={16} style={{ width: '100%' }}>
                  <CameraPanel
                    cameraState={cameraState}
                    onCameraState={setCameraState}
                    onStateRefresh={() => requestState()}
                    classes={classes}
                  />
                  <ResultPanel
                    detection={detection}
                    snapshot={snapshot}
                    confidence={confidence}
                    history={history}
                  />
                </Space>
              </Col>
              <Col xs={24} xl={10}>
                <Space direction="vertical" size={16} style={{ width: '100%' }}>
                  <ModelPanel
                    modelState={modelState}
                    onModelState={setModelState}
                    confidence={confidence}
                    onConfidenceChange={setConfidence}
                    detectionRunning={running}
                    onStateRefresh={() => requestState()}
                  />
                  <SerialPanel
                    serialState={serialState}
                    onSerialState={setSerialState}
                    classes={classes}
                    log={serialLog}
                    onStateRefresh={() => requestState()}
                  />
                </Space>
              </Col>
            </Row>
          ) : (
            <AnnotatePanel
              cameraState={cameraState}
              onCameraState={setCameraState}
              onStateRefresh={() => requestState()}
            />
          )}
        </main>
      </div>

      <footer className="app-footer">
        <span>后端：FastAPI + OpenCV + ultralytics + pyserial</span>
        <span className="dot">·</span>
        <span>串口协议：一行 JSON {'{"cmd":"sort","class":"battery","slot":1,"angle":90}'}</span>
        <span className="dot">·</span>
        <span>WS：{trashSocket.url}</span>
      </footer>
    </div>
  );
}
