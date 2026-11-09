// Formatting and CSV export for the admin analytics.

/** Days are UTC days on the server; "2026-11-09" -> "9 Nov". */
export function fmtDay(iso: string): string {
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
}

/** "9 Nov 2026". */
export function fmtDayLong(iso: string): string {
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/** An hourly reading's time, in UTC like the days: "9 Nov, 14:00". */
export function fmtHour(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const day = d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  return `${day}, ${d.toISOString().slice(11, 16)}`;
}

/** 1,284 in full below 10,000; 12.9K / 3.4M above. */
export function fmtCount(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs < 10_000) return Math.round(n).toLocaleString("en-US");
  if (abs < 1_000_000) return `${trim(n / 1000)}K`;
  if (abs < 1_000_000_000) return `${trim(n / 1_000_000)}M`;
  return `${trim(n / 1_000_000_000)}B`;
}

function trim(v: number): string {
  return v.toFixed(1).replace(/\.0$/, "");
}

/** "$0.02", "$1.50", "$1,234". Small amounts keep the cents that matter. */
export function fmtUsd(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return "—";
  if (n === 0) return "$0";
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4).replace(/0+$/, "")}`;
  if (Math.abs(n) < 1000) return `$${n.toFixed(2)}`;
  return `$${Math.round(n).toLocaleString("en-US")}`;
}

/** 1536 -> "1.5 KB" (1024-based, like the disk tools on the server). */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${i === 0 ? Math.round(v) : trim(v)} ${units[i]}`;
}

/** Round ticks for a byte axis, in whole KB/MB/GB steps (ticks that are
 *  round in bytes show as 139.7 GB). The last tick is at or above `max`. */
export function byteTicks(max: number): number[] {
  if (!(max > 0)) return [0];
  let unit = 1;
  while (max / unit >= 1024) unit *= 1024;
  const span = max / unit;
  const step = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000].find((s) => span / s <= 5) ?? 1000;
  const ticks: number[] = [];
  for (let v = 0; v < span + step; v += step) ticks.push(v * unit);
  return ticks;
}

/** Seconds -> "0.4 s", "42 s", "3 min", "1.5 h". */
export function fmtSecs(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return "—";
  if (n < 10) return `${trim(n)} s`;
  if (n < 90) return `${Math.round(n)} s`;
  if (n < 5400) return `${trim(n / 60)} min`;
  return `${trim(n / 3600)} h`;
}

export function fmtPct(n: number | null | undefined): string {
  return n == null || isNaN(n) ? "—" : `${trim(n)}%`;
}

export type Tone = "good" | "bad" | "neutral";

/** The change from the previous period: "+3", "−$0.40", "no change", and
 *  whether that's good news (up is good unless `better` is "lower"; with no
 *  direction given, neither). */
export function change(
  value: number,
  previous: number,
  opts: { better?: "higher" | "lower" | "none"; format?: (n: number) => string } = {},
): { text: string; tone: Tone; direction: "up" | "down" | "flat" } {
  const diff = Math.round((value - previous) * 10_000) / 10_000;
  const format = opts.format ?? fmtCount;
  if (diff === 0) return { text: "no change", tone: "neutral", direction: "flat" };
  const direction = diff > 0 ? "up" : "down";
  const better = opts.better ?? "higher";
  const tone: Tone = better === "none" ? "neutral" : (direction === "up") === (better === "higher") ? "good" : "bad";
  return { text: `${diff > 0 ? "+" : "−"}${format(Math.abs(diff))}`, tone, direction };
}

// ---- CSV ----------------------------------------------------------------------
export interface CsvColumn<T> {
  key: string;
  title: string;
  /** The value as written to the CSV (default: the raw field). */
  value?: (row: T) => string | number | boolean | null | undefined;
}

function cell(v: unknown): string {
  if (v == null) return "";
  let s = String(v);
  // a spreadsheet would run a cell starting with these as a formula
  if (/^[=+\-@\t\r]/.test(s) && !/^-?\d+(\.\d+)?$/.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv<T>(columns: CsvColumn<T>[], rows: T[]): string {
  const head = columns.map((c) => cell(c.title)).join(",");
  const body = rows.map((r) =>
    columns.map((c) => cell(c.value ? c.value(r) : (r as Record<string, unknown>)[c.key])).join(","));
  return [head, ...body].join("\r\n") + "\r\n";
}

export function downloadCsv(filename: string, csv: string): void {
  const url = URL.createObjectURL(new Blob(["﻿", csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- date ranges ---------------------------------------------------------------
export interface DayRange {
  start: string;
  end: string;
}

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** The last `days` UTC days, today included. */
export function lastDays(days: number, now: Date = new Date()): DayRange {
  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const start = new Date(end.getTime() - (days - 1) * 86_400_000);
  return { start: isoDay(start), end: isoDay(end) };
}

/** How many days a range covers, both ends included. */
export function rangeDays(r: DayRange): number {
  return Math.round((Date.parse(`${r.end}T00:00:00Z`) - Date.parse(`${r.start}T00:00:00Z`)) / 86_400_000) + 1;
}
