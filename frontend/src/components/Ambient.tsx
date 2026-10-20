/**
 * Ambient.tsx —— 背景氛围层 + 鼠标跟随
 *
 * 借鉴 RhineLabUI 的「HUD 鼠标追踪」思路（它把 UI 投影到共同径向曲面做视差），
 * 这里改成本项目自己的表达，不照抄实现：
 *
 *   1. 跟手光晕   一大团生命绿→青的柔光跟着鼠标走（带缓动，不是硬跟）
 *   2. 极光漂移   两层大半径柔光缓慢游移（24~30s，看得见但不抢戏）
 *   3. 浮游孢子   两层不同速度/大小的点阵向相反方向漂，形成纵深
 *   4. 点阵行波   逐点计算相位的 canvas 点阵，从左下往右上推进（DotWave.tsx）
 *   5. 微倾斜     整页随鼠标做 ≤0.35° 的视差，产生"一层层浮着"的错觉
 *
 * 分寸控制：
 *   · 只用 transform / opacity（GPU 合成，不触发布局与重绘）
 *   · 鼠标位置用 rAF + 线性插值缓动，且指针静止时不再写样式
 *   · 全部 pointer-events: none，不挡任何点击
 *   · prefers-reduced-motion：动效全停，只留静态氛围
 */

import { useEffect, useRef } from 'react';

import DotWave from './DotWave';

/** 光晕位置：直接跟手（不做插值），要的就是"无缝" */
const SPOT_DIRECT = true;
/** 视差/卡片柔光用的缓动：略微阻尼，避免整页抖动 */
const EASE = 0.42;
/** 每帧最多推进的时间，避免切回标签页时一步跳到位 */
const MAX_STEP = 40;

export default function Ambient() {
  const spotRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const root = document.documentElement;
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    if (reduced) {
      // 减少动态效果：只保留静态氛围，不跟鼠标、不倾斜
      root.style.setProperty('--mx', '0');
      root.style.setProperty('--my', '0');
      return;
    }

    let targetX = window.innerWidth / 2;
    let targetY = window.innerHeight * 0.36;
    let currentX = targetX;
    let currentY = targetY;
    let frame = 0;
    let last = performance.now();
    let needsUpdate = true;
    let lastVarWrite = 0;

    const onMove = (event: PointerEvent) => {
      targetX = event.clientX;
      targetY = event.clientY;
      needsUpdate = true;
    };

    const onLeave = () => {
      // 指针离开窗口：光晕慢慢回到中间偏上
      targetX = window.innerWidth / 2;
      targetY = window.innerHeight * 0.36;
      needsUpdate = true;
    };

    const tick = (now: number) => {
      const delta = Math.min(now - last, MAX_STEP);
      last = now;
      // 与帧间隔无关的缓动，保证 30fps 与 120fps 手感一致
      const k = 1 - Math.pow(1 - EASE, delta / 16.67);
      currentX += (targetX - currentX) * k;
      currentY += (targetY - currentY) * k;

      // CSS 变量只驱动 ≤0.35° 的页面倾斜，20fps 足够；每帧写会让全文档样式失效
      if (now - lastVarWrite > 50) {
        lastVarWrite = now;
        const width = window.innerWidth || 1;
        const height = window.innerHeight || 1;
        root.style.setProperty('--mx', ((currentX / width) * 2 - 1).toFixed(3));
        root.style.setProperty('--my', ((currentY / height) * 2 - 1).toFixed(3));
      }
      const spotX = SPOT_DIRECT ? targetX : currentX;
      const spotY = SPOT_DIRECT ? targetY : currentY;
      if (spotRef.current) {
        spotRef.current.style.transform = `translate3d(${spotX.toFixed(1)}px, ${spotY.toFixed(1)}px, 0)`;
      }

      const settled = Math.abs(targetX - currentX) < 0.4 && Math.abs(targetY - currentY) < 0.4;
      if (settled && !needsUpdate) {
        frame = window.requestAnimationFrame(tick);
        return;
      }
      needsUpdate = false;
      frame = window.requestAnimationFrame(tick);
    };

    window.addEventListener('pointermove', onMove, { passive: true });
    window.addEventListener('pointerleave', onLeave, { passive: true });
    window.addEventListener('blur', onLeave);
    frame = window.requestAnimationFrame(tick);

    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerleave', onLeave);
      window.removeEventListener('blur', onLeave);
      root.style.removeProperty('--mx');
      root.style.removeProperty('--my');
    };
  }, []);

  return (
    <div className="ambient" aria-hidden="true">
      <div className="amb-aurora" />
      <div className="amb-spores" />
      <DotWave />
      <div className="amb-spot-wrap">
        <div className="amb-spot" ref={spotRef} />
      </div>
    </div>
  );
}
