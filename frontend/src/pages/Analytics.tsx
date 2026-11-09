// The platform admin's analytics (Admin → Analytics): how the whole service is
// doing. Totals and metadata only: the API never returns a workspace's scan
// results or keys. One date range above everything drives every panel; each
// chart has a table view and a CSV download.
import { DownloadOutlined } from "@ant-design/icons";
import { keepPreviousData, useQuery, type UseQueryResult } from "@tanstack/react-query";
import {
  Alert, App, Button, Card, Col, DatePicker, Descriptions, Row, Segmented, Skeleton, Space, Table, Tag,
  Tooltip, Typography,
} from "antd";
import dayjs from "dayjs";
import { useState, type ReactNode } from "react";
import { api, errorMessage } from "../api";
import { ChartCard, DayBars, HBars, HStack, Meter, RetentionGrid, StatTile, TimeLines, type Column } from "../components/charts";
import { WorkspaceDrawer } from "../components/WorkspaceDrawer";
import { flagInfo } from "../flags";
import { fmtDate, fmtDateTime, fmtRelative } from "../format";
import {
  byteTicks, downloadCsv, fmtBytes, fmtCount, fmtDay, fmtDayLong, fmtHour, fmtPct, fmtSecs, fmtUsd, lastDays, rangeDays, toCsv,
  type DayRange,
} from "../lib/analyticsFormat";
import { AI_ERROR, AI_STATUS, FLAG_TONE, OUTCOMES, STATUS } from "../lib/chartTheme";
import type { AnalyticsPanels, AnalyticsWorkspace } from "../types";

const PRESETS = [
  { label: "7 days", value: "7" },
  { label: "30 days", value: "30" },
  { label: "90 days", value: "90" },
  { label: "12 months", value: "365" },
];

const STAGE: Record<string, string> = {
  clone: "Clone",
  extract: "Read manifests",
  inspect: "Inspect images",
  enrich: "Look up versions",
  ai: "AI research",
  flag: "Flag",
  snapshot: "Save results",
  unknown: "Unknown",
};

const TRIGGER: Record<string, string> = {
  scheduled: "Schedule",
  manual: "Scan now",
  initial: "First scan",
  upload: "Zip upload",
};

function usePanel<K extends keyof AnalyticsPanels>(panel: K, range: DayRange, refetchInterval?: number) {
  return useQuery({
    queryKey: ["admin", "analytics", panel, range.start, range.end],
    queryFn: () => api.adminAnalytics(panel, range),
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    refetchInterval,
  });
}

/** A panel's data once it's here. While a new range loads, the old numbers
 *  stay on screen, faded (no skeleton flash). */
function Loaded<T>({ q, children }: { q: UseQueryResult<T>; children: (data: T, dimmed: boolean) => ReactNode }) {
  if (q.isError && !q.data) return <Alert type="error" showIcon message={errorMessage(q.error)} />;
  if (!q.data) return <Card size="small"><Skeleton active paragraph={{ rows: 5 }} /></Card>;
  return <>{children(q.data, q.isPlaceholderData)}</>;
}

function Section({ title, help, children }: { title: string; help?: ReactNode; children: ReactNode }) {
  return (
    <section style={{ marginTop: 28 }}>
      <Typography.Title level={4} style={{ marginBottom: 4 }}>{title}</Typography.Title>
      {help && <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>{help}</Typography.Paragraph>}
      {!help && <div style={{ height: 8 }} />}
      {children}
    </section>
  );
}

function Half({ children }: { children: ReactNode }) {
  return <Col xs={24} xl={12}>{children}</Col>;
}

function dayColumn<T extends { day: string }>(): Column<T> {
  return { key: "day", title: "Day (UTC)", render: (r) => fmtDayLong(r.day) };
}

