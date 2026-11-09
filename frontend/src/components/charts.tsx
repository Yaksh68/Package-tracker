// Chart building blocks for the admin analytics: a card with a chart/table
// switch and CSV export, stat tiles, and thin Recharts wrappers in one house
// style: 2px lines, bars at most 24px thick with rounded ends, a hairline
// grid, muted axes, a legend whenever there are two or more series, text in
// text colours only, and every value also in the card's table view.
import { DownloadOutlined } from "@ant-design/icons";
import { Button, Card, Empty, Progress, Segmented, Space, Table, Tooltip as AntTooltip, Typography, theme } from "antd";
import { useMemo, useState, type ReactNode } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, LabelList, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import {
  change, downloadCsv, fmtCount, fmtDay, fmtDayLong, fmtPct, toCsv, type CsvColumn,
} from "../lib/analyticsFormat";
import { chartTheme, STATUS, type ChartTheme } from "../lib/chartTheme";
import { useThemeMode } from "../theme";
import type { AnalyticsTile, RetentionCohort } from "../types";

export function useChartTheme(): ChartTheme {
  const { mode } = useThemeMode();
  return useMemo(() => chartTheme(mode), [mode]);
}

// ---- the card --------------------------------------------------------------------
export interface Column<T> extends CsvColumn<T> {
  /** How the table view shows it (default: the CSV value). */
  render?: (row: T) => ReactNode;
  align?: "left" | "right";
}

export function ChartCard<T extends object>({
  title, help, rows, columns, csvName, children, emptyText, dimmed,
}: {
  title: string;
  help?: ReactNode;
  rows: T[];
  columns: Column<T>[];
  csvName: string;
  children: ReactNode;
  emptyText?: string;
  /** The previous range's numbers, shown faded while the new ones load. */
  dimmed?: boolean;
}) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const empty = rows.length === 0;
  const keyed = useMemo(() => rows.map((r, i) => ({ ...r, __key: i })), [rows]);
  return (
    <Card
      size="small"
      title={title}
      style={{ height: "100%", opacity: dimmed ? 0.55 : 1, transition: "opacity 0.2s" }}
      extra={
        <Space size={4}>
          <Segmented
            size="small"
            value={view}
            onChange={(v) => setView(v as "chart" | "table")}
            options={[{ label: "Chart", value: "chart" }, { label: "Table", value: "table" }]}
          />
          <AntTooltip title="Download as CSV">
            <Button
              size="small"
              type="text"
              icon={<DownloadOutlined />}
              aria-label={`Download ${title} as CSV`}
              disabled={empty}
              onClick={() => downloadCsv(csvName, toCsv(columns, rows))}
            />
          </AntTooltip>
        </Space>
      }
    >
      {help && <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 8 }}>{help}</Typography.Paragraph>}
      {empty ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText ?? "Nothing in this range"} />
      ) : view === "chart" ? (
        children
      ) : (
        <Table
          size="small"
          rowKey="__key"
          dataSource={keyed}
          scroll={{ x: true }}
          pagination={rows.length > 10 ? { pageSize: 10, size: "small", showSizeChanger: false } : false}
          columns={columns.map((c) => ({
            key: c.key,
            title: c.title,
            align: c.align,
            render: (_: unknown, row: T) => {
              if (c.render) return c.render(row);
              const v = c.value ? c.value(row) : (row as Record<string, unknown>)[c.key];
              return v == null || v === "" ? "—" : String(v);
            },
          }))}
        />
      )}
    </Card>
  );
}

// ---- tooltip -----------------------------------------------------------------------
interface TipItem {
  name?: string | number;
  value?: unknown;
  color?: string;
  dataKey?: unknown;
  payload?: Record<string, unknown>;
}

/** Values first, the series name after, keyed by a short line in its colour. */
function ChartTip({ active, payload, label, format, labelFormat }: {
  active?: boolean;
  payload?: readonly TipItem[];
  label?: unknown;
  format: (n: number) => string;
  labelFormat?: (l: string) => string;
}) {
  const { token } = theme.useToken();
  if (!active || !payload?.length) return null;
  return (
    <div style={{
      background: token.colorBgElevated, border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 6,
      padding: "6px 10px", boxShadow: token.boxShadowSecondary, fontSize: 13, lineHeight: 1.6,
    }}>
      {label != null && label !== "" && (
        <div style={{ color: token.colorTextSecondary }}>{labelFormat ? labelFormat(String(label)) : String(label)}</div>
      )}
      {payload.map((p, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span aria-hidden style={{
            width: 12, height: 2, borderRadius: 1, display: "inline-block",
            background: (p.payload?.__color as string | undefined) ?? p.color,
          }} />
          <span style={{ color: token.colorText, fontWeight: 600 }}>
            {typeof p.value === "number" ? format(p.value) : String(p.value ?? "—")}
          </span>
          <span style={{ color: token.colorTextSecondary }}>{String(p.name ?? "")}</span>
        </div>
      ))}
    </div>
  );
}

