/**
 * theme.ts —— 主题系统
 *
 * 两套主题，一键切换（偏好记在 localStorage，首次跟随系统）：
 *   light  精炼浅色（默认打底）：白卡片 + 浅灰工作区 + 环保绿点缀，投影最安全
 *   dark   深色科技仪表盘：深底 + 微描边 + 数据用青色高亮，与摄像头画面融为一体
 *
 * 实现要点：
 *   1. 设计令牌集中在这里 —— 改一处全站生效，不再散落在各个组件里；
 *   2. `cssVar: true` 让 antd 把 token 编译成 CSS 变量（--ant-color-primary 等），
 *      自定义样式表可以直接引用，两套主题自动跟随；
 *   3. 同时把主题写到 <html data-theme="...">，styles.css 里的 --ui-* 变量据此切换
 *      （图片画布、检测框这类"永远深色"的表面不参与切换）。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { theme as antdTheme, type ThemeConfig } from 'antd';

export type ThemeMode = 'light' | 'dark';

const STORAGE_KEY = 'trash-sorter-theme';

/** 品牌色板（两套主题共用；深色下会更亮一点，见下方的 token 覆盖） */
export const PALETTE = {
  green: '#12B76A',
  cyan: '#0E9384',
  amber: '#B54708',
  red: '#B42318',
  violet: '#6941C6',
} as const;

/** 四个物理桶的固定配色（图表与标签共用，保证同一个桶在哪儿都是同一个颜色） */
export const BUCKET_COLORS = ['#12B76A', '#F79009', '#7A5AF8', '#0E9384'] as const;

const FONT_SANS =
  "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif";
const FONT_MONO = "'JetBrains Mono', 'Cascadia Code', Consolas, 'Courier New', monospace";

const baseToken: ThemeConfig['token'] = {
  colorPrimary: PALETTE.green,
  colorInfo: PALETTE.green,
  colorSuccess: PALETTE.green,
  colorWarning: PALETTE.amber,
  colorError: PALETTE.red,
  borderRadius: 10,
  fontSize: 13,
  fontFamily: FONT_SANS,
  fontFamilyCode: FONT_MONO,
  controlHeight: 32,
};

/** 浅色：精炼 + 高对比（次要文字用 #667085，对比度约 4.8:1，满足 WCAG AA） */
export const lightTheme: ThemeConfig = {
  algorithm: antdTheme.defaultAlgorithm,
  cssVar: true,
  token: {
    ...baseToken,
    colorBgLayout: '#F2F5F7',
    colorBgContainer: '#FFFFFF',
    colorText: '#101828',
    colorTextSecondary: '#475467',
    colorTextTertiary: '#667085',
    colorBorder: '#E4E7EC',
    colorBorderSecondary: '#EFF1F4',
  },
  components: {
    Card: { paddingLG: 16 },
    Table: { cellPaddingBlockSM: 6, headerBg: '#F7FAFC' },
    Segmented: { itemSelectedBg: '#FFFFFF' },
  },
};

/** 深色：科技仪表盘（深底 + 微描边 + 数据青色高亮） */
export const darkTheme: ThemeConfig = {
  algorithm: antdTheme.darkAlgorithm,
  cssVar: true,
  token: {
    ...baseToken,
    colorPrimary: '#2FD67F',
    colorInfo: '#2FD67F',
    colorBgLayout: '#0B1418',
    colorBgContainer: '#151F26',
    colorBgElevated: '#18242C',
    colorText: '#E6EDF3',
    colorTextSecondary: '#A9B6C2',
    colorTextTertiary: '#8B9AA8',
    colorBorder: '#26313A',
    colorBorderSecondary: '#1E2A32',
  },
  components: {
    Card: { paddingLG: 16 },
    Table: { cellPaddingBlockSM: 6, headerBg: '#18242C' },
  },
};

export function themeConfigOf(mode: ThemeMode): ThemeConfig {
  return mode === 'dark' ? darkTheme : lightTheme;
}

/* ------------------------------------------------------------------ 状态 */

interface ThemeModeValue {
  mode: ThemeMode;
  theme: ThemeConfig;
  toggle: () => void;
  setMode: (mode: ThemeMode) => void;
}

const ThemeModeContext = createContext<ThemeModeValue | null>(null);

function initialMode(): ThemeMode {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch {
    /* 隐私模式下 localStorage 可能不可用 */
  }
  // 基准是参考项目的暖灰白浅色；暗色（石墨）随时可切
  return 'light';
}

export function ThemeModeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(initialMode);

  useEffect(() => {
    // 交给 styles.css 的 [data-theme] 选择器，同时让滚动条/表单控件跟随
    document.documentElement.dataset.theme = mode;
    document.documentElement.style.colorScheme = mode;
    try {
      window.localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      /* 忽略 */
    }
  }, [mode]);

  const toggle = useCallback(() => setMode((value) => (value === 'dark' ? 'light' : 'dark')), []);
  const value = useMemo<ThemeModeValue>(
    () => ({ mode, theme: themeConfigOf(mode), toggle, setMode }),
    [mode, toggle],
  );

  return <ThemeModeContext.Provider value={value}>{children}</ThemeModeContext.Provider>;
}

export function useThemeMode(): ThemeModeValue {
  const value = useContext(ThemeModeContext);
  if (!value) throw new Error('useThemeMode 必须在 ThemeModeProvider 内使用');
  return value;
}