export default function AnalyticsPage() {
  const { message } = App.useApp();
  const [preset, setPreset] = useState("30");
  const [range, setRange] = useState<DayRange>(() => lastDays(30));
  const overview = usePanel("overview", range);
  const growth = usePanel("growth", range);
  const activation = usePanel("activation", range);
  const retention = usePanel("retention", range);
  const scans = usePanel("scans", range);
  const content = usePanel("content", range);
  const ai = usePanel("ai", range);
  const system = usePanel("system", range, 60_000);
  const workspaces = usePanel("workspaces", range);
  const csv = (name: string) => `packages-tracker-${name}-${range.start}-to-${range.end}.csv`;
  const today = lastDays(1).end;

  return (
    <div>
      <Space wrap align="center" size={[12, 8]}>
        <Segmented
          value={preset}
          onChange={(v) => {
            if (v === "custom") return;
            setPreset(String(v));
            setRange(lastDays(Number(v)));
          }}
          options={[...PRESETS, ...(preset === "custom" ? [{ label: "Custom", value: "custom" }] : [])]}
        />
        <DatePicker.RangePicker
          allowClear={false}
          value={[dayjs(range.start), dayjs(range.end)]}
          disabledDate={(d) => d.format("YYYY-MM-DD") > today}
          onChange={(v) => {
            if (!v || !v[0] || !v[1]) return;
            const next = { start: v[0].format("YYYY-MM-DD"), end: v[1].format("YYYY-MM-DD") };
            if (rangeDays(next) > 366) {
              message.warning("Pick at most 366 days");
              return;
            }
            setRange(next);
            setPreset("custom");
          }}
        />
      </Space>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginTop: 8 }}>
        Days are UTC days. The numbers are worked out again at most every 5 minutes
        {overview.data ? ` (these at ${dayjs(overview.data.generated_at).format("HH:mm")})` : ""}.
        Only totals and metadata: never a workspace's scan results or keys.
      </Typography.Paragraph>

      <Loaded q={overview}>
        {(d, dimmed) => (
          <div style={{
            display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12,
            opacity: dimmed ? 0.55 : 1, transition: "opacity 0.2s",
          }}>
            {d.tiles.map((tile) => (
              <StatTile key={tile.key} tile={tile} days={d.range.days} format={tile.unit === "usd" ? fmtUsd : fmtCount} />
            ))}
          </div>
        )}
      </Loaded>

      <Section title="Growth">
        <Loaded q={growth}>
          {(d, dimmed) => (
            <Row gutter={[16, 16]}>
              <Half>
                <ChartCard title="Users" csvName={csv("users")} rows={d.days} dimmed={dimmed}
                  columns={[dayColumn(), { key: "users", title: "Users" }]}>
                  <TimeLines data={d.days} series={[{ key: "users", name: "Users" }]} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Active users" csvName={csv("active-users")} rows={d.days} dimmed={dimmed}
                  help="People who used the app that day, and in the 7 days up to it."
                  columns={[dayColumn(), { key: "active", title: "That day" }, { key: "active_7d", title: "In the 7 days to it" }]}>
                  <TimeLines data={d.days} series={[{ key: "active", name: "That day" }, { key: "active_7d", name: "In the 7 days to it" }]} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Sign-ups" csvName={csv("signups")} rows={d.days} dimmed={dimmed}
                  columns={[dayColumn(), { key: "signups", title: "Sign-ups" }]}>
                  <DayBars data={d.days} series={[{ key: "signups", name: "Sign-ups" }]} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="New repositories and teams" csvName={csv("added")} rows={d.days}
                  dimmed={dimmed}
                  columns={[dayColumn(), { key: "repos_added", title: "Repositories" },
                    { key: "workspaces_created", title: "Team workspaces" }]}>
                  <DayBars data={d.days} stacked={false} series={[{ key: "repos_added", name: "Repositories" },
                    { key: "workspaces_created", name: "Team workspaces" }]} />
                </ChartCard>
              </Half>
            </Row>
          )}
        </Loaded>
      </Section>

      <Section title="Activation and retention">
        <Row gutter={[16, 16]}>
          <Half>
            <Loaded q={activation}>
              {(d, dimmed) => {
                const pct = Object.fromEntries(d.steps.map((s) => [s.label, s.pct]));
                return (
                  <ChartCard title="What new people did" csvName={csv("activation")} dimmed={dimmed}
                    rows={d.cohort ? d.steps : []} emptyText="Nobody signed up in this range"
                    help={`Of the ${d.cohort} people who signed up in this range, how many went on to each step, at any time since. Each step is counted on its own.`}
                    columns={[{ key: "label", title: "Step" }, { key: "users", title: "People" },
                      { key: "pct", title: "% of sign-ups", value: (s) => s.pct ?? "" }]}>
                    <HBars name="People" rows={d.steps.map((s) => ({ label: s.label, value: s.users }))}
                      valueLabel={(r) => `${fmtCount(r.value)} · ${fmtPct(pct[r.label])}`} labelWidth={180} />
                  </ChartCard>
                );
              }}
            </Loaded>
          </Half>
          <Half>
            <Loaded q={retention}>
              {(d, dimmed) => (
                <ChartCard title="Retention by sign-up week" csvName={csv("retention")} dimmed={dimmed}
                  rows={d.cohorts.some((c) => c.users) ? d.cohorts : []}
                  emptyText="Nobody signed up in these 8 weeks"
                  help="Of the people who signed up each week (weeks start on Monday), the share who used the app that week and in each week after."
                  columns={[
                    { key: "week", title: "Week of", render: (c) => fmtDayLong(c.week) },
                    { key: "users", title: "People" },
                    ...Array.from({ length: 8 }, (_, k): Column<(typeof d.cohorts)[number]> => ({
                      key: `w${k}`, title: k === 0 ? "That week (%)" : `+${k} weeks (%)`, value: (c) => c.weeks[k] ?? "",
                    })),
                  ]}>
                  <RetentionGrid cohorts={d.cohorts} />
                </ChartCard>
              )}
            </Loaded>
          </Half>
        </Row>
      </Section>

      <Loaded q={scans}>
        {(d, dimmed) => (
          <Section title="Scans" help={`${fmtCount(d.totals.runs)} scans in this range; ${fmtPct(d.totals.success_pct)} gave results (complete or incomplete). A whole scan takes ${fmtSecs(d.duration.median)} (median), and the slowest 10% take ${fmtSecs(d.duration.p90)} or more. Waiting in the queue: ${fmtSecs(d.queue_wait.median)} (median).`}>
            <Row gutter={[16, 16]}>
              <Half>
                <ChartCard title="Scans per day" csvName={csv("scans")} rows={d.totals.runs ? d.days : []} dimmed={dimmed}
                  emptyText="No scans in this range"
                  columns={[dayColumn(), { key: "done", title: "Complete" }, { key: "degraded", title: "Incomplete" },
                    { key: "error", title: "Failed" }]}>
                  <DayBars data={d.days} series={OUTCOMES.map((o) => ({ key: o.key, name: o.name, color: STATUS[o.tone] }))} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Time per stage" csvName={csv("stage-times")} rows={d.stages} dimmed={dimmed}
                  emptyText="No scans in this range"
                  help="The median time each stage takes. Looking up versions includes the AI research."
                  columns={[{ key: "stage", title: "Stage", value: (s) => STAGE[s.stage] ?? s.stage },
                    { key: "runs", title: "Scans" }, { key: "median", title: "Median (s)" },
                    { key: "p90", title: "Slowest 10% from (s)" }]}>
                  <HBars name="Median" format={fmtSecs}
                    rows={d.stages.map((s) => ({ label: STAGE[s.stage] ?? s.stage, value: s.median ?? 0 }))} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Where scans failed" csvName={csv("failures")} rows={d.failures} dimmed={dimmed}
                  emptyText="No failed or incomplete scans"
                  help="The stage that reported the first problem."
                  columns={[{ key: "stage", title: "Stage", value: (f) => STAGE[f.stage] ?? f.stage },
                    { key: "error", title: "Failed" }, { key: "degraded", title: "Incomplete" }]}>
                  <HStack rows={d.failures.map((f) => ({ ...f, label: STAGE[f.stage] ?? f.stage }))}
                    series={[{ key: "error", name: "Failed", color: STATUS.critical },
                      { key: "degraded", name: "Incomplete", color: STATUS.warning }]} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="What started the scans" csvName={csv("triggers")} rows={d.triggers} dimmed={dimmed}
                  emptyText="No scans in this range"
                  columns={[{ key: "trigger", title: "Started by", value: (r) => TRIGGER[r.trigger] ?? r.trigger },
                    { key: "runs", title: "Scans" }]}>
                  <HBars name="Scans" rows={d.triggers.map((r) => ({ label: TRIGGER[r.trigger] ?? r.trigger, value: r.runs }))} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Outside services that failed" csvName={csv("upstream-errors")} rows={d.upstream_errors}
                  dimmed={dimmed} emptyText="No failed lookups"
                  help="Lookups to package registries and other services that failed during scans."
                  columns={[{ key: "host", title: "Service" }, { key: "kind", title: "Problem" }, { key: "count", title: "Lookups" }]}>
                  <HBars name="Failed lookups" labelWidth={210}
                    rows={d.upstream_errors.map((u) => ({ label: `${u.host} · ${u.kind.replace(/_/g, " ")}`, value: u.count }))} />
                </ChartCard>
              </Half>
            </Row>
          </Section>
        )}
      </Loaded>

      <Loaded q={content}>
        {(d, dimmed) => (
          <Section title="What people scan" help={`From the latest scan in this range of each branch (${fmtCount(d.branches)} branches).`}>
            <Row gutter={[16, 16]}>
              <Half>
                <ChartCard title="Packages by ecosystem" csvName={csv("ecosystems")} rows={d.ecosystems} dimmed={dimmed}
                  columns={[{ key: "ecosystem", title: "Ecosystem" }, { key: "packages", title: "Packages" },
                    { key: "repos", title: "Repositories" }]}>
                  <HBars name="Packages" rows={d.ecosystems.map((e) => ({ label: e.ecosystem, value: e.packages }))} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Packages by flag" csvName={csv("flags")} rows={d.flags} dimmed={dimmed}
                  help="Worst first."
                  columns={[{ key: "status", title: "Flag", value: (f) => flagInfo(f.status)?.label ?? f.status },
                    { key: "packages", title: "Packages" }]}>
                  <HBars name="Packages" rows={d.flags.map((f) => ({
                    label: flagInfo(f.status)?.label ?? f.status, value: f.packages,
                    color: STATUS[FLAG_TONE[f.status] ?? "neutral"],
                  }))} />
                </ChartCard>
              </Half>
              <Col xs={24}>
                <ChartCard title="Common end-of-life packages" csvName={csv("common-eol")} rows={d.common_eol} dimmed={dimmed}
                  emptyText={`No end-of-life package is in ${d.common_min_workspaces} or more workspaces yet`}
                  help={`Packages past their end of life in the latest scans of ${d.common_min_workspaces} or more workspaces. With fewer, a name could point at one workspace, so it isn't shown.`}
                  columns={[{ key: "name", title: "Package" }, { key: "ecosystem", title: "Ecosystem" },
                    { key: "workspaces", title: "Workspaces" }, { key: "repos", title: "Repositories" }]}>
                  <HBars name="Workspaces" labelWidth={220}
                    rows={d.common_eol.map((p) => ({ label: `${p.name} (${p.ecosystem})`, value: p.workspaces }))} />
                </ChartCard>
              </Col>
            </Row>
          </Section>
        )}
      </Loaded>

      <Loaded q={ai}>
        {(d, dimmed) => (
          <Section title="AI research" help={
            `AI research ran in ${fmtCount(d.totals.runs)} of ${fmtCount(d.totals.scans)} scans. Estimated spend ${fmtUsd(d.totals.cost_usd)}, on the workspaces' own keys.`
            + (d.totals.unpriced_runs ? ` ${fmtCount(d.totals.unpriced_runs)} of those scans used models without a known price and aren't in it.` : "")
          }>
            <Row gutter={[16, 16]}>
              <Half>
                <ChartCard title="How the AI passes went" csvName={csv("ai-status")} rows={d.statuses} dimmed={dimmed}
                  emptyText="No scans in this range"
                  columns={[{ key: "status", title: "Result", value: (s) => AI_STATUS[s.status]?.name ?? s.status },
                    { key: "runs", title: "Scans" }]}>
                  <HBars name="Scans" rows={d.statuses.map((s) => ({
                    label: AI_STATUS[s.status]?.name ?? s.status, value: s.runs,
                    color: STATUS[AI_STATUS[s.status]?.tone ?? "neutral"],
                  }))} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Providers" csvName={csv("ai-providers")} rows={d.providers} dimmed={dimmed}
                  emptyText="No AI research in this range"
                  columns={[{ key: "provider", title: "Provider" }, { key: "runs", title: "Scans" },
                    { key: "tokens_in", title: "Input tokens" }, { key: "tokens_out", title: "Output tokens" },
                    { key: "searches", title: "Searches" },
                    { key: "cost_usd", title: "Spend (USD)", render: (p) => fmtUsd(p.cost_usd) },
                    { key: "unpriced_runs", title: "Scans without a price" },
                    { key: "models", title: "Models", value: (p) => p.models.join(", ") }]}>
                  <HBars name="Scans" labelWidth={200} rows={d.providers.map((p) => ({ label: p.provider, value: p.runs }))} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Tokens per day" csvName={csv("ai-tokens")} rows={d.totals.runs ? d.days : []}
                  dimmed={dimmed} emptyText="No AI research in this range"
                  columns={[dayColumn(), { key: "tokens_in", title: "Input" }, { key: "tokens_out", title: "Output" }]}>
                  <TimeLines data={d.days} series={[{ key: "tokens_in", name: "Input" }, { key: "tokens_out", name: "Output" }]} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Estimated spend per day" csvName={csv("ai-spend")} rows={d.totals.runs ? d.days : []}
                  dimmed={dimmed} emptyText="No AI research in this range"
                  columns={[dayColumn(), { key: "cost_usd", title: "Spend (USD)", render: (r) => fmtUsd(r.cost_usd) }]}>
                  <DayBars data={d.days} format={fmtUsd} decimals series={[{ key: "cost_usd", name: "Spend" }]} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Web searches per day" csvName={csv("ai-searches")} rows={d.totals.runs ? d.days : []}
                  dimmed={dimmed} emptyText="No AI research in this range"
                  columns={[dayColumn(), { key: "searches", title: "Searches" }]}>
                  <DayBars data={d.days} series={[{ key: "searches", name: "Searches" }]} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="Why AI passes failed" csvName={csv("ai-errors")} rows={d.errors} dimmed={dimmed}
                  emptyText="No AI failures in this range"
                  columns={[{ key: "kind", title: "Reason", value: (e) => AI_ERROR[e.kind] ?? e.kind }, { key: "runs", title: "Scans" }]}>
                  <HBars name="Scans" rows={d.errors.map((e) => ({ label: AI_ERROR[e.kind] ?? e.kind, value: e.runs }))} />
                </ChartCard>
              </Half>
              <Half>
                <ChartCard title="What the research found" csvName={csv("ai-results")}
                  rows={d.results.some((r) => r.packages) ? d.results : []} dimmed={dimmed}
                  emptyText="No AI research in this range"
                  help="The packages the AI looked at, in the scans still kept from this range."
                  columns={[{ key: "label", title: "Result" }, { key: "packages", title: "Packages" }]}>
                  <HBars name="Packages" rows={d.results.map((r) => ({ label: r.label, value: r.packages }))} />
                </ChartCard>
              </Half>
            </Row>
          </Section>
        )}
      </Loaded>

      <Section title="System health">
        <Loaded q={system}>
          {(d) => {
            const n = d.now;
            const hourly = d.per === "hour";
            const samples = d.samples;
            const lines = (title: string, key: keyof (typeof samples)[number], format: (v: number) => string) => (
              <ChartCard title={title} csvName={csv(key)} rows={samples}
                emptyText="No readings in this range yet. The server takes one every hour."
                help={hourly ? "Hourly readings." : "Each day's highest reading."}
                columns={[{ key: "at", title: hourly ? "Time (UTC)" : "Day (UTC)",
                  render: (s) => (hourly ? fmtHour(s.at) : fmtDayLong(s.at)) },
                  { key, title, render: (s) => format(Number(s[key] ?? 0)) }]}>
                <TimeLines data={samples} x="at" series={[{ key, name: title }]} format={format} decimals
                  ticks={byteTicks(Math.max(0, ...samples.map((s) => Number(s[key] ?? 0))))}
                  xFormat={hourly ? fmtHour : fmtDay} labelFormat={hourly ? fmtHour : fmtDayLong} />
              </ChartCard>
            );
            return (
              <Row gutter={[16, 16]}>
                <Half>
                  <Card size="small" title="Right now" style={{ height: "100%" }}>
                    <Space direction="vertical" size={12} style={{ width: "100%" }}>
                      <Meter label="Disk (the data volume)" used={n.disk_used_bytes} total={n.disk_total_bytes} format={fmtBytes} />
                      <Meter label="Memory the app uses" used={n.rss_bytes} total={n.mem_total_bytes} format={fmtBytes} />
                      <Meter label="Scan workers busy" used={n.scans_running} total={d.workers} format={fmtCount} />
                      <Descriptions size="small" column={1} items={[
                        { key: "db", label: "Database", children: fmtBytes(n.db_bytes) },
                        { key: "uploads", label: "Uploaded zips", children: fmtBytes(n.uploads_bytes) },
                        { key: "queue", label: "Scans waiting", children: fmtCount(n.queue_waiting) },
                      ]} />
                    </Space>
                  </Card>
                </Half>
                <Half>{lines("Disk used", "disk_used_bytes", fmtBytes)}</Half>
                <Half>{lines("Database size", "db_bytes", fmtBytes)}</Half>
                <Half>{lines("App memory", "rss_bytes", fmtBytes)}</Half>
                <Half>
                  <ChartCard title="Scan queue" csvName={csv("queue")} rows={samples}
                    emptyText="No readings in this range yet. The server takes one every hour."
                    help={hourly ? "At each hourly reading." : "Each day's highest reading."}
                    columns={[{ key: "at", title: hourly ? "Time (UTC)" : "Day (UTC)",
                      render: (s) => (hourly ? fmtHour(s.at) : fmtDayLong(s.at)) },
                      { key: "queue_waiting", title: "Waiting" }, { key: "scans_running", title: "Running" }]}>
                    <TimeLines data={samples} x="at" series={[{ key: "queue_waiting", name: "Waiting" },
                      { key: "scans_running", name: "Running" }]}
                      xFormat={hourly ? fmtHour : fmtDay} labelFormat={hourly ? fmtHour : fmtDayLong} />
                  </ChartCard>
                </Half>
              </Row>
            );
          }}
        </Loaded>
      </Section>

      <Section title="Workspaces" help="Metadata and counts only. Opening one is recorded in its audit log.">
        <Loaded q={workspaces}>
          {(d, dimmed) => <WorkspacesTable rows={d.workspaces} dimmed={dimmed} csvName={csv("workspaces")} />}
        </Loaded>
      </Section>
    </div>
  );
}

