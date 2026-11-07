import { Badge } from "antd";

const MAP: Record<string, { status: "default" | "processing" | "success" | "error" | "warning"; text: string }> = {
  idle: { status: "default", text: "Idle" },
  queued: { status: "warning", text: "Queued" },
  running: { status: "processing", text: "Scanning" },
  done: { status: "success", text: "Up to date" },
  error: { status: "error", text: "Scan failed" },
};

export function ScanStatusPill({ state, phase }: { state?: string; phase?: string }) {
  const m = MAP[state ?? "idle"] ?? MAP.idle;
  const text = state === "running" && phase && phase !== "running" ? `${m.text} · ${phase}` : m.text;
  return <Badge status={m.status} text={text} />;
}
