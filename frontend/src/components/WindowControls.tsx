/**
 * WindowControls.tsx —— 无边框窗口的自绘控制按钮
 *
 * 只在 pywebview 里出现（普通浏览器访问时整块隐藏，零影响）。
 * 拖拽不在这里做：拖动交给标题区的 onMouseDown（见 App.tsx），
 * 这样表格选字、画布拖框都不会误触窗口移动。
 */

import { useEffect, useState } from 'react';
import { BorderOutlined, CloseOutlined, MinusOutlined } from '@ant-design/icons';

interface PyWebviewApi {
  minimize: () => void;
  toggle_maximize: () => void;
  move_to: (x: number, y: number) => void;
  close: () => void;
}

declare global {
  interface Window {
    pywebview?: { api: PyWebviewApi };
  }
}

export default function WindowControls() {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    // pywebview 的注入是异步的，启动时轮询一下（最多 10 秒）
    let tries = 0;
    const timer = window.setInterval(() => {
      tries += 1;
      if (window.pywebview?.api) {
        setReady(true);
        window.clearInterval(timer);
      } else if (tries > 40) {
        window.clearInterval(timer);
      }
    }, 250);
    return () => window.clearInterval(timer);
  }, []);

  if (!ready) return null;
  const api = window.pywebview?.api;
  if (!api) return null;

  return (
    <span className="win-ctl">
      <button type="button" title="最小化" onClick={() => api.minimize()}>
        <MinusOutlined />
      </button>
      <button type="button" title="最大化 / 还原" onClick={() => api.toggle_maximize()}>
        <BorderOutlined />
      </button>
      <button type="button" className="is-close" title="关闭" onClick={() => api.close()}>
        <CloseOutlined />
      </button>
    </span>
  );
}