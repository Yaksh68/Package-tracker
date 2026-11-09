// Chart colours for the admin analytics, per theme. The series and status
// colours come from the data-viz reference palette; the two series colours
// were checked (colour-blind separation, contrast) against this app's own card
// surfaces in both themes. Text never takes a series colour: axes and labels
// use the muted and secondary inks.
import { PALETTE, type Mode } from "../theme";

export type StatusTone = "good" | "warning" | "serious" | "critical" | "neutral";

// Fixed in both themes; they always come with a label (legend or axis).
export const STATUS: Record<StatusTone, string> = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
  neutral: "#898781",
};

// The sequential blue ramp (light to dark), for the retention grid.
const BLUE_RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6",
  "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"];

export interface ChartTheme {
  mode: Mode;
  surface: string;
  grid: string;
  axis: string;
  muted: string;
  secondary: string;
  hover: string;
  series: [string, string];
  up: string;
  down: string;
  meterTrack: string;
  /** A retention cell's fill and text colour for 0..1. */
  heat: (t: number) => { background: string; color: string };
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function chartTheme(mode: Mode): ChartTheme {
  const dark = mode === "dark";
  // near zero recedes towards the surface: light-to-dark on light, dark-to-light on dark
  const ramp = dark ? [...BLUE_RAMP].reverse() : BLUE_RAMP;
  return {
    mode,
    surface: PALETTE[mode].surface,
    grid: PALETTE[mode].border,
    axis: dark ? "#3a4048" : "#c3c2b7",
    muted: "#898781",
    secondary: dark ? "#c3c2b7" : "#52514e",
    hover: dark ? "rgba(255,255,255,0.05)" : "rgba(11,11,11,0.04)",
    series: dark ? ["#3987e5", "#d95926"] : ["#2a78d6", "#eb6834"],
    up: dark ? "#0ca30c" : "#006300",
    down: "#d03b3b",
    meterTrack: dark ? "#184f95" : "#cde2fb",
    heat: (t: number) => {
      const step = ramp[Math.min(ramp.length - 1, Math.max(0, Math.round(t * (ramp.length - 1))))];
      return { background: step, color: luminance(step) > 0.35 ? "#0b0b0b" : "#ffffff" };
    },
  };
}

/** How serious each flag is, worst first, for the "flags" chart. */
export const FLAG_TONE: Record<string, StatusTone> = {
  ALREADY_EOL: "critical",
  VULNERABLE: "critical",
  EOS_PASSED: "critical",
  EOL_BEFORE_RELEASE: "serious",
  EOL_SOON_AFTER_RELEASE: "warning",
  DEPRECATED: "warning",
  ARCHIVED: "warning",
  STALE: "warning",
  UNKNOWN: "neutral",
  OK: "good",
  IGNORED: "neutral",
};

export const OUTCOMES = [
  { key: "done", name: "Complete", tone: "good" },
  { key: "degraded", name: "Incomplete", tone: "warning" },
  { key: "error", name: "Failed", tone: "critical" },
] as const;

export const AI_STATUS: Record<string, { name: string; tone: StatusTone }> = {
  ok: { name: "Worked", tone: "good" },
  partial: { name: "Partly failed", tone: "warning" },
  quota: { name: "Hit a rate limit or quota", tone: "serious" },
  failed: { name: "Failed", tone: "critical" },
  no_key: { name: "No AI key", tone: "neutral" },
  off: { name: "Off", tone: "neutral" },
};

export const AI_ERROR: Record<string, string> = {
  auth: "Key rejected",
  quota: "Rate limit or quota",
  config: "AI not set up",
  timeout: "Timed out",
  invalid: "Unreadable answer",
  error: "Call failed",
  other: "Other",
};
