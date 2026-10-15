import React from 'react';
import ReactDOM from 'react-dom/client';
import { App as AntApp, ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';

import App from './App';
import './styles.css';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('未找到 #root 挂载点');
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: '#12b76a',
          colorInfo: '#12b76a',
          borderRadius: 10,
          fontSize: 13,
          colorBgLayout: '#f2f5f7',
        },
        components: {
          Card: { paddingLG: 16 },
          Table: { cellPaddingBlockSM: 6, headerBg: '#f7fafc' },
        },
      }}
    >
      <AntApp>
        <App />
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
