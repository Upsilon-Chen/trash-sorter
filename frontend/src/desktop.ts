/**
 * desktop.ts —— 桌面窗口（pywebview）的桥接
 *
 * 只在 exe / 启动器开出来的窗口里存在；浏览器访问时 window.pywebview 是 undefined，
 * 所有调用都会安全地变成空操作。
 */

export interface DesktopApi {
  /** 同步系统标题栏配色，让标题栏跟随界面明暗主题 */
  set_caption: (dark: boolean) => void;
  minimize: () => void;
  toggle_maximize: () => void;
  move_to: (x: number, y: number) => void;
  close: () => void;
}

declare global {
  interface Window {
    pywebview?: { api: DesktopApi };
  }
}

export function desktopApi(): DesktopApi | undefined {
  return window.pywebview?.api;
}

export function syncCaption(dark: boolean): void {
  try {
    desktopApi()?.set_caption?.(dark);
  } catch {
    /* 标题栏染色失败不影响使用 */
  }
}