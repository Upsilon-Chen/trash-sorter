import { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Badge,
  Card,
  Empty,
  List,
  Segmented,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  type TableProps,
} from 'antd';
import { AimOutlined, CheckCircleTwoTone, ClockCircleOutlined, ThunderboltOutlined } from '@ant-design/icons';

import {
  errorText,
  setMultiObjectPolicy,
  type DetectionEventData,
  type DetectionResult,
  type DetectionSnapshot,
  type SortEventData,
} from '../api';
import { BUCKET_COLORS } from '../theme';
import { BarList, ConfidenceBar, DonutChart, Sparkline } from './Charts';
import { HudRing } from './Hud';

export interface ResultPanelProps {
  /** 最近一次 WS detection 推送 */
  detection: DetectionEventData | null;
  /** 后端全量快照（含计数、冷却、类别与物理桶映射） */
  snapshot: DetectionSnapshot | null;
  /** 前端显示阈值：低于该值的结果不展示 */
  confidence: number;
  /** 最近的分类下发记录 */
  history: SortEventData[];
}

/** 多目标策略的中文说明（与后端 detection.multi_object 对应） */
const MULTI_POLICY_LABEL: Record<string, string> = {
  priority: '按物理桶优先级（有害垃圾最优先）',
  largest: '取画面里面积最大的',
  confidence: '取置信度最高的',
  hold: '不下发，等人工处理',
};

/** 切换控件上的短标签 */
const MULTI_POLICY_SHORT: Record<string, string> = {
  priority: '优先级',
  largest: '最大件',
  confidence: '置信度',
  hold: '暂停',
};

/**
 * ResultPanel：通过 WS /ws 展示当前类别、置信度、bbox、串口发送状态与历史统计。
 * 注意这里是两层：识别类别（6 类）→ 物理桶（4 个 slot）。
 */