const WORKSPACE_COLUMNS: Column<AnalyticsWorkspace>[] = [
  { key: "name", title: "Workspace" },
  { key: "personal", title: "Kind", value: (w) => (w.personal ? "personal" : "team") },
  { key: "members", title: "Members" },
  { key: "active_members", title: "Active members" },
  { key: "repos", title: "Repositories" },
  { key: "scans", title: "Scans" },
  { key: "failed_scans", title: "Failed scans" },
  { key: "ai_cost_usd", title: "AI spend (USD)" },
  { key: "last_scan_at", title: "Last scan" },
  { key: "created_at", title: "Created" },
  { key: "paused", title: "Scans paused", value: (w) => (w.paused ? "yes" : "no") },
  { key: "id", title: "Id" },
];

function WorkspacesTable({ rows, dimmed, csvName }: { rows: AnalyticsWorkspace[]; dimmed: boolean; csvName: string }) {
  const [open, setOpen] = useState<string | null>(null);
  const num = (k: keyof AnalyticsWorkspace) => (a: AnalyticsWorkspace, b: AnalyticsWorkspace) => Number(a[k]) - Number(b[k]);
  return (
    <Card
      size="small"
      styles={{ body: { padding: 0 } }}
      style={{ opacity: dimmed ? 0.55 : 1, transition: "opacity 0.2s" }}
      title={`${rows.length} workspaces`}
      extra={
        <Tooltip title="Download as CSV">
          <Button size="small" type="text" icon={<DownloadOutlined />} aria-label="Download workspaces as CSV"
            disabled={!rows.length} onClick={() => downloadCsv(csvName, toCsv(WORKSPACE_COLUMNS, rows))} />
        </Tooltip>
      }
    >
      <Table<AnalyticsWorkspace>
        rowKey="id"
        size="small"
        dataSource={rows}
        scroll={{ x: 980 }}
        pagination={{ pageSize: 25, hideOnSinglePage: true, showSizeChanger: false }}
        columns={[
          {
            title: "Workspace",
            sorter: (a, b) => a.name.localeCompare(b.name),
            render: (_, w) => (
              <Space size={4} wrap>
                <span>{w.name}</span>
                {w.personal && <Tag>personal</Tag>}
                {w.paused && <Tooltip title="Nobody has used it for a month, so its scheduled scans are paused"><Tag color="gold">paused</Tag></Tooltip>}
              </Space>
            ),
          },
          {
            title: "Members", width: 110, sorter: num("members"),
            render: (_, w) => <Tooltip title={`${w.active_members} used the app in this range`}>{w.members} ({w.active_members} active)</Tooltip>,
          },
          { title: "Repositories", dataIndex: "repos", width: 110, sorter: num("repos") },
          { title: "Scans", dataIndex: "scans", width: 80, sorter: num("scans"), defaultSortOrder: "descend" },
          { title: "Failed", dataIndex: "failed_scans", width: 80, sorter: num("failed_scans") },
          { title: "AI spend", width: 90, sorter: num("ai_cost_usd"), render: (_, w) => fmtUsd(w.ai_cost_usd) },
          {
            title: "Last scan", width: 110,
            sorter: (a, b) => (a.last_scan_at ?? "").localeCompare(b.last_scan_at ?? ""),
            render: (_, w) => <Tooltip title={fmtDateTime(w.last_scan_at)}>{w.last_scan_at ? fmtRelative(w.last_scan_at) : "never"}</Tooltip>,
          },
          {
            title: "Created", width: 120,
            sorter: (a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""),
            render: (_, w) => fmtDate(w.created_at),
          },
          {
            title: "", width: 90,
            render: (_, w) => <Button type="link" size="small" onClick={() => setOpen(w.id)}>Details</Button>,
          },
        ]}
      />
      <WorkspaceDrawer id={open} onClose={() => setOpen(null)} />
    </Card>
  );
}
