import { useCallback, useEffect, useMemo, useState } from 'react';
import DatasetBrowser from './DatasetBrowser';
import {
  App as AntApp,
  Button,
  Card,
  DatePicker,
  Drawer,
  Empty,
  Input,
  Modal,
  Pagination,
  Segmented,
  Select,
  Space,
  Row,
  Col,
  Spin,
  Table,
  Tag,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  CheckCircleOutlined,
  CloudDownloadOutlined,
  CloseCircleOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  ReloadOutlined,
} from '@ant-design/icons';

// ---------------------------------------------------------------------------
// 请求层（自带，避免改动 api.ts）
// ---------------------------------------------------------------------------
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* 忽略解析失败 */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------
interface SortRecordItem {
  id: number;
  created_at: string | null;
  source: string;
  session_id: string | null;
  class_name: string;
  label_zh: string;
  confidence: number | null;
  bbox: { x1: number; y1: number; x2: number; y2: number } | null;
  model_name: string | null;
  bucket_slot: number;
  bucket_label: string;
  angle: number;
  serial_sent: boolean;
  reason: string;
  verified: 'pending' | 'correct' | 'wrong' | string;
  true_class: string | null;
  verified_at: string | null;
  in_dataset: boolean;
  dataset_image_id: number | null;
  has_image: boolean;
}

interface RecordListResponse {
  total: number;
  page: number;
  size: number;
  pages: number;
  items: SortRecordItem[];
}

interface DbStatus {
  available: boolean;
  version?: string;
  database?: string;
  records?: number;
  error?: string | null;
  recorder?: { enabled: boolean; written: number; failed: number; queued: number };
}

interface StatsResponse {
  days: number;
  total: number;
  serial_failed: number;
  by_class: { class_name: string; label_zh: string; count: number }[];
  by_bucket: { slot: number; label: string; count: number }[];
  by_day: { day: string; count: number }[];
  by_verified: Record<string, number>;
}

interface Props {
  classes: Record<string, { label_zh?: string; slot?: number }>;
  onStateRefresh?: () => void;
}

const PAGE_SIZES = [10, 20, 50, 100];

