/**
 * DotWave.tsx —— 背景点阵行波（canvas）
 *
 * 为什么不用 CSS：需求是"点阵随波移动 + 鼠标影响点阵"，CSS 的 background-position /
 * opacity 动画只能整层平移或整体呼吸，做不到逐点位移与相位。
 * canvas 可以逐点算相位，代价也很低（一次全屏几千个 fillRect）。
 *
 * 实现要点：
 *   · 点阵按 spacing 铺满，沿波方向 s = x·cosθ + y·sinθ 计算行波相位
 *   · 每个点：位移（沿波方向来回摆）+ 亮度 + 尺寸 都由相位驱动 → 看得见"波在走"
 *   · 鼠标：附近点额外提亮并向外推；点击时发出扩散涟漪
 *   · 只画方块不画圆（fillRect 比 arc 快一个量级），用 globalAlpha 控制亮度，
 *     避免每个点拼 rgba 字符串造成 GC 抖动
 *   · DPR 上限 1.75、限制总点数、标签页隐藏时暂停、prefers-reduced-motion 只画静态
 */

import { useEffect, useRef } from 'react';

/** 波的传播方向：约 -35°（从左下往右上） */
const THETA = -0.62;
/** 波长（px）与角速度（rad/s → 约 7 秒一个周期） */
const WAVELENGTH = 280;
const OMEGA = 0.9;   // 正值 = 波朝 +along 方向传播（左下 → 右上），已用互相关实测确认
/** 鼠标影响半径与力度 */
const MOUSE_RADIUS = 220;

