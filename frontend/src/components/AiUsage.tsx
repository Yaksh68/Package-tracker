import { Card, Descriptions, Tag, Typography } from "antd";
import { providerLabel } from "../lib/aiSettings";
import type { AiStatus, AiUsage } from "../types";

const STATUS: Record<AiStatus, { text: string; color?: string }> = {
  ok: { text: "Completed", color: "success" },
  partial: { text: "Partly failed", color: "warning" },
  failed: { text: "Failed", color: "error" },
  quota: { text: "Stopped by a quota", color: "warning" },
  no_key: { text: "Not set up", color: "default" },
  off: { text: "Off", color: "default" },
};

/** "anthropic" → "Anthropic (Claude)", "openai-compatible:groq" → "Groq". */
export function fmtProvider(label: string | null): string {
  if (!label) return "—";
  if (label === "claude-cli") return "Claude Code";
  const [kind, preset] = label.split(":");
  if (kind === "openai-compatible") return preset && preset !== "custom" ? providerLabel(preset) : "Custom endpoint";
  return providerLabel(kind);
}

/** "$0.0335", "$1.20", "free", or "not estimated". */
export function fmtCost(usd: number | null): string {
  if (usd === null) return "not estimated";
  if (usd === 0) return "free";
  return `$${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(2)}`;
}

/** What one scan's AI research used. Shown on Scan Activity. */
export function AiUsageCard({ usage }: { usage?: AiUsage | null }) {
  if (!usage) return null;
  const status = usage.status ?? "off";
  if (status === "off") {
    return (
      <Card size="small" style={{ marginBottom: 16 }}>
        <Typography.Text type="secondary">AI research was off for this scan.</Typography.Text>
      </Card>
    );
  }
  const s = STATUS[status] ?? { text: status };
  const ran = usage.batches > 0;
  return (
    <Card size="small" title="AI research" style={{ marginBottom: 16 }}>
      <Descriptions
        size="small"
        column={{ xs: 1, sm: 2, lg: 4 }}
        items={[
          {
            key: "result",
            label: "Result",
            children: (
              <span>
                <Tag color={s.color}>{s.text}</Tag>
                {usage.detail && <Typography.Text type="secondary">{usage.detail}</Typography.Text>}
              </span>
            ),
            span: { xs: 1, sm: 2, lg: 4 },
          },
          { key: "provider", label: "Provider", children: fmtProvider(usage.provider) },
          { key: "model", label: "Model", children: usage.model ?? "—" },
          ...(ran
            ? [
                {
                  key: "batches",
                  label: "Batches",
                  children: usage.failed_batches ? `${usage.batches} (${usage.failed_batches} failed)` : String(usage.batches),
                },
                {
                  key: "tokens",
                  label: "Tokens",
                  children: `${usage.tokens_in.toLocaleString()} in · ${usage.tokens_out.toLocaleString()} out`,
                },
                { key: "searches", label: "Web searches", children: usage.searches.toLocaleString() },
                { key: "cost", label: "Estimated cost", children: fmtCost(usage.cost_usd) },
                ...(usage.seconds !== null
                  ? [{ key: "time", label: "Time", children: `${Math.round(usage.seconds)}s` }]
                  : []),
              ]
            : []),
        ]}
      />
    </Card>
  );
}
