import { useQuery } from "@tanstack/react-query";
import { Breadcrumb, Button, Card, Checkbox, Empty, Select, Space, Steps, Table, Tag, Typography, theme } from "antd";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { AiUsageCard } from "../components/AiUsage";
import { ScanIssueAlert } from "../components/ScanIssueAlert";
import { RepoTabs } from "../components/RepoTabs";
import { ScanStatusPill } from "../components/ScanStatusPill";
import { fmtSnapshotTs, fmtTime, repoName } from "../format";
import type { LogEntry, ScanIssue } from "../types";

// Must list every stage the pipeline can fail in — a stage missing here has its
// errors silently dropped from the progress bar (`inspect` used to be, so a
// failed image pull showed an all-green run).
const STAGE_ORDER = ["clone", "extract", "inspect", "enrich", "flag", "snapshot"];
const LEVEL_COLOR: Record<string, string> = {
  info: "default", warn: "orange", error: "red", tool: "default", result: "green",
};
// package lookups and AI research are logged under their own names but are
// part of the enrich step
const STEP_OF: Record<string, string> = { source: "enrich", agent: "enrich" };

function stageSteps(entries: LogEntry[], state: string) {
  const step = (s: string) => STEP_OF[s] ?? s;
  const seen = new Set(entries.map((e) => step(e.stage)));
  const errored = new Set(entries.filter((e) => e.level === "error").map((e) => step(e.stage)));
  const lastStage = entries.length ? step(entries[entries.length - 1].stage) : "";
  return STAGE_ORDER.map((s) => {
    let status: "wait" | "process" | "finish" | "error" = "wait";
    if (errored.has(s)) status = "error";
    else if (seen.has(s)) status = state === "running" && s === lastStage ? "process" : "finish";
    return { title: s, status };
  });
}

/** The trace is the only error record that survives a backend restart and the
 * only one a *past* snapshot has at all, so the summary is derived from it
 * rather than from the live ScanStatus. */
function issuesFromLog(entries: LogEntry[]): ScanIssue[] {
  return entries
    .filter((e) => e.level === "error")
    .map((e) => ({ stage: e.stage, message: e.message, fatal: e.stage === "scan" }));
}

