/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 REST 根地址，留空表示走 Vite dev 代理（同源 /api） */
  readonly VITE_API_BASE?: string;
  /** 后端 WebSocket 地址，留空则按 API_BASE / 当前 origin 推导 */
  readonly VITE_WS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
