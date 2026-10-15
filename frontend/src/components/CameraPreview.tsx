import { useCallback, useEffect, useMemo, useState } from 'react';
import { App as AntApp, Button, Select, Space, Tag, Tooltip } from 'antd';
import {
  DownOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  ReloadOutlined,
  UpOutlined,
  VideoCameraAddOutlined,
} from '@ant-design/icons';

import {
  errorText,
  listCameras,
  selectCamera,
  videoFeedUrl,
  type CameraInfo,
  type CameraState,
} from '../api';

export interface CameraPreviewProps {
  cameraState: CameraState | null;
  onCameraState: (state: CameraState) => void;
  /** 打开摄像头后让 App 重新拉一次全量状态 */
  onStateRefresh: () => void;
  /** 预览下方的附加内容（标注页用来放"选类别 + 抓拍"） */
  children?: React.ReactNode;
  defaultOpen?: boolean;
}

/**
 * CameraPreview：紧凑的实时画面 + 摄像头选择。
 *
 * 与「摄像头」面板里的 CameraPanel 的区别：这里只保留"看画面 + 选设备"，
 * 没有统计、没有连拍，方便嵌在别的页面（例如数据标注页）里。
 */
export default function CameraPreview({
  cameraState,
  onCameraState,
  onStateRefresh,
  children,
  defaultOpen = true,
}: CameraPreviewProps) {
  const { message } = AntApp.useApp();
  const [open, setOpen] = useState(defaultOpen);
  const [cameras, setCameras] = useState<CameraInfo[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [opening, setOpening] = useState(false);
  const [previewing, setPreviewing] = useState(true);
  const [nonce, setNonce] = useState(() => Date.now());
  const [feedFailed, setFeedFailed] = useState(false);

  const current = cameraState?.camera ?? null;
  const opened = Boolean(cameraState?.opened);

  const refresh = useCallback(
    async (silent = false) => {
      setLoading(true);
      try {
        const data = await listCameras();
        setCameras(data.cameras ?? []);
        setSelected((previous) => previous ?? data.current ?? data.cameras?.[0]?.index ?? null);
        if (!silent) message.success(`发现 ${data.count} 个可用摄像头`);
      } catch (error) {
        message.error(errorText(error));
      } finally {
        setLoading(false);
      }
    },
    [message],
  );

  // 首次挂载扫描一次设备列表（静默，不弹提示）
  useEffect(() => {
    void refresh(true);
  }, [refresh]);

  // 后端的当前设备同步到下拉框
  useEffect(() => {
    if (current && current.index !== selected) setSelected(current.index);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.index]);

  // 打开摄像头后自动开始预览
  useEffect(() => {
    if (opened) {
      setFeedFailed(false);
      setNonce(Date.now());
    }
  }, [opened]);

  const handleOpen = useCallback(async () => {
    if (selected === null) {
      message.warning('请先选择一个摄像头');
      return;
    }
    setOpening(true);
    try {
      const data = await selectCamera(selected);
      onCameraState(data.state);
      onStateRefresh();
      setPreviewing(true);
      setFeedFailed(false);
      setNonce(Date.now());
      message.success(`已切到摄像头 ${data.camera.index}（${data.camera.width}×${data.camera.height}）`);
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setOpening(false);
    }
  }, [message, onCameraState, onStateRefresh, selected]);

  const feedUrl = useMemo(() => videoFeedUrl(nonce), [nonce]);
  const showFeed = open && opened && previewing;

  return (
    <div className="camera-preview panel-card">
      <div className="section-title" style={{ marginTop: 0, paddingTop: 0, borderTop: 'none' }}>
        <Space size={8}>
          <span className="panel-title">摄像头画面</span>
          {opened ? (
            <Tag color="green">
              {current ? `${current.width}×${current.height} · ${current.backend}` : '已打开'}
            </Tag>
          ) : (
            <Tag color="orange">未打开</Tag>
          )}
          {cameraState?.overlay_attached && <Tag color="gold">检测框叠加</Tag>}
        </Space>
        <Button
          size="small"
          type="link"
          icon={open ? <UpOutlined /> : <DownOutlined />}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? '收起' : '展开'}
        </Button>
      </div>

      {open && (
        <>
          <Space.Compact style={{ width: '100%', marginBottom: 8 }}>
            <Select
              size="small"
              style={{ flex: 1 }}
              value={selected ?? undefined}
              placeholder="选择摄像头"
              onChange={(value: number) => setSelected(value)}
              options={cameras.map((item) => ({
                value: item.index,
                label: `#${item.index} · ${item.width}×${item.height} · ${item.backend}`,
              }))}
            />
            <Tooltip title="重新扫描可用摄像头">
              <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void refresh()} />
            </Tooltip>
            <Button
              size="small"
              type="primary"
              icon={<VideoCameraAddOutlined />}
              loading={opening}
              onClick={() => void handleOpen()}
            >
              {opened ? '切换' : '打开'}
            </Button>
            <Tooltip title={previewing ? '暂停预览（摄像头保持打开）' : '开始预览'}>
              <Button
                size="small"
                icon={previewing ? <EyeInvisibleOutlined /> : <EyeOutlined />}
                disabled={!opened}
                onClick={() => {
                  setFeedFailed(false);
                  setNonce(Date.now());
                  setPreviewing((value) => !value);
                }}
              />
            </Tooltip>
          </Space.Compact>

          <div className="camera-preview-stage">
            {showFeed ? (
              <img
                src={feedUrl}
                alt="摄像头预览"
                onError={() => setFeedFailed(true)}
                onLoad={() => setFeedFailed(false)}
              />
            ) : (
              <div className="camera-preview-empty">
                {opened ? '预览已暂停' : '点「打开」开始预览'}
              </div>
            )}
            {showFeed && feedFailed && <div className="preview-error">视频流中断</div>}
          </div>

          {children}
        </>
      )}
    </div>
  );
}
