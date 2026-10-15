/**
 * Charts.tsx —— 手写的轻量图表（纯 SVG，不引图表库）
 *
 * 为什么不用 @ant-design/plots / echarts：这个项目只需要"环形占比 + 横向条形 +
 * 迷你趋势线 + 置信度条"四种极简图形，手写 SVG 一共一百多行、零依赖、可控性最好；
 * 引入 G2/ECharts 会让本就 1.1 MB 的前端包再涨 300~500 KB，对课程项目不划算。
 * 将来要做复杂图表（热力图、多轴联动）再换图表库也不迟。
 */

import { useId, useMemo } from 'react';

export interface ChartDatum {
  label: string;
  value: number;
  color: string;
}

/* ------------------------------------------------------------------ 环形图 */

export function DonutChart({
  data,
  size = 140,
  thickness = 15,
  centerLabel,
}: {
  data: ChartDatum[];
  size?: number;
  thickness?: number;
  centerLabel?: string;
}) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  const radius = (size - thickness) / 2;
  const circumference = 2 * Math.PI * radius;
  const center = size / 2;

  // 每段用 dasharray 画，段间留 2px 间隙避免粘在一起
  let offset = 0;
  const segments = data
    .filter((item) => item.value > 0)
    .map((item) => {
      const fraction = item.value / total;
      const length = Math.max(fraction * circumference - 2, 0.5);
      const segment = { ...item, length, offset };
      offset += fraction * circumference;
      return segment;
    });

  return (
    <div className="donut">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="分拣占比">
        <circle
          cx={center}
          cy={center}
          r={radius}
          fill="none"
          stroke="var(--ui-track)"
          strokeWidth={thickness}
        />
        {total > 0 &&
          segments.map((segment) => (
            <circle
              key={segment.label}
              cx={center}
              cy={center}
              r={radius}
              fill="none"
              stroke={segment.color}
              strokeWidth={thickness}
              strokeLinecap="round"
              strokeDasharray={`${segment.length} ${circumference - segment.length}`}
              strokeDashoffset={-segment.offset}
              transform={`rotate(-90 ${center} ${center})`}
              className="donut-seg"
            />
          ))}
      </svg>
      <div className="donut-center">
        <div className="donut-value mono">{total}</div>
        <div className="donut-label">{centerLabel ?? '累计分拣'}</div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 条形列表 */

export function BarList({ data, max, unit }: { data: ChartDatum[]; max?: number; unit?: string }) {
  const peak = max ?? Math.max(1, ...data.map((item) => item.value));
  return (
    <div className="bar-list">
      {data.map((item) => (
        <div className="bar-row" key={item.label}>
          <span className="bar-label" title={item.label}>
            {item.label}
          </span>
          <span className="bar-track">
            <span
              className="bar-fill"
              style={{ width: `${Math.max((item.value / peak) * 100, item.value > 0 ? 4 : 0)}%`, background: item.color }}
            />
          </span>
          <span className="bar-value mono">
            {item.value}
            {unit}
          </span>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ 迷你趋势 */

export function Sparkline({
  points,
  width = 260,
  height = 44,
  color = 'var(--brand)',
  label,
}: {
  points: number[];
  width?: number;
  height?: number;
  color?: string;
  label?: string;
}) {
  const gradientId = `spark-fill-${useId()}`;
  const path = useMemo(() => {
    if (points.length === 0) return { line: '', area: '' };
    const peak = Math.max(1, ...points);
    const stepX = points.length > 1 ? width / (points.length - 1) : width;
    const coords = points.map((value, index) => {
      const x = index * stepX;
      const y = height - 4 - (value / peak) * (height - 10);
      return [x, y] as const;
    });
    const line = coords.map(([x, y], index) => `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const area = `${line} L${width},${height} L0,${height} Z`;
    return { line, area };
  }, [height, points, width]);

  return (
    <div className="sparkline">
      {label && <div className="sparkline-label muted">{label}</div>}
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.28" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        {path.area && <path d={path.area} fill={`url(#${gradientId})`} />}
        {path.line && (
          <path d={path.line} fill="none" stroke={color} strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
        )}
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------------ 置信度条 */

export function ConfidenceBar({ value, color = 'var(--brand)' }: { value: number; color?: string }) {
  const percent = Math.round(Math.min(Math.max(value, 0), 1) * 100);
  return (
    <span className="conf-bar" title={`置信度 ${percent}%`}>
      <span className="conf-track">
        <span className="conf-fill" style={{ width: `${percent}%`, background: color }} />
      </span>
      <span className="conf-value mono">{percent}%</span>
    </span>
  );
}
