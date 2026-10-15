import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 后端地址（REST + WebSocket 的代理目标）。
 * 如需指向别的机器，改这里，或在前端目录建 .env.local：
 *   VITE_API_BASE=http://192.168.1.20:8000
 *   VITE_WS_URL=ws://192.168.1.20:8000/ws
 * （配置了 VITE_API_BASE 时，请求不再走下面的 dev 代理）
 */
const BACKEND_HOST = process.env.VITE_BACKEND_HOST ?? '127.0.0.1';
const BACKEND_PORT = process.env.VITE_BACKEND_PORT ?? '8000';
const BACKEND_HTTP = `http://${BACKEND_HOST}:${BACKEND_PORT}`;
const BACKEND_WS = `ws://${BACKEND_HOST}:${BACKEND_PORT}`;

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
    proxy: {
      // REST：/api/** -> FastAPI
      '/api': {
        target: BACKEND_HTTP,
        changeOrigin: true,
        // MJPEG 预览流必须实时透传，不能缓冲
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (String(proxyRes.headers['content-type'] ?? '').includes('multipart/x-mixed-replace')) {
              delete proxyRes.headers['content-length'];
            }
          });
        },
      },
      // WebSocket：/ws -> FastAPI
      '/ws': {
        target: BACKEND_WS,
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
  },
});
