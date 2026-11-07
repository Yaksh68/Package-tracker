// Theme: the original antd look, with a dark mode (default), a calmer accent
// than antd's stock blue, and a warmer font. Kept deliberately small — the
// layout and components are the original ones.
import { ConfigProvider, theme as antdTheme, type ThemeConfig } from "antd";
import { createContext, useContext, useLayoutEffect, useMemo, useState, type ReactNode } from "react";

export type Mode = "dark" | "light";

const FONT = `"Source Sans 3", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`;

// Also used by index.html for the first paint — keep the two in sync.
export const PALETTE = {
  dark: {
    page: "#131519",
    surface: "#1b1e23",
    raised: "#22262c",
    border: "#2e333a",
    accent: "#6fa3e0",
  },
  light: {
    page: "#f4f5f7",
    surface: "#ffffff",
    raised: "#ffffff",
    border: "#e3e5e9",
    accent: "#2f6db5",
  },
} as const;

function buildTheme(mode: Mode): ThemeConfig {
  const p = PALETTE[mode];
  return {
    algorithm: mode === "dark" ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      colorPrimary: p.accent,
      colorInfo: p.accent,
      colorLink: p.accent,
      colorBgLayout: p.page,
      colorBgContainer: p.surface,
      colorBgElevated: p.raised,
      colorBorderSecondary: p.border,
      fontFamily: FONT,
      fontSize: 15,
      borderRadius: 6,
    },
    components: {
      Layout: { headerBg: p.surface, headerHeight: 56, headerPadding: "0 24px", bodyBg: p.page },
      Table: { headerBg: p.surface, rowHoverBg: mode === "dark" ? "#242930" : "#f5f7fa" },
      Card: { headerFontSize: 16 },
      // dark text on the lighter dark-mode accent; white on the deeper light-mode one
      Button: { primaryColor: mode === "dark" ? "#0d1724" : "#ffffff", primaryShadow: "none" },
    },
  };
}

const KEY = "packages-tracker.theme";
const Ctx = createContext<{ mode: Mode; toggle: () => void } | null>(null);

/** Dark unless the viewer chose light before (not tied to the OS setting). */
function initialMode(): Mode {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "light" || attr === "dark") return attr;
  try {
    if (localStorage.getItem(KEY) === "light") return "light";
  } catch {
    /* storage blocked */
  }
  return "dark";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Mode>(initialMode);
  useLayoutEffect(() => {
    const el = document.documentElement;
    el.setAttribute("data-theme", mode);
    el.style.colorScheme = mode;
    el.style.background = PALETTE[mode].page;
    try {
      localStorage.setItem(KEY, mode);
    } catch {
      /* storage blocked — choice just won't persist */
    }
  }, [mode]);
  const config = useMemo(() => buildTheme(mode), [mode]);
  const value = useMemo(() => ({ mode, toggle: () => setMode((m) => (m === "dark" ? "light" : "dark")) }), [mode]);
  return (
    <Ctx.Provider value={value}>
      <ConfigProvider theme={config}>{children}</ConfigProvider>
    </Ctx.Provider>
  );
}

export function useThemeMode() {
  const c = useContext(Ctx);
  if (!c) throw new Error("useThemeMode outside ThemeProvider");
  return c;
}