export default function ResultPanel({ detection, snapshot, confidence, history }: ResultPanelProps) {
  const { message } = AntApp.useApp();
  const [policyBusy, setPolicyBusy] = useState(false);
  const running = Boolean(snapshot?.running);

  // 当前策略：优先用本次检测事件里的，其次用后端快照
  const policy = detection?.multi?.policy ?? snapshot?.multi_object ?? 'priority';

  const handlePolicyChange = useCallback(
    async (value: string) => {
      setPolicyBusy(true);
      try {
        const data = await setMultiObjectPolicy(value);
        message.success(
          `多目标策略已切到「${MULTI_POLICY_SHORT[value] ?? value}」` +
            (data.persisted ? '（已写回 settings.yaml）' : '（仅本次运行生效）'),
        );
      } catch (error) {
        message.error(errorText(error));
      } finally {
        setPolicyBusy(false);
      }
    },
    [message],
  );

  const results = useMemo(
    () => (detection?.results ?? []).filter((item) => item.confidence >= confidence),
    [confidence, detection],
  );
  const best: DetectionResult | null = results[0] ?? null;

  const labelOf = (name: string): string => snapshot?.classes?.[name]?.label_zh ?? name;
  const slotOf = (name: string): number | undefined => snapshot?.classes?.[name]?.slot;
  const groupLabelOfSlot = (slot: number | string): string => {
    const key = String(slot);
    return snapshot?.groups?.[key]?.label_zh ?? `slot ${key}`;
  };
  const groupLabelOfClass = (name: string): string | undefined => {
    const slot = slotOf(name);
    return slot === undefined ? undefined : groupLabelOfSlot(slot);
  };

  const cooldowns = Object.entries(snapshot?.cooldowns ?? {}).filter(([, remaining]) => remaining > 0);

  /** 物理桶固定配色：同一个桶在环形图、条形图、置信度条里始终是同一个颜色 */
  const bucketColor = useCallback((slot: number) => BUCKET_COLORS[slot % BUCKET_COLORS.length], []);

  const bucketData = useMemo(() => {
    const counts = snapshot?.group_counts ?? {};
    const slots = new Set<number>([
      ...Object.keys(snapshot?.groups ?? {}).map((key) => Number(key)),
      ...Object.keys(counts).map((key) => Number(key)),
    ]);
    return [...slots]
      .filter((slot) => Number.isFinite(slot))
      .sort((a, b) => a - b)
      .map((slot) => ({
        label: groupLabelOfSlot(slot),
        value: Number(counts[String(slot)] ?? 0),
        color: bucketColor(slot),
      }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bucketColor, snapshot?.group_counts, snapshot?.groups]);

  const classData = useMemo(
    () =>
      Object.entries(snapshot?.counts ?? {})
        .map(([name, value]) => ({
          label: labelOf(name),
          value: Number(value),
          color: bucketColor(slotOf(name) ?? 0),
        }))
        .sort((a, b) => b.value - a.value),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [bucketColor, snapshot?.counts],
  );

  /** 近 10 分钟的分拣节奏（每分钟一个柱子，最后一个是当前分钟） */
  const trend = useMemo(() => {
    const bins = new Array<number>(10).fill(0);
    const now = Date.now() / 1000;
    for (const item of history) {
      const age = now - item.ts;
      if (age < 0 || age >= 600) continue;
      const index = 9 - Math.floor(age / 60);
      if (index >= 0) bins[index] += 1;
    }
    return bins;
  }, [history]);

  const columns: TableProps<DetectionResult>['columns'] = [
    {
      title: '识别类别 → 物理桶',
      dataIndex: 'class_name',
      key: 'class_name',
      render: (value: string, record) => (
        <Space size={6} wrap>
          <Tag color="geekblue">{labelOf(value)}</Tag>
          <span className="mono muted">{value}</span>
          {slotOf(value) !== undefined && (
            <Tag color="cyan" icon={<ThunderboltOutlined />}>
              slot {slotOf(value)} · {groupLabelOfClass(value)}
            </Tag>
          )}
          {record.serial_sent && <Tag color="green">已下发</Tag>}
        </Space>
      ),
    },
    {
      title: '置信度',
      dataIndex: 'confidence',
      key: 'confidence',
      width: 128,
      render: (value: number) => <ConfidenceBar value={value} />,
    },
    {
      title: 'bbox (x1, y1, x2, y2)',
      dataIndex: 'bbox',
      key: 'bbox',
      width: 190,
      render: (value: number[]) => <span className="mono">{`[${value.join(', ')}]`}</span>,
    },
    {
      title: '串口',
      dataIndex: 'serial_sent',
      key: 'serial_sent',
      width: 96,
      render: (value: boolean) =>
        value ? (
          <Tag icon={<CheckCircleTwoTone twoToneColor="#52c41a" />} color="green">
            已发送
          </Tag>
        ) : (
          <Tag>未发送</Tag>
        ),
    },
  ];

  return (
    <Card
      className="panel-card"
      title={
        <Space size={8}>
          <span className="panel-title">
            <AimOutlined />
            识别结果
          </span>
          <Badge status={running ? 'processing' : 'default'} text={running ? '检测中' : '已停止'} className="panel-badge" />
        </Space>
      }
      extra={
        <Space size={16}>
          <Statistic title="推理 FPS" value={detection?.fps ?? snapshot?.fps ?? 0} precision={1} valueStyle={{ fontSize: 16 }} />
          <Statistic
            title="单帧耗时"
            value={detection?.inference_ms ?? snapshot?.model?.last_inference_ms ?? 0}
            precision={1}
            suffix="ms"
            valueStyle={{ fontSize: 16 }}
          />
          <Statistic title="累计帧" value={snapshot?.frames ?? 0} valueStyle={{ fontSize: 16 }} />
        </Space>
      }
    >
      <div className="multi-policy-bar">
        <Space size={8} wrap>
          <Tooltip title="一帧里同时检测到多个不同类别的垃圾时倒哪一个。托盘一次只能往一个方向倒，所以必然有一件会被分错，这个策略决定牺牲哪一个。">
            <span className="muted">多目标策略</span>
          </Tooltip>
          <Segmented
            size="small"
            value={policy}
            disabled={policyBusy}
            onChange={(value) => void handlePolicyChange(String(value))}
            options={[
              { value: 'priority', label: MULTI_POLICY_SHORT.priority },
              { value: 'largest', label: MULTI_POLICY_SHORT.largest },
              { value: 'confidence', label: MULTI_POLICY_SHORT.confidence },
              { value: 'hold', label: MULTI_POLICY_SHORT.hold },
            ]}
          />
          <span className="muted" style={{ fontSize: 12 }}>
            {MULTI_POLICY_LABEL[policy] ?? policy}
          </span>
        </Space>
      </div>

      {detection?.multi && detection.multi.count >= 2 && (
        <Alert
          type={detection.multi.held ? 'error' : 'warning'}
          showIcon
          style={{ marginBottom: 12 }}
          message={
            detection.multi.held
              ? `画面里有 ${detection.multi.count} 个不同类别的垃圾：已暂停下发`
              : `画面里有 ${detection.multi.count} 个不同类别的垃圾：倒向「${
                  detection.multi.chosen_group_zh ?? '未知桶'
                }」`
          }
          description={
            <div style={{ fontSize: 12, lineHeight: 1.7 }}>
              <div>
                {detection.multi.objects
                  .map(
                    (item) =>
                      `${item.label_zh}（${item.class_name} · ${Math.round(item.area * 100)}% · 置信 ${
                        item.confidence
                      }）`,
                  )
                  .join('　｜　')}
              </div>
              <div style={{ marginTop: 4 }}>
                当前策略：
                {MULTI_POLICY_LABEL[detection.multi.policy] ?? detection.multi.policy}
                {detection.multi.held
                  ? '。托盘一次只能往一个方向倒，请每次只放一件垃圾；也可以把 settings.yaml 的 multi_object 改成 priority / largest / confidence。'
                  : '。托盘一次只能往一个方向倒，另一件会跟着进同一个桶——建议一次只放一件。'}
              </div>
            </div>
          }
        />
      )}

      <div className="result-hero">
        {best && (
          <span
            className="hero-ripple"
            key={`${best.class_name}-${best.bbox.join('-')}-${best.confidence}`}
          />
        )}
        <div className="result-main">
          {best ? (
            <>
              <div className="result-label">{labelOf(best.class_name)}</div>
              <div className="result-sub mono">{best.class_name}</div>
              <Space size={6} wrap style={{ marginTop: 8 }}>
                {slotOf(best.class_name) !== undefined && (
                  <Tag color="cyan">
                    → slot {slotOf(best.class_name)} · {groupLabelOfClass(best.class_name)}
                  </Tag>
                )}
                <Tag className="mono">{`bbox [${best.bbox.join(', ')}]`}</Tag>
                {best.serial_sent ? (
                  <Tag color="green" icon={<ThunderboltOutlined />}>
                    已发送串口
                  </Tag>
                ) : (
                  <Tag color="default">未发送串口</Tag>
                )}
              </Space>
            </>
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={running ? '视野内没有识别到垃圾' : '点击顶部「开始检测」'}
            />
          )}
        </div>
        <div className="result-gauge">
          <HudRing value={best?.confidence ?? 0} size={136} caption="置信度" />
        </div>
      </div>

      <Table<DetectionResult>
        size="small"
        rowKey={(record) => `${record.class_name}-${record.bbox.join('-')}`}
        columns={columns}
        dataSource={results}
        pagination={false}
        scroll={{ y: 168 }}
        locale={{ emptyText: running ? '暂无满足阈值的目标' : '未开始检测' }}
      />

      <div className="section-title">
        <span>分类统计</span>
        <span className="muted">冷却按物理桶计算</span>
      </div>
      <div className="chart-block">
        <div className="chart-block-title">
          <span>分拣统计</span>
          <span className="muted">环形=物理桶占比　条形=识别类别次数</span>
        </div>
        <div className="chart-grid">
          <DonutChart data={bucketData} centerLabel="累计分拣" />
          <BarList data={classData} />
        </div>
        <Sparkline points={trend} label="近 10 分钟分拣节奏（每分钟）" />
      </div>

      {cooldowns.length > 0 && (
        <div className="count-tags" style={{ marginTop: 8 }}>
          <span className="muted count-tag-label">冷却中</span>
          {cooldowns.map(([slot, remaining]) => (
            <Tooltip key={`cd-${slot}`} title={`${groupLabelOfSlot(slot)} 冷却中，剩余 ${remaining.toFixed(1)}s`}>
              <Tag icon={<ClockCircleOutlined />} color="orange" className="count-tag">
                {groupLabelOfSlot(slot)} {remaining.toFixed(1)}s
              </Tag>
            </Tooltip>
          ))}
        </div>
      )}

      <div className="section-title">
        <span>分类下发记录</span>
        <span className="muted">{history.length} 条</span>
      </div>
      <div className="history-box">
        {history.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无下发记录" />
        ) : (
          <List
            size="small"
            dataSource={history}
            renderItem={(item) => (
              <List.Item className="history-item">
                <Space size={6} wrap>
                  <Tag color={item.manual ? 'purple' : 'blue'}>{item.manual ? '手动' : '自动'}</Tag>
                  <Tag color={item.serial_sent ? 'green' : 'red'}>{item.serial_sent ? '串口已发送' : '未发送'}</Tag>
                  <span className="history-class">{item.label_zh || item.class_name}</span>
                  <span className="mono muted">{item.class_name}</span>
                </Space>
                <Space size={8}>
                  <Tag color="cyan" className="count-tag">
                    {item.group_label_zh ?? groupLabelOfSlot(item.slot)}
                  </Tag>
                  <span className="muted log-time">{item.time?.replace('T', ' ') ?? ''}</span>
                </Space>
              </List.Item>
            )}
          />
        )}
      </div>

      {snapshot?.last_error && <div className="error-line">最近异常：{snapshot.last_error}</div>}
    </Card>
  );
}