function axis(t: ChartTheme) {
  return { tick: { fill: t.muted, fontSize: 12 }, tickLine: false, axisLine: { stroke: t.axis } } as const;
}

function legendText(t: ChartTheme) {
  return (value: unknown) => <span style={{ color: t.secondary, fontSize: 12 }}>{String(value)}</span>;
}

export interface Series {
  key: string;
  name: string;
  color?: string;
}

/** Keep the legend in the series' own order (Recharts sorts it by name). */
function legendOrder(series: Series[]) {
  return (item: { dataKey?: unknown }) => series.findIndex((s) => s.key === item.dataKey);
}


// ---- over time ----------------------------------------------------------------------
/** Lines over days (or hours): 2px, no dots, a crosshair that finds the day. */
export function TimeLines<T extends object>({
  data, x = "day", series, format = fmtCount, xFormat = fmtDay, labelFormat = fmtDayLong, decimals = false, height = 220,
  ticks,
}: {
  data: T[];
  x?: string;
  series: Series[];
  format?: (n: number) => string;
  xFormat?: (v: string) => string;
  labelFormat?: (v: string) => string;
  decimals?: boolean;
  height?: number;
  /** Fixed y ticks (e.g. `byteTicks`); the axis then ends at the last one. */
  ticks?: number[];
}) {
  const t = useChartTheme();
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke={t.grid} />
        <XAxis dataKey={x} tickFormatter={(v) => xFormat(String(v))} minTickGap={28} {...axis(t)} />
        <YAxis tickFormatter={(v) => format(Number(v))} width={60} allowDecimals={decimals} {...axis(t)} axisLine={false}
          {...(ticks ? { ticks, domain: [0, ticks[ticks.length - 1]] } : {})} />
        <Tooltip
          cursor={{ stroke: t.axis, strokeWidth: 1 }}
          content={(p) => <ChartTip active={p.active} payload={p.payload as readonly TipItem[]} label={p.label}
            format={format} labelFormat={labelFormat} />}
        />
        {series.length > 1 && <Legend iconType="plainline" formatter={legendText(t)} itemSorter={legendOrder(series)} />}
        {series.map((s, i) => (
          <Line key={s.key} type="linear" dataKey={s.key} name={s.name} stroke={s.color ?? t.series[i % 2]}
            strokeWidth={2} dot={false} activeDot={{ r: 4, stroke: t.surface, strokeWidth: 2 }}
            isAnimationActive={false} connectNulls />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Columns per day, stacked when there are several series (a 2px surface gap
 *  between the segments), side by side otherwise. */
export function DayBars<T extends object>({
  data, series, format = fmtCount, stacked = true, decimals = false, height = 220,
}: {
  data: T[];
  series: Series[];
  format?: (n: number) => string;
  stacked?: boolean;
  decimals?: boolean;
  height?: number;
}) {
  const t = useChartTheme();
  const stack = stacked && series.length > 1;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke={t.grid} />
        <XAxis dataKey="day" tickFormatter={(v) => fmtDay(String(v))} minTickGap={28} {...axis(t)} />
        <YAxis tickFormatter={(v) => format(Number(v))} width={60} allowDecimals={decimals} {...axis(t)} axisLine={false} />
        <Tooltip
          cursor={{ fill: t.hover }}
          content={(p) => <ChartTip active={p.active} payload={p.payload as readonly TipItem[]} label={p.label}
            format={format} labelFormat={fmtDayLong} />}
        />
        {series.length > 1 && <Legend iconType="square" formatter={legendText(t)} itemSorter={legendOrder(series)} />}
        {series.map((s, i) => (
          <Bar key={s.key} dataKey={s.key} name={s.name} stackId={stack ? "day" : undefined}
            fill={s.color ?? t.series[i % 2]} maxBarSize={24}
            radius={!stack || i === series.length - 1 ? [4, 4, 0, 0] : 0}
            stroke={stack ? t.surface : undefined} strokeWidth={stack ? 2 : 0} isAnimationActive={false} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---- categories ---------------------------------------------------------------------
export interface BarRow {
  label: string;
  value: number;
  color?: string;
}

function CategoryTick({ x, y, payload, width, fill }: {
  x?: number | string; y?: number | string; payload?: { value?: unknown }; width: number; fill: string;
}) {
  const text = String(payload?.value ?? "");
  const room = Math.max(6, Math.floor(width / 6.6));
  const shown = text.length > room ? `${text.slice(0, room - 1)}…` : text;
  return (
    <text x={x} y={y} dy={4} textAnchor="end" fill={fill} fontSize={12}>
      <title>{text}</title>
      {shown}
    </text>
  );
}

/** Horizontal bars, one per category, the value at the tip. One colour unless
 *  the colour means something (a status), given per row. */
export function HBars({
  rows, name, format = fmtCount, valueLabel, labelWidth = 170,
}: {
  rows: BarRow[];
  name: string;
  format?: (n: number) => string;
  valueLabel?: (row: BarRow) => string;
  labelWidth?: number;
}) {
  const t = useChartTheme();
  const data = rows.map((r) => ({
    ...r, __color: r.color ?? t.series[0], __text: valueLabel ? valueLabel(r) : format(r.value),
  }));
  return (
    <ResponsiveContainer width="100%" height={rows.length * 32 + 12}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 72, bottom: 4, left: 4 }}>
        <XAxis type="number" hide domain={[0, "dataMax"]} />
        <YAxis type="category" dataKey="label" width={labelWidth} interval={0} tickLine={false}
          axisLine={{ stroke: t.axis }} tick={(p) => <CategoryTick {...p} width={labelWidth} fill={t.secondary} />} />
        <Tooltip
          cursor={{ fill: t.hover }}
          content={(p) => <ChartTip active={p.active} payload={p.payload as readonly TipItem[]} label={p.label}
            format={format} />}
        />
        {/* a zero still gets its "0" label: an invisible 1px bar carries it */}
        <Bar dataKey="value" name={name} maxBarSize={20} radius={[0, 4, 4, 0]} isAnimationActive={false}
          minPointSize={(v) => (v ? 0 : 1)}>
          {data.map((d) => <Cell key={d.label} fill={d.value ? d.__color : "transparent"} />)}
          <LabelList dataKey="__text" position="right" style={{ fill: t.secondary, fontSize: 12 }} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Horizontal bars split into series (e.g. failed / incomplete), with a legend. */
export function HStack<T extends { label: string }>({
  rows, series, format = fmtCount, labelWidth = 170,
}: {
  rows: T[];
  series: Series[];
  format?: (n: number) => string;
  labelWidth?: number;
}) {
  const t = useChartTheme();
  return (
    <ResponsiveContainer width="100%" height={rows.length * 32 + 48}>
      <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 4 }}>
        <XAxis type="number" hide />
        <YAxis type="category" dataKey="label" width={labelWidth} interval={0} tickLine={false}
          axisLine={{ stroke: t.axis }} tick={(p) => <CategoryTick {...p} width={labelWidth} fill={t.secondary} />} />
        <Tooltip
          cursor={{ fill: t.hover }}
          content={(p) => <ChartTip active={p.active} payload={p.payload as readonly TipItem[]} label={p.label}
            format={format} />}
        />
        <Legend iconType="square" formatter={legendText(t)} itemSorter={legendOrder(series)} />
        {series.map((s, i) => (
          <Bar key={s.key} dataKey={s.key} name={s.name} stackId="row" fill={s.color ?? t.series[i % 2]} maxBarSize={20}
            radius={i === series.length - 1 ? [0, 4, 4, 0] : 0} stroke={t.surface} strokeWidth={2}
            isAnimationActive={false} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---- figures ------------------------------------------------------------------------
/** A headline number: its value, the change since the previous period (an
 *  arrow and a sign, not colour alone), and a 30-day trend with the selected
 *  range drawn in the accent. */
export function StatTile({ tile, days, format }: {
  tile: AnalyticsTile;
  days: number;
  format: (n: number) => string;
}) {
  const t = useChartTheme();
  const { token } = theme.useToken();
  const better = tile.better === "lower" ? "lower" : tile.unit === "usd" ? "none" : "higher";
  const d = change(tile.value, tile.previous, { better, format });
  const toneColor = d.tone === "good" ? t.up : d.tone === "bad" ? t.down : token.colorTextSecondary;
  const split = Math.max(0, tile.trend.length - Math.min(days, tile.trend.length));
  const spark = tile.trend.map((p, i) => ({
    day: p.day, before: i <= split ? p.value : null, within: i >= split ? p.value : null,
  }));
  return (
    <Card size="small" styles={{ body: { padding: "12px 14px" } }} style={{ height: "100%" }}>
      <Typography.Text type="secondary" style={{ fontSize: 13 }}>{tile.label}</Typography.Text>
      <div style={{ fontSize: 26, fontWeight: 600, lineHeight: 1.25, color: token.colorText }}>{format(tile.value)}</div>
      <div style={{ fontSize: 12, color: toneColor }}>
        {d.direction !== "flat" && <span aria-hidden>{d.direction === "up" ? "▲ " : "▼ "}</span>}
        {d.text}
        <span style={{ color: token.colorTextTertiary }}> vs the {days} days before</span>
      </div>
      <div style={{ height: 32, marginTop: 6 }} aria-hidden>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={spark} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
            <YAxis hide domain={["dataMin", "dataMax"]} />
            {split > 0 && <Line dataKey="before" stroke={t.muted} strokeWidth={1.5} dot={false} isAnimationActive={false} />}
            <Line dataKey="within" stroke={t.series[0]} strokeWidth={2} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}

/** How full something is. The fill turns amber from 75% and red from 90%. */
export function Meter({ label, used, total, format }: {
  label: string;
  used: number | null;
  total: number | null;
  format: (n: number) => string;
}) {
  const t = useChartTheme();
  if (used == null || !total) {
    return (
      <div style={{ fontSize: 13 }}>
        <span>{label}</span> <Typography.Text type="secondary">— not measured on this server</Typography.Text>
      </div>
    );
  }
  const pct = Math.min(100, (used / total) * 100);
  const fill = pct >= 90 ? STATUS.critical : pct >= 75 ? STATUS.warning : t.series[0];
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 13, flexWrap: "wrap" }}>
        <span>{label}</span>
        <Typography.Text type="secondary">{format(used)} of {format(total)} · {Math.round(pct)}%</Typography.Text>
      </div>
      <Progress percent={pct} showInfo={false} strokeColor={fill} trailColor={t.meterTrack} size="small"
        aria-label={`${label}: ${Math.round(pct)}% used`} />
    </div>
  );
}

/** Weekly cohorts: one row per sign-up week, the % still using the app in each
 *  week after, shaded on one blue ramp. */
export function RetentionGrid({ cohorts }: { cohorts: RetentionCohort[] }) {
  const t = useChartTheme();
  const { token } = theme.useToken();
  const weeks = Math.max(1, ...cohorts.map((c) => c.weeks.length));
  const head = { fontWeight: 500, padding: "2px 6px", color: token.colorTextSecondary } as const;
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ borderCollapse: "separate", borderSpacing: 2, width: "100%", minWidth: 480, fontSize: 12 }}>
        <thead>
          <tr>
            <th style={{ ...head, textAlign: "left" }}>Signed up (week of)</th>
            <th style={{ ...head, textAlign: "right" }}>People</th>
            {Array.from({ length: weeks }, (_, k) => (
              <th key={k} style={{ ...head, textAlign: "center" }}>{k === 0 ? "That week" : `+${k}`}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cohorts.map((c) => (
            <tr key={c.week}>
              <td style={{ padding: "2px 6px", whiteSpace: "nowrap" }}>{fmtDay(c.week)}</td>
              <td style={{ padding: "2px 6px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{c.users}</td>
              {Array.from({ length: weeks }, (_, k) => {
                const v = c.weeks[k];
                if (v == null || c.users === 0) {
                  return (
                    <td key={k} style={{ textAlign: "center", color: token.colorTextQuaternary }}>
                      {k < c.weeks.length ? "–" : ""}
                    </td>
                  );
                }
                const { background, color } = t.heat(v / 100);
                const back = Math.round((v * c.users) / 100);
                return (
                  <td key={k} title={`${back} of ${c.users} (${fmtPct(v)})`} style={{
                    background, color, textAlign: "center", borderRadius: 4, padding: "4px 2px",
                    fontVariantNumeric: "tabular-nums",
                  }}>
                    {Math.round(v)}%
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
