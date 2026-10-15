/**
 * Hud.tsx —— HUD 皮肤的基础构件
 *
 * antd 只提供行为和复杂控件；"长得像工业仪表"这件事靠这几个自绘小件：
 *   HudRing   环形刻度仪表（置信度）
 *   HudMeter  分段刻度条（10 格，比连续条更有"仪器感"）
 *   HudStat   标签 + 大号等宽数值
 *   HudChip   状态胶囊（带呼吸点）
 * 全部纯 SVG / DOM，零依赖。
 */

export type HudTone = 'ok' | 'warn' | 'off';

/* ------------------------------------------------------------------ 状态点 */

export function HudDot({ state = 'ok' }: { state?: HudTone }) {
  return <span className={`hud-dot${state === 'ok' ? '' : ` is-${state}`}`} />;
}

export function HudChip({
  label,
  value,
  state = 'ok',
}: {
  label: string;
  value?: string | number;
  state?: HudTone;
}) {
  return (
    <span className={`hud-chip${state === 'ok' ? ' is-ok' : state === 'warn' ? ' is-warn' : ' is-off'}`}>
      <HudDot state={state} />
      {label}
      {value !== undefined && value !== '' && <strong>{value}</strong>}
    </span>
  );
}

/* ------------------------------------------------------------------ 数值 */

export function HudStat({
  label,
  value,
  unit,
  accent,
}: {
  label: string;
  value: string | number;
  unit?: string;
  accent?: boolean;
}) {
  return (
    <div className={`hud-stat${accent ? ' is-accent' : ''}`}>
      <span className="hud-label">{label}</span>
      <span className="hud-value">
        {value}
        {unit && <span className="hud-unit">{unit}</span>}
      </span>
    </div>
  );
}

/* ------------------------------------------------------------ 分段刻度条 */

export function HudMeter({
  value,
  cells = 10,
  showValue = true,
  tone,
}: {
  /** 0~1 */
  value: number;
  cells?: number;
  showValue?: boolean;
  tone?: 'ok' | 'warn' | 'danger';
}) {
  const percent = Math.round(Math.min(Math.max(value, 0), 1) * 100);
  const filled = Math.round((percent / 100) * cells);
  const toneClass = tone ?? (percent >= 60 ? 'ok' : percent >= 40 ? 'warn' : 'danger');
  return (
    <span className="hud-meter">
      <span className="hud-meter-track">
        {Array.from({ length: cells }).map((_, index) => (
          <span
            key={index}
            className={
              index < filled
                ? `hud-meter-cell is-on${toneClass === 'warn' ? ' is-warn' : toneClass === 'danger' ? ' is-danger' : ''}`
                : 'hud-meter-cell'
            }
          />
        ))}
      </span>
      {showValue && <span className="hud-meter-value">{percent}%</span>}
    </span>
  );
}

/* ------------------------------------------------------------ 环形仪表 */

export function HudRing({
  value,
  size = 132,
  caption = '置信度',
  ticks = 28,
}: {
  /** 0~1 */
  value: number;
  size?: number;
  caption?: string;
  ticks?: number;
}) {
  const percent = Math.round(Math.min(Math.max(value, 0), 1) * 100);
  const stroke = 5;
  const center = size / 2;
  const radius = center - 14;
  const sweep = 270; // 只画 270°，缺口朝下，更像仪表
  const startAngle = 135; // 从左下开始，顺时针

  const toXY = (angleDeg: number, r: number) => {
    const rad = ((angleDeg - 90) * Math.PI) / 180;
    return [center + r * Math.cos(rad), center + r * Math.sin(rad)] as const;
  };

  const circumference = 2 * Math.PI * radius;
  const arcLength = (sweep / 360) * circumference;
  const dash = arcLength * (percent / 100);

  return (
    <div className="hud-ring" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        {/* 外圈刻度 */}
        <g>
          {Array.from({ length: ticks }).map((_, index) => {
            const ratio = index / (ticks - 1);
            const angle = startAngle + ratio * sweep;
            const active = ratio <= percent / 100;
            const [x1, y1] = toXY(angle, radius + 7);
            const [x2, y2] = toXY(angle, radius + (index % 4 === 0 ? 12 : 10));
            return (
              <line
                key={index}
                x1={x1}
                y1={y1}
                x2={x2}
                y2={y2}
                stroke={active ? 'var(--hud-accent)' : 'var(--hud-line)'}
                strokeWidth={index % 4 === 0 ? 1.4 : 0.9}
                opacity={active ? 0.95 : 1}
              />
            );
          })}
        </g>
        {/* 轨道 */}
        <circle
          cx={center}
          cy={center}
          r={radius}
          fill="none"
          stroke="var(--hud-track-color)"
          strokeWidth={stroke}
          strokeDasharray={`${arcLength} ${circumference}`}
          transform={`rotate(${startAngle} ${center} ${center})`}
          strokeLinecap="butt"
        />
        {/* 数值弧 */}
        <circle
          cx={center}
          cy={center}
          r={radius}
          fill="none"
          stroke={percent >= 60 ? 'var(--hud-accent)' : percent >= 40 ? 'var(--hud-warn)' : 'var(--hud-danger)'}
          strokeWidth={stroke}
          strokeDasharray={`${dash} ${circumference}`}
          transform={`rotate(${startAngle} ${center} ${center})`}
          strokeLinecap="butt"
          style={{ transition: 'stroke-dasharray .5s var(--ease)', filter: 'drop-shadow(0 0 6px currentColor)' }}
        />
      </svg>
      <div className="hud-ring-center">
        <div className="hud-value is-accent">
          {percent}
          <span className="hud-unit">%</span>
        </div>
        <div className="hud-label">{caption}</div>
      </div>
    </div>
  );
}
