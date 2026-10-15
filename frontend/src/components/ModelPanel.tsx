import { useCallback, useEffect, useState } from 'react';
import {
  App as AntApp,
  Alert,
  Button,
  Card,
  Descriptions,
  Slider,
  Space,
  Table,
  Tag,
  Tooltip,
  Upload,
  type TableProps,
} from 'antd';
import { CloudUploadOutlined, DeploymentUnitOutlined, ReloadOutlined, SwapOutlined } from '@ant-design/icons';

import {
  ApiError,
  errorText,
  listModels,
  selectModel,
  uploadModel,
  type ModelFile,
  type ModelState,
} from '../api';

export interface ModelPanelProps {
  modelState: ModelState | null;
  onModelState: (state: ModelState) => void;
  confidence: number;
  onConfidenceChange: (value: number) => void;
  detectionRunning: boolean;
  onStateRefresh: () => void;
}

/**
 * ModelPanel：扫描 backend/models/*.pt、热切换、上传、置信度设置。
 */
export default function ModelPanel({
  modelState,
  onModelState,
  confidence,
  onConfidenceChange,
  detectionRunning,
  onStateRefresh,
}: ModelPanelProps) {
  const { message, modal } = AntApp.useApp();
  const [models, setModels] = useState<ModelFile[]>([]);
  const [modelDir, setModelDir] = useState('');
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [switching, setSwitching] = useState<string | null>(null);

  const refresh = useCallback(
    async (silent = false) => {
      setLoading(true);
      try {
        const data = await listModels();
        setModels(data.models ?? []);
        setModelDir(data.model_dir);
        onModelState(data.info);
        if (!silent) {
          message.success(`发现 ${data.models?.length ?? 0} 个模型`);
        }
      } catch (error) {
        message.error(errorText(error));
      } finally {
        setLoading(false);
      }
    },
    [message, onModelState],
  );

  useEffect(() => {
    void refresh(true);
  }, [refresh]);

  const handleSelect = useCallback(
    async (name: string) => {
      setSwitching(name);
      try {
        const data = await selectModel(name);
        onModelState(data.model);
        message.success(`已切换到 ${name}（${data.model.class_count} 个类别，${data.model.device}）`);
        onStateRefresh();
        void refresh(true);
      } catch (error) {
        message.error(errorText(error));
      } finally {
        setSwitching(null);
      }
    },
    [message, onModelState, onStateRefresh, refresh],
  );

  const handleUpload = useCallback(
    async (file: File, overwrite = false) => {
      setUploading(true);
      try {
        const data = await uploadModel(file, overwrite);
        message.success(`上传成功：${data.uploaded?.name ?? file.name}（${data.size_mb} MB）`);
        setModels(data.models ?? []);
        void refresh(true);
      } catch (error) {
        if (error instanceof ApiError && error.status === 409 && !overwrite) {
          modal.confirm({
            title: '模型已存在',
            content: `${file.name} 已存在于后端，是否覆盖？`,
            okText: '覆盖',
            cancelText: '取消',
            onOk: () => handleUpload(file, true),
          });
        } else {
          message.error(errorText(error));
        }
      } finally {
        setUploading(false);
      }
    },
    [message, modal, refresh],
  );

  const columns: TableProps<ModelFile>['columns'] = [
    {
      title: '模型文件',
      dataIndex: 'name',
      key: 'name',
      render: (_value, record) => (
        <Space size={6}>
          <span className="mono">{record.name}</span>
          {record.current && <Tag color="green">当前</Tag>}
        </Space>
      ),
    },
    {
      title: '大小',
      dataIndex: 'size_mb',
      key: 'size_mb',
      width: 90,
      render: (value: number) => `${value} MB`,
    },
    {
      title: '修改时间',
      dataIndex: 'modified',
      key: 'modified',
      width: 170,
      render: (value: string) => <span className="muted">{value.replace('T', ' ')}</span>,
    },
    {
      title: '操作',
      key: 'action',
      width: 100,
      render: (_value, record) => (
        <Button
          size="small"
          type={record.current ? 'default' : 'primary'}
          icon={<SwapOutlined />}
          disabled={record.current}
          loading={switching === record.name}
          onClick={() => void handleSelect(record.name)}
        >
          {record.current ? '使用中' : '加载'}
        </Button>
      ),
    },
  ];

  return (
    <Card
      className="panel-card"
      title={
        <Space size={8}>
          <span className="panel-title"><DeploymentUnitOutlined />模型</span>
          {modelState?.loaded ? (
            <Tag color="green">{modelState.current}</Tag>
          ) : (
            <Tag color="red">未加载</Tag>
          )}
        </Space>
      }
      extra={
        <Space size={8}>
          <Upload
            accept=".pt"
            showUploadList={false}
            beforeUpload={(file) => {
              void handleUpload(file as unknown as File);
              return false;
            }}
          >
            <Button size="small" icon={<CloudUploadOutlined />} loading={uploading}>
              上传 .pt
            </Button>
          </Upload>
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void refresh()}>
            刷新
          </Button>
        </Space>
      }
    >
      <Table<ModelFile>
        size="small"
        rowKey="name"
        columns={columns}
        dataSource={models}
        loading={loading}
        pagination={false}
        scroll={{ y: 168 }}
        locale={{ emptyText: `未发现模型，请把 .pt 放到 ${modelDir || 'backend/models'}` }}
        rowClassName={(record) => (record.current ? 'row-active' : '')}
      />

      <div className="section-title">
        <span>置信度阈值</span>
        <Tooltip title="低于该阈值的结果不显示、不参与自动分类">
          <Tag color="blue">{confidence.toFixed(2)}</Tag>
        </Tooltip>
      </div>
      <Slider
        min={0.05}
        max={0.95}
        step={0.05}
        value={confidence}
        onChange={onConfidenceChange}
        marks={{ 0.05: '0.05', 0.35: '0.35', 0.95: '0.95' }}
        tooltip={{ formatter: (value) => `conf ≥ ${Number(value ?? 0).toFixed(2)}` }}
      />
      {detectionRunning ? (
        <Alert
          type="warning"
          showIcon
          message={`检测运行中，当前阈值 ${confidence.toFixed(2)}；修改后需重新开始检测才生效`}
        />
      ) : (
        <Alert
          type="info"
          showIcon
          message="「开始检测」时会把该阈值提交给后端，覆盖 settings.yaml 的 detection.min_confidence"
        />
      )}

      <div className="section-title">
        <span>当前模型信息</span>
      </div>
      <Descriptions size="small" column={2} colon={false} className="compact-descriptions">
        <Descriptions.Item label="设备">{modelState?.device ?? '-'}</Descriptions.Item>
        <Descriptions.Item label="类别数">{modelState?.class_count ?? 0}</Descriptions.Item>
        <Descriptions.Item label="推理次数">{modelState?.inference_count ?? 0}</Descriptions.Item>
        <Descriptions.Item label="单帧耗时">
          {modelState ? `${modelState.last_inference_ms.toFixed(1)} ms` : '-'}
        </Descriptions.Item>
        <Descriptions.Item label="加载时间" span={2}>
          {(modelState?.loaded_at ?? '-').replace('T', ' ')}
        </Descriptions.Item>
      </Descriptions>
      <div className="class-tags">
        {(modelState?.classes ?? []).map((name) => (
          <Tag key={name} color="geekblue">
            {name}
          </Tag>
        ))}
        {!modelState?.loaded && <span className="muted">加载模型后显示类别列表</span>}
      </div>
    </Card>
  );
}