export default function RecordsPanel({ classes }: Props) {
  const { message } = AntApp.useApp();

  const [status, setStatus] = useState<DbStatus | null>(null);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [items, setItems] = useState<SortRecordItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState({
    class_name: undefined as string | undefined,
    source: undefined as string | undefined,
    verified: undefined as string | undefined,
    in_dataset: undefined as boolean | undefined,
    q: '',
    date_from: undefined as string | undefined,
    date_to: undefined as string | undefined,
  });
  const [detail, setDetail] = useState<SortRecordItem | null>(null);
  const [wrongFor, setWrongFor] = useState<SortRecordItem | null>(null);
  const [wrongClass, setWrongClass] = useState<string | undefined>(undefined);
  const [busyId, setBusyId] = useState<number | null>(null);

  // 统计与训练集索引（P1）
  const [dataset, setDataset] = useState<{
    total: number;
    classes: { class_name: string; count: number; labeled: number; unlabeled: number }[];
  } | null>(null);
  const [backfilling, setBackfilling] = useState(false);

  const classOptions = useMemo(() => {
    const options = Object.entries(classes ?? {}).map(([name, entry]) => ({
      value: name,
      label: entry?.label_zh ? `${entry.label_zh}（${name}）` : name,
    }));
    // 7 类之外的人工纠正 / 无法归类的情况
    options.push({ value: 'other', label: '其它（other）' });
    options.push({ value: 'unassigned', label: '未归类（待处理）' });
    return options;
  }, [classes]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      params.set('page', String(page));
      params.set('size', String(size));
      if (filters.class_name) params.set('class_name', filters.class_name);
      if (filters.source) params.set('source', filters.source);
      if (filters.verified) params.set('verified', filters.verified);
      if (filters.in_dataset !== undefined) params.set('in_dataset', String(filters.in_dataset));
      if (filters.q.trim()) params.set('q', filters.q.trim());
      if (filters.date_from) params.set('date_from', filters.date_from);
      if (filters.date_to) params.set('date_to', filters.date_to);

      // 只查列表：翻页/改筛选时不再连带重查统计与数据库状态
      const list = await call<RecordListResponse>(`/api/records?${params.toString()}`);
      setItems(list.items ?? []);
      setTotal(list.total ?? 0);
    } catch (error) {
      message.error(`读取记录失败：${(error as Error).message}`);
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [page, size, filters, message]);

  useEffect(() => {
    void load();
  }, [load]);

  // 侧栏数据（统计 + 数据库状态）：只在挂载与手动刷新时查
  const loadSide = useCallback(async () => {
    try {
      const [st, dbStatus] = await Promise.all([
        call<StatsResponse>('/api/records/stats?days=7').catch(() => null),
        call<DbStatus>('/api/db/status').catch(() => null),
      ]);
      if (st) setStats(st);
      if (dbStatus) setStatus(dbStatus);
    } catch {
      /* 侧栏失败不影响列表 */
    }
  }, []);

  useEffect(() => {
    void loadSide();
  }, [loadSide]);

  // ---------------------------------------------------------------- 操作
  const verify = useCallback(
    async (row: SortRecordItem, verified: 'correct' | 'wrong', trueClass?: string) => {
      setBusyId(row.id);
      try {
        const updated = await call<SortRecordItem>(`/api/records/${row.id}/verify`, {
          method: 'PATCH',
          body: JSON.stringify({ verified, true_class: trueClass ?? null }),
        });
        setItems((prev) => prev.map((item) => (item.id === row.id ? { ...item, ...updated } : item)));
        message.success(verified === 'correct' ? `#${row.id} 已标记为识别正确` : `#${row.id} 已纠正为 ${trueClass}`);
      } catch (error) {
        message.error(`判定失败：${(error as Error).message}`);
      } finally {
        setBusyId(null);
      }
    },
    [message],
  );

  const promote = useCallback(
    async (row: SortRecordItem) => {
      setBusyId(row.id);
      try {
        const res = await call<SortRecordItem & { already?: boolean; saved_path?: string }>(
          `/api/records/${row.id}/promote`,
          { method: 'POST', body: JSON.stringify({}) },
        );
        setItems((prev) => prev.map((item) => (item.id === row.id ? { ...item, ...res } : item)));
        message.success(res.already ? `#${row.id} 已经在训练集里` : `#${row.id} 已加入训练集：${res.saved_path ?? ''}`);
      } catch (error) {
        message.error(`加入训练集失败：${(error as Error).message}`);
      } finally {
        setBusyId(null);
      }
    },
    [message],
  );

  const removeRecord = useCallback(
    async (row: SortRecordItem) => {
      setBusyId(row.id);
      try {
        await call(`/api/records/${row.id}`, { method: 'DELETE' });
        setItems((prev) => prev.filter((item) => item.id !== row.id));
        setTotal((prev) => Math.max(0, prev - 1));
        message.success(`记录 #${row.id} 已删除`);
      } catch (error) {
        message.error(`删除失败：${(error as Error).message}`);
      } finally {
        setBusyId(null);
      }
    },
    [message],
  );

  const exportCsv = useCallback(() => {
    const params = new URLSearchParams();
    if (filters.date_from) params.set('date_from', filters.date_from);
    if (filters.date_to) params.set('date_to', filters.date_to);
    window.open(`/api/records-export.csv?${params.toString()}`, '_blank');
  }, [filters.date_from, filters.date_to]);

  const loadDataset = useCallback(async () => {
    try {
      setDataset(
        await call<{
          total: number;
          classes: { class_name: string; count: number; labeled: number; unlabeled: number }[];
        }>('/api/dataset/summary'),
      );
    } catch {
      /* 尚未回填时忽略 */
    }
  }, []);

  const backfill = useCallback(async () => {
    setBackfilling(true);
    try {
      const res = await call<{ scanned: number; inserted: number; updated: number; skipped: number }>(
        '/api/dataset/backfill',
        { method: 'POST', body: JSON.stringify({}) },
      );
      message.success(`回填完成：扫描 ${res.scanned}，新增 ${res.inserted}，更新 ${res.updated}，跳过 ${res.skipped}`);
      await loadDataset();
    } catch (error) {
      message.error(`回填失败：${(error as Error).message}`);
    } finally {
      setBackfilling(false);
    }
  }, [loadDataset, message]);

  useEffect(() => {
    void loadDataset();
  }, [loadDataset]);

  // ---------------------------------------------------------------- 表格
  const columns: ColumnsType<SortRecordItem> = [
    {
      title: '缩略图',
      dataIndex: 'id',
      width: 92,
      align: 'center',
      render: (_: unknown, row) =>
        row.has_image ? (
          <img
            src={`/api/records/${row.id}/image?kind=thumb`}
            alt={`记录 ${row.id}`}
            style={{ width: 72, height: 44, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--hud-line)' }}
            onClick={() => setDetail(row)}
          />
        ) : (
          <span style={{ opacity: 0.4, fontSize: 12 }}>无图</span>
        ),
    },
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 150,
      render: (v: string | null) => <span className="mono">{v ?? '-'}</span>,
    },
    {
      title: '识别结果',
      dataIndex: 'class_name',
      width: 130,
      render: (_: unknown, row) => (
        <Space size={4} direction="vertical">
          <span>{row.label_zh || row.class_name || '-'}</span>
          <span style={{ fontSize: 11, opacity: 0.6 }}>{row.class_name}</span>
        </Space>
      ),
    },
    {
      title: '置信度',
      dataIndex: 'confidence',
      width: 84,
      align: 'right',
      render: (v: number | null) => (v == null ? '-' : `${(v * 100).toFixed(1)}%`),
    },
    {
      title: '桶位',
      dataIndex: 'bucket_slot',
      width: 96,
      align: 'center',
      render: (_: unknown, row) => <Tag color="green">slot {row.bucket_slot}</Tag>,
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 76,
      align: 'center',
      render: (v: string) => <Tag>{v === 'manual' ? '手动' : '自动'}</Tag>,
    },
    {
      title: '串口',
      dataIndex: 'serial_sent',
      width: 78,
      align: 'center',
      render: (v: boolean) => (v ? <Tag color="green">已下发</Tag> : <Tag color="orange">未下发</Tag>),
    },
    {
      title: '人工判定',
      dataIndex: 'verified',
      width: 128,
      render: (_: unknown, row) => {
        if (row.verified === 'correct') return <Tag color="green">正确</Tag>;
        if (row.verified === 'wrong')
          return (
            <Tooltip title={`模型识别 ${row.class_name} → 人工纠正 ${row.true_class ?? '?'}`}>
              <Tag color="red">已纠正 → {row.true_class ?? '?'}</Tag>
            </Tooltip>
          );
        return <Tag>未判定</Tag>;
      },
    },
    {
      title: '训练集',
      dataIndex: 'in_dataset',
      width: 86,
      align: 'center',
      render: (v: boolean) => (v ? <Tag color="blue">已加入</Tag> : <Tag color="default">未加入</Tag>),
    },
    {
      title: '操作',
      key: 'actions',
      width: 320,
      render: (_: unknown, row) => (
        <Space size={4} wrap>
          <Button size="small" type="text" onClick={() => setDetail(row)}>
            查看
          </Button>
          <Button
            size="small"
            icon={<CheckCircleOutlined />}
            loading={busyId === row.id}
            onClick={() => void verify(row, 'correct')}
          >
            正确
          </Button>
          <Button
            size="small"
            danger
            icon={<CloseCircleOutlined />}
            onClick={() => {
              setWrongFor(row);
              setWrongClass(row.true_class ?? undefined);
            }}
          >
            错误
          </Button>
          <Button
            size="small"
            type="primary"
            ghost
            icon={<ExperimentOutlined />}
            loading={busyId === row.id}
            disabled={!row.has_image || row.in_dataset}
            onClick={() => void promote(row)}
          >
            入训练集
          </Button>
          <Button size="small" danger loading={busyId === row.id} onClick={() => void removeRecord(row)}>
            删除
          </Button>
        </Space>
      ),
    },
  ];

  const bars = (rows: { label: string; value: number }[]) => {
    if (!rows.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />;
    const max = Math.max(1, ...rows.map((row) => row.value));
    return (
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        {rows.map((row) => (
          <div key={row.label} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
            <span style={{ width: 112, opacity: 0.78, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {row.label || '-'}
            </span>
            <div style={{ flex: 1, height: 8, background: 'var(--hud-line)', borderRadius: 4, overflow: 'hidden' }}>
              <div style={{ width: `${Math.round((row.value / max) * 100)}%`, height: '100%', background: 'var(--hud-accent)', borderRadius: 4 }} />
            </div>
            <span className="mono" style={{ width: 36, textAlign: 'right' }}>
              {row.value}
            </span>
          </div>
        ))}
      </Space>
    );
  };

  // ---------------------------------------------------------------- 渲染
  const verifiedCount = (v: string) => stats?.by_verified?.[v] ?? 0;

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        className="panel-card"
        size="small"
        title={
          <Space size={8}>
            <DatabaseOutlined />
            <span>分拣记录</span>
            <Tag color={status?.available ? 'green' : 'red'}>
              {status?.available ? `MySQL ${status.version ?? ''}` : '数据库未连接'}
            </Tag>
            <span style={{ fontSize: 12, opacity: 0.65 }}>
              {status?.recorder?.enabled ? `采集中 · 已写 ${status.recorder.written} 条` : '采集未启动'}
            </span>
          </Space>
        }
        extra={
          <Space size={8}>
            <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => { void load(); void loadSide(); void loadDataset(); }}>
              刷新
            </Button>
            <Button size="small" icon={<CloudDownloadOutlined />} onClick={exportCsv}>
              导出 CSV
            </Button>
          </Space>
        }
      >
        <Space size={8} wrap>
          <Segmented
            options={[
              { label: '全部', value: 'all' },
              { label: '未判定', value: 'pending' },
              { label: '正确', value: 'correct' },
              { label: '已纠正', value: 'wrong' },
            ]}
            value={filters.verified ?? 'all'}
            onChange={(value) =>
              setFilters((prev) => ({
                ...prev,
                verified: value === 'all' ? undefined : String(value),
              }))
            }
          />
          <Select
            allowClear
            size="small"
            placeholder="类别"
            style={{ width: 168 }}
            options={classOptions}
            value={filters.class_name}
            onChange={(value) => setFilters((prev) => ({ ...prev, class_name: value }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="来源"
            style={{ width: 110 }}
            options={[
              { value: 'auto', label: '自动' },
              { value: 'manual', label: '手动' },
            ]}
            value={filters.source}
            onChange={(value) => setFilters((prev) => ({ ...prev, source: value }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="训练集"
            style={{ width: 120 }}
            options={[
              { value: true, label: '已加入' },
              { value: false, label: '未加入' },
            ]}
            value={filters.in_dataset}
            onChange={(value) => setFilters((prev) => ({ ...prev, in_dataset: value ?? undefined }))}
          />
          <DatePicker.RangePicker
            size="small"
            onChange={(values) =>
              setFilters((prev) => ({
                ...prev,
                date_from: values?.[0] ? values[0].format('YYYY-MM-DD') : undefined,
                date_to: values?.[1] ? values[1].format('YYYY-MM-DD') : undefined,
              }))
            }
          />
          <Input.Search
            size="small"
            allowClear
            placeholder="搜索类别 / 桶名 / 原因"
            style={{ width: 200 }}
            value={filters.q}
            onChange={(event) => setFilters((prev) => ({ ...prev, q: event.target.value }))}
            onSearch={() => void load()}
          />
        </Space>

        <div style={{ marginTop: 12, marginBottom: 8, fontSize: 12, opacity: 0.75 }}>
          近 7 天：共 <b>{stats?.total ?? 0}</b> 次下发 · 串口失败 <b>{stats?.serial_failed ?? 0}</b> ·
          已判定正确 <b>{verifiedCount('correct')}</b> · 已纠正 <b>{verifiedCount('wrong')}</b> · 待判定{' '}
          <b>{verifiedCount('pending')}</b>
          {stats?.by_class?.length ? (
            <span>
              {' '}
              · 最多：
              {stats.by_class
                .slice(0, 3)
                .map((item) => `${item.label_zh || item.class_name}×${item.count}`)
                .join('，')}
            </span>
          ) : null}
        </div>

        <Spin spinning={loading}>
          <Table<SortRecordItem>
            size="small"
            rowKey="id"
            columns={columns}
            dataSource={items}
            pagination={false}
            scroll={{ x: 1200 }}
            locale={{ emptyText: <Empty description="还没有分拣记录（识别并下发一次就会出现）" /> }}
          />
        </Spin>

        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 12 }}>
          <Pagination
            current={page}
            pageSize={size}
            total={total}
            showSizeChanger
            pageSizeOptions={PAGE_SIZES.map(String)}
            showTotal={(count) => `共 ${count} 条`}
            onChange={(nextPage, nextSize) => {
              setPage(nextPage);
              setSize(nextSize);
            }}
          />
        </div>
      </Card>

      {/* 统计 · 训练集索引 */}
      <Card
        className="panel-card"
        size="small"
        title={
          <Space size={8}>
            <ExperimentOutlined />
            <span>统计 · 训练集索引</span>
            <Tag>共 {dataset?.total ?? 0} 张已入索引</Tag>
          </Space>
        }
        extra={
          <Space size={8}>
            <Button size="small" loading={backfilling} onClick={() => void backfill()}>
              回填历史抓拍
            </Button>
            <Button size="small" onClick={() => void loadDataset()}>
              刷新索引
            </Button>
          </Space>
        }
      >
        <Row gutter={[16, 16]}>
          <Col xs={24} xl={12}>
            <div className="section-title">近 {stats?.days ?? 7} 天 · 按类别</div>
            {bars((stats?.by_class ?? []).map((item) => ({ label: item.label_zh || item.class_name, value: item.count })))}
            <div className="section-title" style={{ marginTop: 14 }}>按物理桶</div>
            {bars((stats?.by_bucket ?? []).map((item) => ({ label: `slot ${item.slot} · ${item.label}`, value: item.count })))}
          </Col>
          <Col xs={24} xl={12}>
            <div className="section-title">按天</div>
            {bars((stats?.by_day ?? []).map((item) => ({ label: item.day, value: item.count })))}
            <div className="section-title" style={{ marginTop: 14 }}>训练集内容（可改类别 / 删除，支持分页浏览）</div>
            <DatasetBrowser classes={classes} onChanged={() => void loadDataset()} />
          </Col>
        </Row>
      </Card>

      {/* 详情抽屉：带框大图 + 原始帧 + 全部字段 */}
      <Drawer
        width={720}
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={detail ? `记录 #${detail.id} · ${detail.label_zh || detail.class_name}` : ''}
      >
        {detail ? (
          <Space direction="vertical" size={14} style={{ width: '100%' }}>
            <div>
              <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 6 }}>识别帧（带检测框）</div>
              {detail.has_image ? (
                <img
                  src={`/api/records/${detail.id}/image?kind=marked`}
                  alt="marked"
                  style={{ width: '100%', borderRadius: 10, border: '1px solid var(--hud-line)' }}
                />
              ) : (
                <Empty description="无图片" />
              )}
            </div>
            {detail.has_image ? (
              <div>
                <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 6 }}>原始帧（无框，可直接回流训练）</div>
                <img
                  src={`/api/records/${detail.id}/image?kind=raw`}
                  alt="raw"
                  style={{ width: '100%', borderRadius: 10, border: '1px solid var(--hud-line)' }}
                />
              </div>
            ) : null}
            <Table
              size="small"
              pagination={false}
              showHeader={false}
              columns={[
                { dataIndex: 'k', width: 130 },
                { dataIndex: 'v' },
              ]}
              dataSource={[
                { key: 'time', k: '时间', v: detail.created_at ?? '-' },
                { key: 'cls', k: '预测类别', v: `${detail.label_zh}（${detail.class_name}）` },
                { key: 'conf', k: '置信度', v: detail.confidence == null ? '-' : detail.confidence.toFixed(4) },
                {
                  key: 'bbox',
                  k: 'bbox',
                  v: detail.bbox ? `(${detail.bbox.x1}, ${detail.bbox.y1}) → (${detail.bbox.x2}, ${detail.bbox.y2})` : '-',
                },
                { key: 'src', k: '来源', v: detail.source === 'manual' ? '手动下发' : '自动识别' },
                { key: 'slot', k: '桶位', v: `slot ${detail.bucket_slot} · ${detail.bucket_label}` },
                { key: 'angle', k: '角度', v: String(detail.angle) },
                { key: 'serial', k: '串口下发', v: detail.serial_sent ? '成功' : `未下发（${detail.reason || '-'}）` },
                { key: 'ver', k: '人工判定', v: detail.verified === 'pending' ? '未判定' : detail.verified === 'correct' ? '正确' : `已纠正 → ${detail.true_class ?? '?'}` },
                { key: 'ds', k: '训练集', v: detail.in_dataset ? `已加入（dataset #${detail.dataset_image_id ?? '?'}）` : '未加入' },
                { key: 'model', k: '模型', v: detail.model_name ?? '-' },
                { key: 'sess', k: '会话', v: detail.session_id ?? '-' },
              ]}
            />
          </Space>
        ) : null}
      </Drawer>

      {/* 判定“错误”时选择真实类别 */}
      <Modal
        open={wrongFor !== null}
        title={wrongFor ? `#${wrongFor.id} 人工纠正：模型识别为 ${wrongFor.label_zh || wrongFor.class_name}` : ''}
        onCancel={() => setWrongFor(null)}
        onOk={() => {
          if (!wrongFor) return;
          if (!wrongClass) {
            message.warning('请选择真实类别');
            return;
          }
          void verify(wrongFor, 'wrong', wrongClass).then(() => setWrongFor(null));
        }}
        okText="保存纠正"
        cancelText="取消"
      >
        <div style={{ marginBottom: 8 }}>选择这张图真正的类别（写入训练集时会用这个类别归档）：</div>
        <Select
          style={{ width: '100%' }}
          placeholder="真实类别"
          options={classOptions}
          value={wrongClass}
          onChange={setWrongClass}
        />
      </Modal>
    </Space>
  );
}
