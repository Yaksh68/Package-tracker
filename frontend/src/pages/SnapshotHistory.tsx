import { useQuery } from "@tanstack/react-query";
import { Breadcrumb, Card, Empty, Select, Space, Table, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { RepoTabs } from "../components/RepoTabs";
import { FlagBadge, FlagCounts } from "../components/FlagBadge";
import { flagRank } from "../flags";
import { fmtSnapshotTs, repoName } from "../format";
import { useIsMobile } from "../lib/responsive";
import type { SnapshotSummary } from "../types";

export function SnapshotHistory() {
  const { id = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const [from, setFrom] = useState<string>();
  const [to, setTo] = useState<string>();

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

  useEffect(() => {
    setFrom(undefined);
    setTo(undefined);
  }, [branch]);  // switching branches invalidates the picked snapshot pair

  useEffect(() => {
    if (snapshots.length >= 2 && !from && !to) {
      setTo(snapshots[0].ts);
      setFrom(snapshots[1].ts);
    }
  }, [snapshots, from, to]);

  const { data: diff } = useQuery({
    queryKey: ["diff", id, branch, from, to],
    queryFn: () => api.diff(id, branch!, from!, to!),
    enabled: !!branch && !!from && !!to && from !== to,
  });

  const opts = snapshots.map((s: SnapshotSummary) => ({ value: s.ts, label: fmtSnapshotTs(s.ts) }));
  const mobile = useIsMobile();
  // on a phone the two pickers go in the card body: the header has no room
  const pickers = (
    <Space wrap>
      <Select style={{ width: 240, maxWidth: "calc(100vw - 48px)" }} placeholder="from" value={from} onChange={setFrom} options={opts} />
      <Select style={{ width: 240, maxWidth: "calc(100vw - 48px)" }} placeholder="to" value={to} onChange={setTo} options={opts} />
    </Space>
  );

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
      <Space style={{ marginBottom: 16 }}>
        <Select
          size="small"
          style={{ width: 200 }}
          value={branch}
          onChange={(b) => setSearchParams({ branch: b })}
          options={(repo?.branches ?? []).map((b) => ({ value: b, label: b }))}
        />
      </Space>

      <Card title="Flag-count trend" style={{ marginBottom: 16 }}>
        {snapshots.length === 0 ? (
          <Empty description="No snapshots yet" />
        ) : (
          <Table<SnapshotSummary>
            dataSource={snapshots}
            rowKey="ts"
            size="small"
            pagination={false}
            scroll={{ x: 520 }}
            columns={[
              { title: "Snapshot", dataIndex: "ts", render: (ts: string) => fmtSnapshotTs(ts) },
              { title: "Packages", dataIndex: "package_count", width: 100 },
              {
                title: "Flags",
                render: (_, s) => <FlagCounts counts={s.flag_counts} empty="no packages" />,
              },
              {
                title: "",
                width: 100,
                render: (_, s) => (s.degraded ? <Tag color="warning">incomplete</Tag> : null),
              },
            ]}
          />
        )}
      </Card>

      <Card
        title="Diff between snapshots"
        extra={mobile ? undefined : pickers}
      >
        {mobile && <div style={{ marginBottom: 12 }}>{pickers}</div>}
        {!diff ? (
          <Empty description="Pick two different snapshots" />
        ) : (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Table
              title={() => `Changed (${diff.changed.length})`}
              dataSource={diff.changed}
              rowKey={(r) => `${r.name}:${r.from}:${r.to}`}
              size="small"
              pagination={false}
              locale={{ emptyText: "No status changes" }}
              columns={[
                { title: "Package", dataIndex: "name" },
                { title: "From", dataIndex: "from", render: (f: string) => <FlagBadge flag={f} /> },
                { title: "To", dataIndex: "to", render: (f: string) => <FlagBadge flag={f} /> },
              ]}
            />
            {(["added", "removed"] as const).map((k) => (
              <Table
                key={k}
                title={() => `${k === "added" ? "Added" : "Removed"} (${diff[k].length})`}
                dataSource={[...diff[k]].sort((a, b) => flagRank(a.flag) - flagRank(b.flag))}
                rowKey={(r) => `${k}:${r.name}:${r.flag}`}
                size="small"
                pagination={diff[k].length > 10 ? { pageSize: 10, size: "small" } : false}
                locale={{ emptyText: `Nothing ${k}` }}
                columns={[
                  { title: "Package", dataIndex: "name" },
                  { title: "Status", dataIndex: "flag", render: (f: string) => <FlagBadge flag={f} /> },
                ]}
              />
            ))}
          </Space>
        )}
      </Card>
    </div>
  );
}