export function ScanActivity() {
  const { token } = theme.useToken();
  const { id = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const [ts, setTs] = useState<string | undefined>();

  const { data: repo } = useQuery({
    queryKey: ["repo", id],
    queryFn: () => api.getRepo(id),
  });

  const requested = searchParams.get("branch");
  const branch = requested && repo?.branches.includes(requested) ? requested : repo?.branches[0];

  useEffect(() => {
    if (repo && branch && searchParams.get("branch") !== branch) {
      setSearchParams({ branch }, { replace: true });
    }
  }, [repo, branch, searchParams, setSearchParams]);

  const { data: snapshots = [] } = useQuery({
    queryKey: ["snapshots", id, branch],
    queryFn: () => api.snapshots(id, branch!),
    enabled: !!branch,
  });

  const { data: log } = useQuery({
    queryKey: ["scan-log", id, branch, ts],
    queryFn: () => api.scanLog(id, branch!, ts),
    enabled: !!branch,
    refetchInterval: (q) => (q.state.data?.state === "running" ? 2000 : false),
  });

  const allEntries = log?.entries ?? [];
  const issues = issuesFromLog(allEntries);

  // "Errors only" collapses a 300-event trace to the handful of lines that
  // explain a degraded run — the common reason for opening this page.
  const [errorsOnly, setErrorsOnly] = useState(false);
  const entries = errorsOnly ? allEntries.filter((e) => e.level === "error") : allEntries;

  // Controlled pagination so live polling (every 2s while running) doesn't snap
  // the user back to page 1.
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  useEffect(() => setPage(1), [ts, errorsOnly]);  // reset when the view changes
  useEffect(() => setTs(undefined), [branch]);  // reset "live" view when switching branches
  useEffect(() => { if (issues.length === 0) setErrorsOnly(false); }, [issues.length]);

  return (
    <div>
      <Breadcrumb
        style={{ marginBottom: 12 }}
        items={[
          { title: <Link to="/">Repositories</Link> },
          { title: <Link to={`/repos/${id}${branch ? `?branch=${encodeURIComponent(branch)}` : ""}`}>{repoName(repo, id)}</Link> },
        ]}
      />
      <Typography.Title level={3} style={{ margin: "0 0 8px" }}>{repoName(repo, id)}</Typography.Title>
      <RepoTabs id={id} branch={branch} />
      <Space style={{ marginBottom: 16, width: "100%", justifyContent: "space-between" }} wrap>
        <Space size="small">
          <Select
            size="small"
            style={{ width: 200 }}
            value={branch}
            onChange={(b) => setSearchParams({ branch: b })}
            options={(repo?.branches ?? []).map((b) => ({ value: b, label: b }))}
          />
          <ScanStatusPill state={log?.state} />
        </Space>
        <Select
          allowClear
          style={{ width: 300, maxWidth: "calc(100vw - 24px)" }}
          placeholder="Live (current run) — or pick a past snapshot"
          value={ts}
          onChange={setTs}
          options={snapshots.map((s) => ({ value: s.ts, label: `${fmtSnapshotTs(s.ts)}${s.degraded ? " (degraded)" : ""}` }))}
        />
      </Space>

      <Card style={{ marginBottom: 16 }}>
        <Steps size="small" items={stageSteps(allEntries, log?.state ?? "idle")} />
      </Card>

      <AiUsageCard usage={log?.ai_usage} />

      <ScanIssueAlert
        issues={issues}
        state={log?.state}
        action={
          !errorsOnly && (
            <Button size="small" onClick={() => setErrorsOnly(true)}>
              Show only these
            </Button>
          )
        }
      />

      <Card
        title={
          <Space>
            <span>Trace ({entries.length} events)</span>
            {issues.length > 0 && (
              <Checkbox checked={errorsOnly} onChange={(e) => setErrorsOnly(e.target.checked)}>
                Errors only ({issues.length})
              </Checkbox>
            )}
          </Space>
        }
        styles={{ body: { padding: 0 } }}
      >
        {entries.length === 0 ? (
          <Empty description="No activity yet — run a scan" style={{ padding: 32 }} />
        ) : (
          <Table<LogEntry>
            dataSource={entries.map((e, i) => ({ ...e, _k: i }))}
            rowKey="_k"
            size="small"
            scroll={{ x: 640 }}
            // tint error rows so they're findable while scrolling an unfiltered trace
            onRow={(r) => (r.level === "error"
              ? { style: { background: token.colorErrorBg } }
              : {})}
            pagination={{
              current: page,
              pageSize,
              total: entries.length,
              showSizeChanger: true,
              showTotal: (t) => `${t} events`,
              pageSizeOptions: [25, 50, 100, 200],
              onChange: (p, ps) => { setPage(p); setPageSize(ps); },
              style: { marginRight: 16 },
            }}
            expandable={{
              rowExpandable: (r) => !!r.data,
              expandedRowRender: (r) => (
                <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12 }}>
                  {JSON.stringify(r.data, null, 2)}
                </pre>
              ),
            }}
            columns={[
              {
                title: "Time",
                dataIndex: "ts",
                width: 110,
                render: (t: string) => fmtTime(t),
              },
              {
                title: "Stage",
                dataIndex: "stage",
                width: 110,
                render: (s: string) => <Tag>{s}</Tag>,
                filters: Array.from(new Set(entries.map((e) => e.stage))).map((s) => ({ text: s, value: s })),
                onFilter: (v, r) => r.stage === v,
              },
              {
                title: "Level",
                dataIndex: "level",
                width: 90,
                render: (l: string) => <Tag color={LEVEL_COLOR[l] ?? "default"}>{l}</Tag>,
              },
              { title: "Message", dataIndex: "message" },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
