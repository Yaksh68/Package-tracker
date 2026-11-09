// A workspace's details for the platform admin: members, repositories and its
// own limits. Metadata only, never scan results or keys; opening it is
// recorded in the workspace's audit log (the API does that).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Descriptions, Drawer, InputNumber, Skeleton, Space, Table, Typography } from "antd";
import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { fmtDate, fmtRelative } from "../format";
import { ROLE_LABEL } from "../lib/permissions";
import { useDrawerWidth } from "../lib/responsive";

// per user, or about free scans without an account: a workspace can't override them
const NOT_PER_WORKSPACE = new Set(["workspaces_per_user", "trial_scans_per_day", "trial_scans_per_ip", "trial_hours"]);
const METERED = new Set(["repos_per_workspace", "members_per_workspace", "upload_storage_mb", "manual_scans_per_hour"]);

/** One workspace's own limits. Blank = the platform's value. */
function WorkspaceLimits({ id }: { id: string }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const q = useQuery({ queryKey: ["admin", "workspace-limits", id], queryFn: () => api.adminWorkspaceLimits(id) });
  const platform = useQuery({ queryKey: ["admin", "limits"], queryFn: api.adminLimits }).data;
  const [draft, setDraft] = useState<Record<string, number | null>>({});
  useEffect(() => {
    if (q.data) setDraft(q.data.overrides);
  }, [q.data]);
  const save = useMutation({
    mutationFn: () => {
      const before = q.data?.overrides ?? {};
      const names = new Set([...Object.keys(before), ...Object.keys(draft)]);
      const changed = Object.fromEntries(
        [...names].filter((n) => (draft[n] ?? null) !== (before[n] ?? null)).map((n) => [n, draft[n] ?? null]),
      );
      return api.setAdminWorkspaceLimits(id, changed);
    },
    onSuccess: (d) => {
      qc.setQueryData(["admin", "workspace-limits", id], d);
      message.success("Saved, and recorded in the workspace's audit log");
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  if (!q.data || !platform) return <Skeleton active paragraph={{ rows: 4 }} />;
  const { labels, usage } = q.data;
  const names = Object.keys(platform.limits).filter((n) => !NOT_PER_WORKSPACE.has(n));
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Table
        rowKey="name"
        size="small"
        pagination={false}
        dataSource={names.map((name) => ({ name }))}
        columns={[
          { title: "Limit", render: (_, r) => labels[r.name] },
          { title: "Uses", width: 70, render: (_, r) => (METERED.has(r.name) ? usage[r.name] ?? 0 : "") },
          {
            title: "Own value",
            width: 130,
            render: (_, r) => (
              <InputNumber
                size="small"
                min={1}
                max={100000}
                precision={0}
                style={{ width: 110 }}
                placeholder={String(platform.limits[r.name])}
                value={draft[r.name] ?? null}
                onChange={(v) => setDraft((old) => ({ ...old, [r.name]: v ?? null }))}
                aria-label={labels[r.name]}
              />
            ),
          },
        ]}
      />
      <Typography.Text type="secondary" style={{ fontSize: 13 }}>
        Leave a value blank for the platform's (shown greyed out).
      </Typography.Text>
      <Button type="primary" onClick={() => save.mutate()} loading={save.isPending}>Save limits</Button>
    </Space>
  );
}

export function WorkspaceDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["admin", "workspace", id], queryFn: () => api.adminWorkspace(id!), enabled: !!id });
  const ws = q.data;
  const drawerWidth = useDrawerWidth(560);
  return (
    <Drawer title={ws?.name ?? "Workspace"} open={!!id} onClose={onClose} width={drawerWidth}>
      {q.isError && <Alert type="error" showIcon message={errorMessage(q.error)} />}
      {ws && (
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Alert type="info" showIcon message="Opening a workspace is recorded in its audit log, where its Admins and Owners see it." />
          <Descriptions size="small" column={1} items={[
            { key: "kind", label: "Kind", children: ws.personal ? "Personal" : "Team" },
            { key: "id", label: "Id", children: <Typography.Text code>{ws.id}</Typography.Text> },
          ]} />
          <Typography.Title level={5} style={{ margin: 0 }}>Members ({ws.members.length})</Typography.Title>
          <Table
            rowKey="user_id"
            size="small"
            pagination={false}
            dataSource={ws.members}
            columns={[
              { title: "Login", dataIndex: "login" },
              { title: "Role", render: (_, m) => ROLE_LABEL[m.role] },
              { title: "Joined", render: (_, m) => fmtDate(m.joined_at) },
            ]}
          />
          <Typography.Title level={5} style={{ margin: 0 }}>Repositories ({ws.repos.length})</Typography.Title>
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={ws.repos}
            columns={[
              { title: "Name", render: (_, r) => r.name ?? r.id },
              { title: "Branches", render: (_, r) => r.branches.join(", ") },
              {
                title: "Last scan",
                render: (_, r) => {
                  const last = Object.values(r.last_scan_by_branch).sort().pop();
                  return last ? fmtRelative(last) : "never";
                },
              },
            ]}
          />
          <Typography.Title level={5} style={{ margin: 0 }}>Limits</Typography.Title>
          <WorkspaceLimits id={ws.id} />
        </Space>
      )}
    </Drawer>
  );
}