export default function DotWave() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d', { alpha: true });
    if (!ctx) return;

    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

    let width = 0;
    let height = 0;
    let spacing = 32;
    let points = new Float32Array(0);
    let count = 0;
    let rgb = '150, 240, 200';
    let baseAlpha = 0.34;

    // 鼠标（带缓动，避免抖动）
    let mouseX = -9999;
    let mouseY = -9999;
    let targetX = -9999;
    let targetY = -9999;
    let pointerInside = false;

    interface Ripple {
      x: number;
      y: number;
      born: number;
    }
    const ripples: Ripple[] = [];

    const readTheme = () => {
      const dark = document.documentElement.dataset.theme === 'dark';
      // 颜色跟着主题走：深色用荧光绿，浅色用近黑点（浅色下点阵是"墨点"）
      rgb = dark ? '150, 240, 200' : '11, 26, 20';
      baseAlpha = dark ? 0.85 : 0.30;
    };

    const build = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.75);
      width = window.innerWidth;
      height = window.innerHeight;
      canvas.width = Math.max(1, Math.floor(width * dpr));
      canvas.height = Math.max(1, Math.floor(height * dpr));
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      spacing = width < 900 ? 24 : 30;
      // 点太多也不划算：给总数设上限，超了就加大间距
      while ((Math.ceil(width / spacing) + 2) * (Math.ceil(height / spacing) + 2) > 5200) {
        spacing += 4;
      }
      const cols = Math.ceil(width / spacing) + 2;
      const rows = Math.ceil(height / spacing) + 2;
      count = cols * rows;
      points = new Float32Array(count * 2);
      let i = 0;
      for (let row = 0; row < rows; row += 1) {
        for (let col = 0; col < cols; col += 1) {
          points[i] = col * spacing - spacing;
          points[i + 1] = row * spacing - spacing;
          i += 2;
        }
      }
    };

    const cosT = Math.cos(THETA);
    const sinT = Math.sin(THETA);
    const k = (Math.PI * 2) / WAVELENGTH;

    const render = (seconds: number) => {
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = `rgb(${rgb})`;
      const rippleCount = ripples.length;
      const r2 = MOUSE_RADIUS * MOUSE_RADIUS;

      for (let index = 0; index < count; index += 1) {
        const baseX = points[index * 2];
        const baseY = points[index * 2 + 1];
        const along = baseX * cosT + baseY * sinT;
        const phase = along * k - seconds * OMEGA;
        const wave = 0.5 + 0.5 * Math.sin(phase);
        const crest = wave * wave; // 峰值更尖，波峰更明显

        // 沿波方向来回摆动 → 点阵"随波移动"
        const swing = (crest - 0.45) * 8;
        let x = baseX + cosT * swing;
        let y = baseY + sinT * swing;

        let alpha = baseAlpha * (0.24 + 1.5 * crest);
        let size = 1.5 + 1.7 * crest;

        // 鼠标附近：提亮 + 向外推
        if (pointerInside) {
          const dx = x - mouseX;
          const dy = y - mouseY;
          const dist2 = dx * dx + dy * dy;
          if (dist2 < r2) {
            const fall = 1 - dist2 / r2;
            const strength = fall * fall;
            alpha += baseAlpha * 1.9 * strength;
            size += 1.5 * strength;
            x += dx * 0.05 * strength;
            y += dy * 0.05 * strength;
          }
        }

        // 点击涟漪：向外扩散的推力 + 增亮
        for (let r = 0; r < rippleCount; r += 1) {
          const ripple = ripples[r];
          const age = seconds - ripple.born;
          if (age > 1.6) continue;
          const radius = age * 460;
          const dx = x - ripple.x;
          const dy = y - ripple.y;
          const distance = Math.sqrt(dx * dx + dy * dy) || 1;
          const band = 1 - Math.min(Math.abs(distance - radius) / 90, 1);
          if (band > 0) {
            const decay = 1 - age / 1.6;
            const push = band * band * decay * 14;
            alpha += baseAlpha * band * decay * 1.2;
            x += (dx / distance) * push;
            y += (dy / distance) * push;
          }
        }

        if (alpha <= 0.004) continue;
        ctx.globalAlpha = alpha > 1 ? 1 : alpha;
        ctx.fillRect(x, y, size, size);
      }
      ctx.globalAlpha = 1;

      // 清理过期涟漪
      for (let index = ripples.length - 1; index >= 0; index -= 1) {
        if (seconds - ripples[index].born > 1.6) ripples.splice(index, 1);
      }
    };

    let frame = 0;
    let last = performance.now();
    let clock = 0;
    let accumulator = 0;
    let running = true;

    const tick = (now: number) => {
      frame = window.requestAnimationFrame(tick);
      if (!running) return;
      const delta = Math.min((now - last) / 1000, 0.05);
      last = now;
      // 限制到 ~40fps：注意要"攒时间"，不能直接丢帧 ——
      // 60Hz 下每帧 delta≈0.0167，若 delta<0.022 就 return，时间永远攒不够，画面会冻住
      accumulator += delta;
      if (accumulator < 0.013) return;
      clock += accumulator;
      accumulator = 0;

      // 鼠标位置直接取目标值（不做缓动）：缓动会让"鼠标附近提亮"明显滞后，
      // 这里要的是几乎没有延迟的跟手。
      if (targetX > -9000) {
        mouseX = targetX;
        mouseY = targetY;
      }
      render(clock);
    };

    const onMove = (event: PointerEvent) => {
      targetX = event.clientX;
      targetY = event.clientY;
      pointerInside = true;
    };
    const onLeave = () => {
      pointerInside = false;
    };
    const onDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      ripples.push({ x: event.clientX, y: event.clientY, born: clock });
      if (ripples.length > 6) ripples.shift();
    };
    const onVisibility = () => {
      running = !document.hidden;
      last = performance.now();
    };

    readTheme();
    build();
    if (reduced) {
      // 减少动态效果：只画一帧静态点阵
      render(0);
    } else {
      frame = window.requestAnimationFrame(tick);
    }

    const themeObserver = new MutationObserver(readTheme);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    window.addEventListener('resize', build);
    window.addEventListener('pointermove', onMove, { passive: true });
    window.addEventListener('pointerdown', onDown, { passive: true });
    window.addEventListener('pointerleave', onLeave, { passive: true });
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      window.cancelAnimationFrame(frame);
      themeObserver.disconnect();
      window.removeEventListener('resize', build);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerdown', onDown);
      window.removeEventListener('pointerleave', onLeave);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  return <canvas className="amb-wave" ref={canvasRef} aria-hidden="true" />;
}
