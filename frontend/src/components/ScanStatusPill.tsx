import { Badge } from "antd";

const MAP: Record<string, { status: "default" | "processing" | "success" | "error" | "warning"; text: string }> = {
  idle: { status: "default", text: "Idle" },
  queued: { status: "warning", text: "Queued" },
  running: { status: "processing", text: "Scanning" },
  done: { status: "success", text: "Up to date" },
  error: { status: "error", text: "Scan failed" },
};

/** "1st", "2nd", "3rd", "11th"… */
export function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  return `${n}${["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
}

/** The scan state, with the phase while running and the place in the scan
 *  queue while waiting (1 = next to start). */
export function scanStateText(state?: string, phase?: string, position?: number | null): string {
  const m = MAP[state ?? "idle"] ?? MAP.idle;
  if (state === "running" && phase && phase !== "running") return `${m.text} · ${phase}`;
  if (state === "queued" && position) return position === 1 ? `${m.text} · next` : `${m.text} · ${ordinal(position)} in line`;
  return m.text;
}

export function ScanStatusPill({ state, phase, position }: { state?: string; phase?: string; position?: number | null }) {
  const m = MAP[state ?? "idle"] ?? MAP.idle;
  return <Badge status={m.status} text={scanStateText(state, phase, position)} />;
}
