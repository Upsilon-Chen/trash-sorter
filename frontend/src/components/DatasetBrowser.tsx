import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  App as AntApp,
  Button,
  Drawer,
  Empty,
  Input,
  Pagination,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { DeleteOutlined, FolderOpenOutlined, ReloadOutlined } from '@ant-design/icons';

// ---------------------------------------------------------------------------
// 训练集浏览器：查看具体内容 + 改类别 / 改划分 / 删除（增删改查）
// ---------------------------------------------------------------------------
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...init });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

interface DatasetItem {
  id: number;
  path: string;
  class_name: string;
  split: string;
  source: string;
  labeled: boolean;
  width: number;
  height: number;
  record_id: number | null;
  created_at: string | null;
  note: string | null;
}

interface ListResponse {
  total: number;
  page: number;
  size: number;
  items: DatasetItem[];
}

interface Props {
  classes: Record<string, { label_zh?: string }>;
  onChanged?: () => void;
}

const SPLITS = [
  { value: 'unsplit', label: '未划分' },
  { value: 'train', label: 'train' },
  { value: 'val', label: 'val' },
  { value: 'test', label: 'test' },
];

export default function DatasetBrowser({ classes, onChanged }: Props) {
  const { message } = AntApp.useApp();
  const [items, setItems] = useState<DatasetItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(12);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);
  const [preview, setPreview] = useState<DatasetItem | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [splitting, setSplitting] = useState(false);
  const [filters, setFilters] = useState({
    class_name: undefined as string | undefined,
    split: undefined as string | undefined,
    labeled: undefined as boolean | undefined,
    q: '',
  });

  const classOptions = useMemo(() => {
    const options = Object.entries(classes ?? {}).map(([name, entry]) => ({
      value: name,
      label: entry?.label_zh ? `${entry.label_zh}（${name}）` : name,
    }));
    // 历史空类别统一归到 unassigned，方便人工改回真实类别
    options.push({ value: 'other', label: '其它（other）' });
    if (!options.some((item) => item.value === 'unassigned')) {
      options.push({ value: 'unassigned', label: '未归类（待处理）' });
    }
    return options;
  }, [classes]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      params.set('page', String(page));
      params.set('size', String(size));
      if (filters.class_name) params.set('class_name', filters.class_name);
      if (filters.split) params.set('split', filters.split);
      if (filters.labeled !== undefined) params.set('labeled', String(filters.labeled));
      if (filters.q.trim()) params.set('q', filters.q.trim());
      const data = await call<ListResponse>(`/api/dataset/images?${params.toString()}`);
      setItems(data.items ?? []);
      setTotal(data.total ?? 0);
    } catch (error) {
      message.error(`读取训练集失败：${(error as Error).message}`);
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [page, size, filters, message]);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = useCallback(
    async (row: DatasetItem, body: Record<string, unknown>) => {
      setBusy(row.id);
      try {
        await call(`/api/dataset/images/${row.id}`, { method: 'PATCH', body: JSON.stringify(body) });
        setItems((prev) => prev.map((item) => (item.id === row.id ? { ...item, ...body } as DatasetItem : item)));
        message.success(`#${row.id} 已更新`);
        onChanged?.();
      } catch (error) {
        message.error(`更新失败：${(error as Error).message}`);
      } finally {
        setBusy(null);
      }
    },
    [message, onChanged],
  );

  const remove = useCallback(
    async (row: DatasetItem, deleteFile: boolean) => {
      setBusy(row.id);
      try {
        const res = await call<{ removed_file: boolean }>(
          `/api/dataset/images/${row.id}?delete_file=${deleteFile ? 'true' : 'false'}`,
          { method: 'DELETE' },
        );
        setItems((prev) => prev.filter((item) => item.id !== row.id));
        setTotal((prev) => Math.max(0, prev - 1));
        message.success(`#${row.id} 已从索引移除${res.removed_file ? '，磁盘文件已删' : ''}`);
        onChanged?.();
      } catch (error) {
        message.error(`删除失败：${(error as Error).message}`);
      } finally {
        setBusy(null);
      }
    },
    [message, onChanged],
  );

  const doSplit = useCallback(
    async (body: Record<string, unknown>) => {
      setSplitting(true);
      try {
        const res = await call<{ updated: number; train: number; val: number }>('/api/dataset/split', {
          method: 'POST',
          body: JSON.stringify({ ids: selected.length ? selected : undefined, ...body }),
        });
        message.success(`已更新 ${res.updated} 张（train ${res.train} / val ${res.val}）`);
        setSelected([]);
        await load();
        onChanged?.();
      } catch (error) {
        message.error(`划分失败：${(error as Error).message}`);
      } finally {
        setSplitting(false);
      }
    },
    [selected, load, message, onChanged],
  );

  const columns: ColumnsType<DatasetItem> = [
    {
      title: '图片',
      dataIndex: 'id',
      width: 84,
      align: 'center',
      render: (_: unknown, row) => (
        <img
          src={`/api/dataset/images/${row.id}/thumb?w=120`}
          alt={row.path}
          loading="lazy"
          style={{ width: 68, height: 46, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--hud-line)', cursor: 'zoom-in' }}
          onClick={() => setPreview(row)}
        />
      ),
    },
    {
      title: '路径',
      dataIndex: 'path',
      ellipsis: true,
      render: (v: string, row) => (
        <Tooltip title={`${v}  ·  ${row.width}×${row.height}`}>
          <span style={{ fontSize: 11, opacity: 0.8 }}>{v.replace('training/captures/', '')}</span>
        </Tooltip>
      ),
    },
    {
      title: '类别',
      dataIndex: 'class_name',
      width: 190,
      render: (v: string, row) => (
        <Select
          size="small"
          style={{ width: '100%' }}
          value={classOptions.some((o) => o.value === v) ? v : undefined}
          placeholder={v || '未归类'}
          options={classOptions}
          disabled={busy === row.id}
          onChange={(next) => void patch(row, { class_name: next })}
        />
      ),
    },
    {
      title: '划分',
      dataIndex: 'split',
      width: 108,
      render: (v: string, row) => (
        <Select
          size="small"
          style={{ width: 92 }}
          value={v}
          options={SPLITS}
          disabled={busy === row.id}
          onChange={(next) => void patch(row, { split: next })}
        />
      ),
    },
    {
      title: '标注',
      dataIndex: 'labeled',
      width: 76,
      align: 'center',
      render: (v: boolean) =>
        v ? <Tag color="green">已标注</Tag> : <Tag color="orange">未标注</Tag>,
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 84,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_: unknown, row) => (
        <Space size={4}>
          <Button size="small" icon={<FolderOpenOutlined />} onClick={() => setPreview(row)} />
          <Button
            size="small"
            danger
            icon={<DeleteOutlined />}
            loading={busy === row.id}
            onClick={() => void remove(row, false)}
          >
            移除
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Space size={8} wrap>
        <Select
          allowClear
          size="small"
          placeholder="类别"
          style={{ width: 150 }}
          options={classOptions}
          value={filters.class_name}
          onChange={(value) => { setPage(1); setFilters((prev) => ({ ...prev, class_name: value })); }}
        />
        <Select
          allowClear
          size="small"
          placeholder="划分"
          style={{ width: 110 }}
          options={SPLITS}
          value={filters.split}
          onChange={(value) => { setPage(1); setFilters((prev) => ({ ...prev, split: value })); }}
        />
        <Select
          allowClear
          size="small"
          placeholder="标注"
          style={{ width: 110 }}
          options={[
            { value: true, label: '已标注' },
            { value: false, label: '未标注' },
          ]}
          value={filters.labeled}
          onChange={(value) => { setPage(1); setFilters((prev) => ({ ...prev, labeled: value ?? undefined })); }}
        />
        <Input.Search
          size="small"
          allowClear
          placeholder="按路径搜索"
          style={{ width: 180 }}
          value={filters.q}
          onChange={(event) => setFilters((prev) => ({ ...prev, q: event.target.value }))}
          onSearch={() => void load()}
        />
        <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
          刷新
        </Button>
        <span style={{ fontSize: 12, opacity: 0.7 }}>共 {total} 张</span>
        <Tag color={selected.length ? 'blue' : 'default'}>已选 {selected.length}</Tag>
        <Button size="small" loading={splitting} onClick={() => void doSplit({ mode: 'ratio', val_ratio: 0.2 })}>
          按 8:2 划分
        </Button>
        <Button size="small" loading={splitting} onClick={() => void doSplit({ mode: 'train' })}>
          全设 train
        </Button>
        <Button size="small" loading={splitting} onClick={() => void doSplit({ mode: 'val' })}>
          全设 val
        </Button>
        <Button size="small" loading={splitting} onClick={() => void doSplit({ mode: 'unsplit' })}>
          清除划分
        </Button>
      </Space>

      <Spin spinning={loading}>
        <Table<DatasetItem>
          size="small"
          rowKey="id"
          columns={columns}
          dataSource={items}
          pagination={false}
          scroll={{ x: 900 }}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys) => setSelected(keys.map(Number)),
            preserveSelectedRowKeys: true,
          }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有训练图（先抓拍或点「回填历史抓拍」）" /> }}
        />
      </Spin>

      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Pagination
          size="small"
          current={page}
          pageSize={size}
          total={total}
          showSizeChanger
          pageSizeOptions={['12', '24', '48']}
          showTotal={(count) => `共 ${count} 张`}
          onChange={(nextPage, nextSize) => {
            setPage(nextPage);
            setSize(nextSize);
          }}
        />
      </div>

      <Drawer
        width={640}
        open={preview !== null}
        onClose={() => setPreview(null)}
        title={preview ? `训练图 #${preview.id}` : ''}
        extra={
          preview ? (
            <Space size={8}>
              <Button
                size="small"
                danger
                onClick={() => {
                  void remove(preview, false);
                  setPreview(null);
                }}
              >
                从索引移除
              </Button>
            </Space>
          ) : null
        }
      >
        {preview ? (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            <img
              src={`/api/dataset/images/${preview.id}/thumb?w=640`}
              alt={preview.path}
              style={{ width: '100%', borderRadius: 10, border: '1px solid var(--hud-line)' }}
            />
            <Table
              size="small"
              showHeader={false}
              pagination={false}
              columns={[{ dataIndex: 'k', width: 90 }, { dataIndex: 'v' }]}
              dataSource={[
                { key: 'p', k: '路径', v: preview.path },
                { key: 'c', k: '类别', v: preview.class_name || '（空）' },
                { key: 's', k: '划分', v: preview.split },
                { key: 'l', k: '标注', v: preview.labeled ? '已标注' : '未标注' },
                { key: 'sz', k: '尺寸', v: `${preview.width}×${preview.height}` },
                { key: 'src', k: '来源', v: preview.source },
                { key: 'rec', k: '来自记录', v: preview.record_id ? `#${preview.record_id}` : '-' },
                { key: 't', k: '入库时间', v: preview.created_at ?? '-' },
                { key: 'n', k: '备注', v: preview.note ?? '-' },
              ]}
            />
          </Space>
        ) : null}
      </Drawer>
    </Space>
  );
}
