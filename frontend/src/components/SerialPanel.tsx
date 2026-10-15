import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  App as AntApp,
  AutoComplete,
  Badge,
  Button,
  Card,
  Descriptions,
  Empty,
  InputNumber,
  List,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  type TableProps,
} from 'antd';
import {
  ApiOutlined,
  DisconnectOutlined,
  ReloadOutlined,
  SendOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';

import {
  connectSerial,
  disconnectSerial,
  errorText,
  listSerialPorts,
  manualSort,
  type ClassEntry,
  type SerialPortInfo,
  type SerialState,
  type SortEventData,
} from '../api';

/** 串口原始报文日志条目（由 App 从 WS 事件累积） */
export interface SerialLogEntry {
  dir: 'tx' | 'rx' | 'log';
  text: string;
  ts: number;
}

export interface SerialPanelProps {
  serialState: SerialState | null;
  onSerialState: (state: SerialState) => void;
  classes: Record<string, ClassEntry>;
  log: SerialLogEntry[];
  onStateRefresh: () => void;
}

const BAUDRATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600];

/**
 * SerialPanel：串口扫描（CP210x / CH340 / ESP32 优先标记 likely_esp）、
 * 连接 / 断开、手动发送测试分类指令、查看原始收发报文。
 */
export default function SerialPanel({
  serialState,
  onSerialState,
  classes,
  log,
  onStateRefresh,
}: SerialPanelProps) {
  const { message } = AntApp.useApp();
  const [ports, setPorts] = useState<SerialPortInfo[]>([]);
  const [port, setPort] = useState<string | undefined>(undefined);
  const [baudrate, setBaudrate] = useState(115200);
  const [scanning, setScanning] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [sending, setSending] = useState(false);

  const [className, setClassName] = useState<string>('');
  const [slot, setSlot] = useState<number>(0);
  const [angle, setAngle] = useState<number>(90);
  const [force, setForce] = useState(true);
  // 手动角度覆盖：null 表示不覆盖，使用固件里 slot 对应的默认角度
  const [overrideH, setOverrideH] = useState<number | null>(null);
  const [overrideV, setOverrideV] = useState<number | null>(null);

  const connected = Boolean(serialState?.connected);
  const connection = serialState?.connection ?? null;

  const classOptions = useMemo(
    () =>
      Object.entries(classes ?? {}).map(([name, entry]) => ({
        value: name,
        // 类别名放最前面：下拉宽度不够被截断时，至少还能看清是哪个类别
        label: `${name}（${entry.label_zh ?? ''}·slot ${entry.slot}）`,
      })),
    [classes],
  );

  /** 当前类别会落到哪个物理桶（用来解释"为什么换类别动作没变"） */
  const currentSlotInfo = useMemo(() => {
    const entry = classes?.[className.trim()];
    if (!entry) return null;
    const sameSlot = Object.entries(classes ?? {})
      .filter(([name, item]) => item.slot === entry.slot && name !== className.trim())
      .map(([name]) => name);
    return { slot: entry.slot, sameSlot };
  }, [className, classes]);

  const scan = useCallback(
    async (silent = false) => {
      setScanning(true);
      try {
        const data = await listSerialPorts();
        setPorts(data.ports ?? []);
        onSerialState(data.state);
        setPort((previous) => previous ?? data.ports?.[0]?.device);
        if (!silent) {
          const likely = data.ports.filter((item) => item.likely_esp).length;
          message.success(`发现 ${data.count} 个串口（其中 ${likely} 个疑似 ESP32）`);
        }
      } catch (error) {
        message.error(errorText(error));
      } finally {
        setScanning(false);
      }
    },
    [message, onSerialState],
  );

  useEffect(() => {
    void scan(true);
  }, [scan]);

  // 类别映射到达后，给手动发送表单填一个默认类别（含 slot/angle）
  useEffect(() => {
    const names = Object.keys(classes ?? {});
    if (names.length === 0) return;
    setClassName((previous) => {
      if (previous.trim()) return previous;
      const first = names[0];
      const entry = classes[first];
      setSlot(entry.slot);
      setAngle(entry.angle);
      return first;
    });
  }, [classes]);

  const handleConnect = useCallback(
    async (target?: string) => {
      const device = target ?? port;
      if (!device) {
        message.warning('请先选择一个串口');
        return;
      }
      setConnecting(true);
      try {
        const data = await connectSerial(device, baudrate);
        onSerialState(data.state);
        setPort(device);
        message.success(
          data.state.connection?.ready
            ? `已连接 ${device}，ESP32 已上报 ready`
            : `已连接 ${device}（未收到 ready，稍后可重试）`,
        );
        onStateRefresh();
      } catch (error) {
        message.error(errorText(error));
      } finally {
        setConnecting(false);
      }
    },
    [baudrate, message, onSerialState, onStateRefresh, port],
  );

  const handleDisconnect = useCallback(async () => {
    setConnecting(true);
    try {
      const data = await disconnectSerial();
      onSerialState(data.state);
      message.success('串口已断开');
      onStateRefresh();
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setConnecting(false);
    }
  }, [message, onSerialState, onStateRefresh]);

  const handleClassChange = useCallback(
    (value: string) => {
      setClassName(value);
      const entry = classes?.[value];
      if (entry) {
        setSlot(entry.slot);
        setAngle(entry.angle);
        // 换类别时清掉上一类的手动角度覆盖，避免"以为是新类别的角度"
        setOverrideH(null);
        setOverrideV(null);
      }
    },
    [classes],
  );

  const handleSend = useCallback(async () => {
    if (!className.trim()) {
      message.warning('请输入或选择要分类的类别');
      return;
    }
    setSending(true);
    try {
      const data = await manualSort({
        class_name: className.trim(),
        slot,
        angle,
        force,
        ...(overrideH !== null ? { h: overrideH } : {}),
        ...(overrideV !== null ? { v: overrideV } : {}),
      });
      const resolved = data.result as SortEventData & { h?: number | null; v?: number | null };
      const angleText =
        resolved.h != null || resolved.v != null
          ? `H=${resolved.h ?? '默认'} V=${resolved.v ?? '默认'}（手动覆盖）`
          : `按 slot ${data.result.slot} 默认角度`;
      message.success(
        `已下发：${data.result.class_name} → ${data.result.group_label_zh}（slot ${data.result.slot}）/ ${angleText}`,
      );
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setSending(false);
    }
  }, [angle, className, force, message, overrideH, overrideV, slot]);

  const columns: TableProps<SerialPortInfo>['columns'] = [
    {
      title: '端口',
      dataIndex: 'device',
      key: 'device',
      width: 130,
      render: (value: string, record) => (
        <Space size={6}>
          <span className="mono">{value}</span>
          {record.likely_esp && (
            <Tooltip title={`命中优先级关键字，score=${record.score}`}>
              <Tag color="green" icon={<ThunderboltOutlined />}>
                likely ESP32
              </Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (value: string, record) => (
        <Tooltip title={record.hwid}>
          <span>{value || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 90,
      render: (_value, record) => (
        <Button
          size="small"
          type={connection?.port === record.device ? 'default' : 'primary'}
          disabled={connection?.port === record.device}
          loading={connecting}
          onClick={() => void handleConnect(record.device)}
        >
          {connection?.port === record.device ? '已连接' : '连接'}
        </Button>
      ),
    },
  ];

  return (
    <Card
      className="panel-card"
      title={
        <Space size={8}>
          <span className="panel-title">串口 / ESP32</span>
          <Badge
            status={connected ? (connection?.ready ? 'success' : 'processing') : 'default'}
            text={connected ? `${connection?.port}${connection?.ready ? ' · ready' : ''}` : '未连接'}
            className="panel-badge"
          />
        </Space>
      }
      extra={
        <Button size="small" icon={<ReloadOutlined />} loading={scanning} onClick={() => void scan()}>
          扫描串口
        </Button>
      }
    >
      <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
        <Select
          style={{ flex: 1 }}
          placeholder="串口（自动按 CP210x / CH340 / ESP32 优先排序）"
          value={port}
          onChange={(value: string) => setPort(value)}
          options={ports.map((item) => ({
            value: item.device,
            label: `${item.device} · ${item.description || '未知设备'}${item.likely_esp ? ' ★' : ''}`,
          }))}
          notFoundContent={scanning ? '扫描中…' : '未发现串口'}
        />
        <Select
          style={{ width: 116 }}
          value={baudrate}
          onChange={(value: number) => setBaudrate(value)}
          options={BAUDRATES.map((value) => ({ value, label: `${value}` }))}
        />
        {connected ? (
          <Button danger icon={<DisconnectOutlined />} loading={connecting} onClick={() => void handleDisconnect()}>
            断开
          </Button>
        ) : (
          <Button type="primary" icon={<ApiOutlined />} loading={connecting} onClick={() => void handleConnect()}>
            连接
          </Button>
        )}
      </Space.Compact>

      <Table<SerialPortInfo>
        size="small"
        rowKey="device"
        columns={columns}
        dataSource={ports}
        loading={scanning}
        pagination={false}
        scroll={{ y: 132 }}
        locale={{ emptyText: '未发现串口，请检查 ESP32 是否已插入' }}
        rowClassName={(record) => (record.likely_esp ? 'row-active' : '')}
      />

      <Descriptions size="small" column={3} colon={false} className="compact-descriptions">
        <Descriptions.Item label="状态">
          {connected ? <Tag color="green">已连接</Tag> : <Tag>未连接</Tag>}
        </Descriptions.Item>
        <Descriptions.Item label="节点">
          {connection?.last_status ? <Tag color="blue">{connection.last_status}</Tag> : '-'}
        </Descriptions.Item>
        <Descriptions.Item label="收发计数">
          {serialState?.tx_count ?? 0} / {serialState?.rx_count ?? 0}
        </Descriptions.Item>
      </Descriptions>

      <div className="section-title">
        <span>手动发送测试分类</span>
        <span className="muted mono">{'{"cmd":"sort","class":"…","slot":0,"angle":90}'}</span>
      </div>
      <Space wrap size={8} style={{ marginBottom: 6 }}>
        <AutoComplete
          style={{ width: 260 }}
          popupMatchSelectWidth={360}
          value={className}
          options={classOptions}
          onChange={(value: string) => setClassName(value)}
          onSelect={(value: string) => handleClassChange(value)}
          placeholder="类别，如 battery"
          filterOption={(input, option) =>
            String(option?.value ?? '').toLowerCase().includes(input.toLowerCase())
          }
        />
        <InputNumber
          addonBefore="slot"
          min={0}
          max={15}
          value={slot}
          onChange={(value) => setSlot(Number(value ?? 0))}
          style={{ width: 108 }}
        />
        <Tooltip title="水平舵机角度覆盖（0~270）。留空 = 用固件里该 slot 的默认角度">
          <InputNumber
            addonBefore="H"
            min={0}
            max={270}
            value={overrideH}
            placeholder="默认"
            onChange={(value) => setOverrideH(value === null || value === undefined ? null : Number(value))}
            style={{ width: 118 }}
          />
        </Tooltip>
        <Tooltip title="竖直舵机角度覆盖（0~180）。留空 = 用固件里该 slot 的默认角度">
          <InputNumber
            addonBefore="V"
            min={0}
            max={180}
            value={overrideV}
            placeholder="默认"
            onChange={(value) => setOverrideV(value === null || value === undefined ? null : Number(value))}
            style={{ width: 118 }}
          />
        </Tooltip>
        <Tooltip title="忽略同类冷却时间">
          <Space size={4}>
            <Switch size="small" checked={force} onChange={setForce} />
            <span className="muted">忽略冷却</span>
          </Space>
        </Tooltip>
        <Button
          type="primary"
          icon={<SendOutlined />}
          loading={sending}
          disabled={!connected}
          onClick={() => void handleSend()}
        >
          发送
        </Button>
      </Space>
      <div className="muted serial-hint">
        类别只决定落到哪个<b>物理桶（slot）</b>：
        {currentSlotInfo ? `当前 ${className.trim()} → slot ${currentSlotInfo.slot}` : '先选一个类别'}
        {currentSlotInfo && currentSlotInfo.sameSlot.length > 0 && (
          <>；同桶的 {currentSlotInfo.sameSlot.join(' / ')} 动作完全一样，换它们看不出区别</>
        )}
        。固件按 slot 查表决定角度，协议里的 <span className="mono">angle</span> 固件不使用，
        要临时改角度请填上面的 <span className="mono">H / V</span>。
      </div>

      <div className="section-title">
        <span>串口报文</span>
      </div>
      <div className="serial-log">
        {log.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无收发记录" />
        ) : (
          <List
            size="small"
            dataSource={log.slice(0, 30)}
            renderItem={(item) => (
              <List.Item className="serial-log-item">
                <Tag color={item.dir === 'tx' ? 'blue' : item.dir === 'rx' ? 'green' : 'default'}>
                  {item.dir === 'tx' ? 'PC → ESP32' : item.dir === 'rx' ? 'ESP32 → PC' : 'LOG'}
                </Tag>
                <span className="mono log-text">{item.text}</span>
                <span className="muted log-time">
                  {new Date(item.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })}
                </span>
              </List.Item>
            )}
          />
        )}
      </div>
    </Card>
  );
}
