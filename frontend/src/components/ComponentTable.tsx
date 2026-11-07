import { Input, Segmented, Select, Space, Switch, Table, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { fmtDate } from "../format";
import { FLAGS, RISK_FLAGS, flagRank } from "../flags";
import type { Component } from "../types";
import { FLAG_ORDER } from "../types";
import { FlagBadge } from "./FlagBadge";

interface Props {
  components: Component[];
  loading?: boolean;
  onSelect: (c: Component) => void;
  emptyText?: ReactNode;
}

const dash = <Typography.Text type="secondary">—</Typography.Text>;

/** Same package pinned in two manifests appears twice; keep row keys unique. */
function rowKeys(rows: Component[]): Map<Component, string> {
  const seen = new Map<string, number>();
  const out = new Map<Component, string>();
  for (const c of rows) {
    const base = `${c.ecosystem}:${c.name}:${c.version ?? ""}:${c.source_file}`;
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    out.set(c, n ? `${base}#${n}` : base);
  }
  return out;
}

export function ComponentTable({ components, loading, onSelect, emptyText }: Props) {
  const [search, setSearch] = useState("");
  const [ecosystem, setEcosystem] = useState<string | undefined>();
  const [flags, setFlags] = useState<string[]>([]);
  const [directOnly, setDirectOnly] = useState(false);
  const [updatesOnly, setUpdatesOnly] = useState(false);

  const ecosystems = useMemo(
    () => Array.from(new Set(components.map((c) => c.ecosystem))).sort(),
    [components],
  );

  // Only worth showing the direct/transitive filter when transitive deps exist
  // (e.g. a package-lock.json was scanned). Otherwise it has nothing to hide.
  const hasTransitive = useMemo(() => components.some((c) => !c.is_direct), [components]);
  // Same idea: no point offering "updates only" when nothing is upgradable.
  const hasUpdates = useMemo(() => components.some((c) => c.update_available), [components]);

  const keys = useMemo(() => rowKeys(components), [components]);

  const rows = useMemo(
    () =>
      components.filter((c) => {
        if (search && !c.name.toLowerCase().includes(search.toLowerCase())) return false;
        if (ecosystem && c.ecosystem !== ecosystem) return false;
        if (flags.length && !flags.includes(c.flag as string)) return false;
        if (directOnly && !c.is_direct) return false;
        if (updatesOnly && !c.update_available) return false;
        return true;
      }),
    [components, search, ecosystem, flags, directOnly, updatesOnly],
  );

  // Controlled pagination so page navigation is reliable; reset to page 1 only
  // when the filtered result set changes (not on unrelated re-renders/polls).
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  useEffect(() => setPage(1), [search, ecosystem, flags, directOnly, updatesOnly]);

  // Quick filters: one click instead of the Flag dropdown. They drive the same
  // `flags` filter, so the dropdown and the quick filter always agree.
  const present = useMemo(() => {
    const n: Record<string, number> = {};
    for (const c of components) if (c.flag) n[c.flag] = (n[c.flag] ?? 0) + 1;
    return n;
  }, [components]);
  // Stale has its own filter; "needs attention" is what has a concrete problem
  const riskPresent = RISK_FLAGS.filter((f) => f !== "STALE" && present[f]);
  const attentionCount = riskPresent.reduce((s, f) => s + present[f], 0);
  const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));
  const quickValue =
    flags.length === 0 ? "all" : sameSet(flags, riskPresent) ? "attention" : flags.length === 1 ? flags[0] : "";
  const quickOptions = [
    { value: "all", label: `All ${components.length}` },
    ...(attentionCount ? [{ value: "attention", label: `Needs attention ${attentionCount}` }] : []),
    // only statuses this snapshot has; the Status dropdown below lists them all
    ...FLAG_ORDER.filter((f) => present[f]).map((f) => ({ value: f, label: `${FLAGS[f].label} ${present[f]}` })),
  ];

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      {components.length > 0 && (
        <Segmented
          value={quickValue}
          onChange={(v) => setFlags(v === "all" ? [] : v === "attention" ? riskPresent : [v as string])}
          options={quickOptions}
          style={{ marginBottom: 4 }}
        />
      )}
      <Space wrap>
        <Input.Search
          placeholder="Search name"
          allowClear
          onChange={(e) => setSearch(e.target.value)}
          style={{ width: 220 }}
        />
        <Select
          placeholder="Ecosystem"
          allowClear
          style={{ width: 150 }}
          value={ecosystem}
          onChange={setEcosystem}
          options={ecosystems.map((e) => ({ value: e, label: e }))}
        />
        <Select
          mode="multiple"
          placeholder="Flag"
          allowClear
          maxTagCount="responsive"
          style={{ minWidth: 240, maxWidth: 460 }}
          value={flags}
          onChange={setFlags}
          options={FLAG_ORDER.map((f) => ({
            value: f,
            label: `${FLAGS[f].label} (${present[f] ?? 0})`,
            disabled: !present[f],
          }))}
        />
        {hasTransitive && (
          <Space>
            Direct only <Switch checked={directOnly} onChange={setDirectOnly} />
          </Space>
        )}
        {hasUpdates && (
          <Space>
            <Tooltip title="Only packages with a newer stable release available">
              <span>Updates only</span>
            </Tooltip>
            <Switch checked={updatesOnly} onChange={setUpdatesOnly} />
          </Space>
        )}
      </Space>

      <Table<Component>
        dataSource={rows}
        loading={loading}
        rowKey={(c) => keys.get(c) ?? `${c.ecosystem}:${c.name}`}
        locale={emptyText ? { emptyText } : undefined}
        size="small"
        onRow={(c) => ({ onClick: () => onSelect(c), style: { cursor: "pointer" } })}
        pagination={{
          current: page,
          pageSize,
          total: rows.length,
          showSizeChanger: true,
          showTotal: (t) => `${t} components`,
          pageSizeOptions: [10, 25, 50, 100],
          onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          style: { marginRight: 16 },
        }}
        columns={[
          {
            title: "Name",
            dataIndex: "name",
            sorter: (a, b) => a.name.localeCompare(b.name),
            render: (name: string, c: Component) => (
              <Space size={6}>
                <span>{name}</span>
                {c.ai_researched && (
                  <Tooltip title="Verdict informed by the AI research agent (web search)">
                    <Tag style={{ marginInlineEnd: 0 }}>AI</Tag>
                  </Tooltip>
                )}
              </Space>
            ),
          },
          {
            title: "Status",
            dataIndex: "flag",
            width: 200,
            render: (f: string | null) => <FlagBadge flag={f} />,
            // most severe first by default; rows without a flag always last
            defaultSortOrder: "ascend",
            sorter: (a, b) => flagRank(a.flag) - flagRank(b.flag) || a.name.localeCompare(b.name),
          },
          { title: "Version", dataIndex: "version", width: 130, render: (v: string | null) => v ?? dash },
          {
            title: "Latest",
            dataIndex: "latest_stable_version",
            width: 170,
            render: (v: string | null, c: Component) =>
              v ? (
                <Space size={6}>
                  <span>{v}</span>
                  {c.update_available && (
                    <Tooltip title={`A newer stable release (${v}) is available`}>
                      <Tag color="green" style={{ marginInlineEnd: 0 }}>↑ update</Tag>
                    </Tooltip>
                  )}
                </Space>
              ) : (
                dash
              ),
            sorter: (a, b) => Number(!!b.update_available) - Number(!!a.update_available),
          },
          {
            title: (
              <Tooltip title="End of life: the date this version stops receiving fixes, including security fixes.">
                <span>End of life</span>
              </Tooltip>
            ),
            dataIndex: "eol_date",
            width: 130,
            render: (d: string | null) => (d ? fmtDate(d) : dash),
            // soonest first; no date last
            sorter: (a, b) => (a.eol_date ?? "9999").localeCompare(b.eol_date ?? "9999"),
          },
          { title: "Ecosystem", dataIndex: "ecosystem", width: 120 },
          { title: "Confidence", dataIndex: "confidence", width: 110, render: (v: string | null) => v ?? dash },
        ]}
      />
    </Space>
  );
}
