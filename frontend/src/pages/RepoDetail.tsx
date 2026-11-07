import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Breadcrumb, Button, Result, Select, Skeleton, Space, Typography } from "antd";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, errorMessage, isNotFound } from "../api";
import { fmtDate, fmtDateTime, fmtRelative } from "../format";
import { AiStatusTag } from "../components/AiStatusTag";
import { CitationsDrawer } from "../components/CitationsDrawer";
import { ComponentTable } from "../components/ComponentTable";
import { ScanIssueAlert } from "../components/ScanIssueAlert";
import { ScanStatusPill } from "../components/ScanStatusPill";
import { RepoTabs } from "../components/RepoTabs";
import { SnapshotPicker } from "../components/SnapshotPicker";
import type { Component } from "../types";

export function RepoDetail() {
  const { id = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { message } = App.useApp();
  // a snapshot the user picked; otherwise the page follows the latest one
  const [pinnedTs, setPinnedTs] = useState<string>();
  const [selected, setSelected] = useState<Component | null>(null);

  const repoQuery = useQuery({
    queryKey: ["repo", id],
    queryFn: () => api.getRepo(id),
  });
  const repo = repoQuery.data;

  // The branch this page is showing — from ?branch=, falling back to the
  // repo's first tracked branch. Each branch scans independently and has its
  // own snapshot history, so everything below is scoped to this one branch.
  const requested = searchParams.get("branch");
  const branch = (requested && repo?.branches.includes(requested) ? requested : repo?.branches[0]);

  // Once the repo has loaded, make sure the URL reflects a real branch — either
  // filling in the default, or correcting a stale/invalid ?branch= from an old link.
  useEffect(() => {
    if (repo && branch && searchParams.get("branch") !== branch) {
      setSearchParams({ branch }, { replace: true });
    }
  }, [repo, branch, searchParams, setSearchParams]);

  const { data: status } = useQuery({
    queryKey: ["scan-status", id, branch],
    queryFn: () => api.scanStatus(id, branch!),
    enabled: !!branch,
    // fast while a scan runs, slow otherwise (a scheduled scan can start any time)
    refetchInterval: (q) => (["running", "queued"].includes(q.state.data?.state ?? "") ? 3000 : 20000),
  });

  const { data: snapshots = [] } = useQuery({
    queryKey: ["snapshots", id, branch],
    queryFn: () => api.snapshots(id, branch!),
    enabled: !!branch,
  });

  // When a scan finishes, refresh the snapshot list so the page moves to the new results.
  const prevState = useRef<string | undefined>(undefined);
  useEffect(() => {
    const was = prevState.current;
    prevState.current = status?.state;
    if ((was === "running" || was === "queued") && status && !["running", "queued"].includes(status.state)) {
      qc.invalidateQueries({ queryKey: ["snapshots", id, branch] });
      qc.invalidateQueries({ queryKey: ["repos"] });
      if (status.state === "error") message.error("Scan failed — see Activity for details");
      else if (!pinnedTs) message.success("Scan finished — showing the new results");
    }
  }, [status, id, branch, qc, message, pinnedTs]);

  // Switching branches drops a snapshot picked on the old one.
  useEffect(() => setPinnedTs(undefined), [branch]);
  const ts = pinnedTs && snapshots.some((s) => s.ts === pinnedTs) ? pinnedTs : snapshots[0]?.ts;
  const currentSnapshot = snapshots.find((s) => s.ts === ts);

  const { data: components = [], isLoading } = useQuery({
    queryKey: ["components", id, branch, ts],
    queryFn: () => api.components(id, branch!, ts!),
    enabled: !!ts && !!branch,
    placeholderData: (prev) => prev, // keep rows on screen while the next snapshot loads
  });

  const scanMut = useMutation({
    mutationFn: () => api.scanNow(id, branch!),
    onSuccess: () => {
      message.info("Scan started");
      // show "Scanning" right away instead of waiting for the next poll
      qc.setQueryData(["scan-status", id, branch], (s: typeof status) => (s ? { ...s, state: "running", phase: "" } : s));
      qc.invalidateQueries({ queryKey: ["scan-status", id, branch] });
    },
    onError: (e) => message.error(errorMessage(e)),
  });

  // Disabled while queued/running AND during the request itself, so it can't be
  // re-clicked in the gap before the next status poll reflects the running scan.
  const scanning =
    scanMut.isPending || status?.state === "running" || status?.state === "queued";

  const branchQuery = branch ? `?branch=${encodeURIComponent(branch)}` : "";

  if (repoQuery.isError) {
    return isNotFound(repoQuery.error) ? (
      <Result status="404" title={`Repository "${id}" not found`} extra={<Link to="/">Back to repositories</Link>} />
    ) : (
      <Result
        status="error"
        title="Couldn't load this repository"
        subTitle={errorMessage(repoQuery.error)}
        extra={<Button onClick={() => repoQuery.refetch()}>Retry</Button>}
      />
    );
  }
  if (!repo) return <Skeleton active paragraph={{ rows: 8 }} />;

  const lastScan = repo.last_scan_by_branch?.[branch ?? ""];

  return (
    <div>
      <Breadcrumb
        style={{ marginBottom: 12 }}
        items={[{ title: <Link to="/">Repositories</Link> }, { title: id }]}
      />
      <Typography.Title level={3} style={{ margin: "0 0 4px" }}>{id}</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
        {repo.release_date ? `Release ${fmtDate(repo.release_date)}` : "No release date set"}
        {" · "}
        <span title={fmtDateTime(lastScan)}>{lastScan ? `scanned ${fmtRelative(lastScan)}` : "not scanned yet"}</span>
      </Typography.Paragraph>
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
          <ScanStatusPill state={status?.state} phase={status?.phase} />
        </Space>
        <Space>
          <AiStatusTag status={currentSnapshot?.ai_status} detail={currentSnapshot?.ai_detail} />
          <SnapshotPicker
            snapshots={snapshots}
            value={ts}
            onChange={(v) => setPinnedTs(v === snapshots[0]?.ts ? undefined : v)}
          />
          {ts && branch && (
            <Button href={api.reportCsvUrl(id, branch, ts)} download>Download CSV</Button>
          )}
          <Button
            type="primary"
            loading={scanning}
            disabled={scanning || !branch}
            onClick={() => scanMut.mutate()}
          >
            {scanning ? "Scanning…" : "Scan now"}
          </Button>
        </Space>
      </Space>

      <ScanIssueAlert
        issues={status?.issues}
        degraded={status?.degraded}
        state={status?.state}
        fallbackMessage={status?.message}
        action={
          <Button size="small" onClick={() => navigate(`/repos/${id}/activity${branchQuery}`)}>
            View details
          </Button>
        }
      />

      <ComponentTable
        components={components}
        loading={isLoading}
        onSelect={setSelected}
        emptyText={
          snapshots.length === 0
            ? scanning
              ? "The first scan is running — results appear here when it finishes."
              : "Not scanned yet. Use Scan now to check this branch."
            : undefined
        }
      />
      <CitationsDrawer component={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
