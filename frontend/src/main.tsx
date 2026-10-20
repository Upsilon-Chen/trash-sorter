import React from 'react';
import ReactDOM from 'react-dom/client';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';

// 自托管字体（只引 latin 子集，每个约 23 KB）：数字等宽对齐，仪表盘才不"业余"
import '@fontsource/inter/latin-400.css';
import '@fontsource/inter/latin-500.css';
import '@fontsource/inter/latin-600.css';
import '@fontsource/inter/latin-700.css';
// Rajdhani：HUD 风格的标题/按钮字体（几何感强、字距好压）
import '@fontsource/rajdhani/latin-500.css';
import '@fontsource/rajdhani/latin-600.css';
import '@fontsource/rajdhani/latin-700.css';
import '@fontsource/jetbrains-mono/latin-400.css';
import '@fontsource/jetbrains-mono/latin-500.css';

import App from './App';
import { ThemeModeProvider, useThemeMode } from './theme';
import './styles.css';
import './theme.css';
import './hud.css';
import './rhine.css';

// ---------------------------------------------------------------------------
// 全局请求超时兜底
//
// 任何 fetch 超过 20 秒没有响应就中止并抛错。这样即使后端卡住、网络断了、
// 或者某个接口永远不返回，界面也只会显示"请求失败"，**不会永远停在"刷新中"**。
// 之前所有列表同时卡在刷新状态、而后端其实早就空闲（日志可证），
// 就是因为请求一直挂着没人中止。
// ---------------------------------------------------------------------------
const rawFetch = window.fetch.bind(window);
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  if (init?.signal) return rawFetch(input, init);
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 20000);
  return rawFetch(input, { ...init, signal: controller.signal }).finally(() => {
    window.clearTimeout(timer);
  });
};

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('未找到 #root 挂载点');
}

/** 从主题上下文里取当前主题配置，交给 ConfigProvider */
function Root() {
  const { theme } = useThemeMode();
  return (
    <ConfigProvider locale={zhCN} theme={theme}>
      <AntApp>
        <App />
      </AntApp>
    </ConfigProvider>
  );
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <ThemeModeProvider>
      <Root />
    </ThemeModeProvider>
  </React.StrictMode>,
);
