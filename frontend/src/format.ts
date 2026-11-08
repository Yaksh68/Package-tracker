// Human-readable date/time helpers.
import dayjs from "dayjs";

/** Snapshot ids look like `2026-07-14T10-30-00Z` (dashes in the time part). */
function snapshotDate(ts?: string | null): Date | null {
  if (!ts) return null;
  const m = ts.match(/^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z$/);
  if (!m) return null;
  const d = new Date(`${m[1]}T${m[2]}:${m[3]}:${m[4]}Z`);
  return isNaN(d.getTime()) ? null : d;
}

function toDate(v?: string | null): Date | null {
  if (!v) return null;
  const d = snapshotDate(v) ?? new Date(v);
  return isNaN(d.getTime()) ? null : d;
}

/** "4 Oct 2026, 13:00" in local time. */
export function fmtDateTime(v?: string | null): string {
  const d = toDate(v);
  return d ? dayjs(d).format("D MMM YYYY, HH:mm") : "—";
}

export const fmtSnapshotTs = fmtDateTime;

/** "17 Aug 2026" for calendar dates like release / EOL dates. */
export function fmtDate(v?: string | null): string {
  if (!v) return "—";
  const d = dayjs(v.slice(0, 10));
  return d.isValid() ? d.format("D MMM YYYY") : v;
}

function span(secs: number): string {
  if (secs < 90) return `${Math.max(1, Math.round(secs))}s`;
  if (secs < 5400) return `${Math.round(secs / 60)}m`;
  if (secs < 129600) return `${Math.round(secs / 3600)}h`;
  if (secs < 45 * 86400) return `${Math.round(secs / 86400)}d`;
  if (secs < 548 * 86400) return `${Math.round(secs / (30.44 * 86400))}mo`;
  return `${Math.round(secs / (365.25 * 86400))}y`;
}

/** "in 3h" / "2d ago". */
export function fmtRelative(v?: string | null): string {
  const d = toDate(v);
  if (!d) return "—";
  const secs = (d.getTime() - Date.now()) / 1000;
  if (Math.abs(secs) < 30) return "just now";
  return secs > 0 ? `in ${span(secs)}` : `${span(-secs)} ago`;
}

/** For a scheduled time: "in 3h", or "overdue 2d" once it has passed. */
export function fmtDue(v?: string | null): string {
  const d = toDate(v);
  if (!d) return "—";
  const secs = (d.getTime() - Date.now()) / 1000;
  if (secs > -120) return secs > 0 ? `in ${span(secs)}` : "due now";
  return `overdue ${span(-secs)}`;
}

/** What to call a repo: its name, else its id (the public mode's ids are random). */
export function repoName(repo?: { id: string; name?: string | null } | null, fallbackId = ""): string {
  return repo?.name || repo?.id || fallbackId;
}

/** Local time only, for dense logs. */
export function fmtTime(v?: string | null): string {
  const d = toDate(v);
  return d ? dayjs(d).format("HH:mm:ss") : "—";
}
