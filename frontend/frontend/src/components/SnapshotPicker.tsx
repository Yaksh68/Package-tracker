import { Select } from "antd";
import { fmtRelative, fmtSnapshotTs } from "../format";
import type { SnapshotSummary } from "../types";

export function SnapshotPicker({
  snapshots,
  value,
  onChange,
}: {
  snapshots: SnapshotSummary[];
  value?: string;
  onChange: (ts: string) => void;
}) {
  return (
    <Select
      style={{ width: 300 }}
      value={value}
      onChange={onChange}
      placeholder="No scans yet"
      disabled={snapshots.length === 0}
      aria-label="Snapshot"
      options={snapshots.map((s, i) => ({
        value: s.ts,
        label: `${fmtSnapshotTs(s.ts)} · ${i === 0 ? "latest" : fmtRelative(s.ts)}${s.degraded ? " · incomplete" : ""}`,
      }))}
    />
  );
}
