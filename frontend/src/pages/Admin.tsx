// The platform admin's area: every user and workspace on the service, and the
// limits. It shows accounts and metadata only, never scan results or keys.
// Charts arrive with Phase 6.
import { UserOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App, Avatar, Button, Card, Descriptions, Drawer, Form, InputNumber, Popconfirm, Result, Skeleton, Space,
  Table, Tabs, Tag, Tooltip, Typography,
} from "antd";
import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { fmtDate, fmtDateTime, fmtRelative } from "../format";
import { ROLE_LABEL } from "../lib/permissions";
import { useSession } from "../session";
import type { AdminUser, AdminWorkspace, LimitValues } from "../types";

function UsersTab() {
  const { me } = useSession();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const q = useQuery({ queryKey: ["admin", "users"], queryFn: api.adminUsers });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["admin"] });
  };
  const statusMut = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "active" | "disabled" }) => api.setUserStatus(id, status),
    onSuccess: (_d, { status }) => {
      message.success(status === "disabled" ? "Disabled and signed out" : "Enabled");
      refresh();
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  const deleteMut = useMutation({
    mutationFn: (id: string) => api.adminDeleteUser(id),
    onSuccess: () => {
      message.success("Deleted");
      refresh();
    },
    onError: (e) => message.error(errorMessage(e)),
  });

  if (q.isError) return <Alert type="error" showIcon message={errorMessage(q.error)} />;
  return (
    <Card styles={{ body: { padding: 0 } }}>
      <Table<AdminUser>
        rowKey="id"
        size="small"
        loading={q.isLoading}
        dataSource={q.data}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        columns={[
          {
            title: "User",
            render: (_, u) => (
              <Space>
                <Avatar size="small" src={u.avatar_url ?? undefined} icon={<UserOutlined />} />
                <span>
                  <Typography.Text strong>{u.login}</Typography.Text>
                  {u.name && <Typography.Text type="secondary"> · {u.name}</Typography.Text>}
                </span>
              </Space>
            ),
          },
          {
            title: "Status",
            width: 170,
            render: (_, u) => (
              <Space size={4}>
                {u.status === "disabled" ? <Tag color="red">disabled</Tag> : <Tag color="green">active</Tag>}
                {u.platform_role === "admin" && <Tag color="purple">admin</Tag>}
              </Space>
            ),
          },
          { title: "Workspaces", dataIndex: "workspaces", width: 110 },
          { title: "Signed up", width: 130, render: (_, u) => fmtDate(u.created_at) },
          {
            title: "Last seen",
            width: 130,
            render: (_, u) => <Tooltip title={fmtDateTime(u.last_seen_at)}>{fmtRelative(u.last_seen_at)}</Tooltip>,
          },
          {
            title: "",
            width: 190,
            render: (_, u) => {
              const self = u.id === me.user.id;
              const why = self ? "That's you" : undefined;
              const disabling = u.status === "active";
              return (
                <Space size={0}>
                  <Popconfirm
                    title={disabling ? `Disable ${u.login}?` : `Enable ${u.login}?`}
                    description={disabling ? "They're signed out everywhere at once and can't sign in." : undefined}
                    disabled={self}
                    onConfirm={() => statusMut.mutate({ id: u.id, status: disabling ? "disabled" : "active" })}
                  >
                    <Tooltip title={why}>
                      <Button type="text" size="small" disabled={self}>{disabling ? "Disable" : "Enable"}</Button>
                    </Tooltip>
                  </Popconfirm>
                  <Popconfirm
                    title={`Delete ${u.login}?`}
                    description="Deletes their account, their personal workspace and any team workspace only they are in. Teams they were the last Owner of pass to their highest-ranked member."
                    okText="Delete"
                    okButtonProps={{ danger: true }}
                    disabled={self}
                    onConfirm={() => deleteMut.mutate(u.id)}
                  >
                    <Tooltip title={why}>
                      <Button type="text" size="small" danger disabled={self}>Delete</Button>
                    </Tooltip>
                  </Popconfirm>
                </Space>
              );
            },
          },
        ]}
      />
    </Card>
  );
}

/** The limits every workspace gets unless it has its own. */
function LimitsTab() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const q = useQuery({ queryKey: ["admin", "limits"], queryFn: api.adminLimits });
  const [values, setValues] = useState<LimitValues>({});
  useEffect(() => {
    if (q.data) setValues(q.data.limits);
  }, [q.data]);
  const save = useMutation({
    mutationFn: () => api.setAdminLimits(values),
    onSuccess: (d) => {
      qc.setQueryData(["admin", "limits"], d);
      message.success("Saved. Every workspace without its own value uses these now.");
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  if (q.isError) return <Alert type="error" showIcon message={errorMessage(q.error)} />;
  if (!q.data) return <Skeleton active />;
  const { defaults, labels } = q.data;
  return (
    <Card>
      <Typography.Paragraph type="secondary">
        Every workspace gets these, unless you give it its own values (Workspaces, then Details). Changes are recorded
        in the platform's audit entries.
      </Typography.Paragraph>
      <Form layout="horizontal" labelCol={{ flex: "320px" }} labelAlign="left" style={{ maxWidth: 640 }}>
        {Object.keys(defaults).map((name) => (
          <Form.Item key={name} label={labels[name]} style={{ marginBottom: 12 }}
            extra={values[name] !== defaults[name] ? `Default: ${defaults[name]}` : undefined}>
            <InputNumber min={1} max={100000} precision={0} value={values[name]} style={{ width: 140 }}
              onChange={(v) => setValues((old) => ({ ...old, [name]: v ?? defaults[name] }))} />
          </Form.Item>
        ))}
        <Button type="primary" onClick={() => save.mutate()} loading={save.isPending}>Save</Button>
      </Form>
    </Card>
  );
}

// per-user, so a workspace can't override it
const NOT_PER_WORKSPACE = new Set(["workspaces_per_user"]);
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

function WorkspaceDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["admin", "workspace", id], queryFn: () => api.adminWorkspace(id!), enabled: !!id });
  const ws = q.data;
  return (
    <Drawer title={ws?.name ?? "Workspace"} open={!!id} onClose={onClose} width={560}>
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

function WorkspacesTab() {
  const q = useQuery({ queryKey: ["admin", "workspaces"], queryFn: api.adminWorkspaces });
  const [open, setOpen] = useState<string | null>(null);
  if (q.isError) return <Alert type="error" showIcon message={errorMessage(q.error)} />;
  return (
    <Card styles={{ body: { padding: 0 } }}>
      <Table<AdminWorkspace>
        rowKey="id"
        size="small"
        loading={q.isLoading}
        dataSource={q.data}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        columns={[
          {
            title: "Workspace",
            render: (_, w) => (
              <Space>
                <span>{w.name}</span>
                {w.personal && <Tag>personal</Tag>}
              </Space>
            ),
          },
          { title: "Members", dataIndex: "members", width: 100 },
          { title: "Repositories", dataIndex: "repos", width: 120 },
          { title: "Created", width: 130, render: (_, w) => fmtDate(w.created_at) },
          {
            title: "",
            width: 100,
            render: (_, w) => <Button type="link" size="small" onClick={() => setOpen(w.id)}>Details</Button>,
          },
        ]}
      />
      <WorkspaceDrawer id={open} onClose={() => setOpen(null)} />
    </Card>
  );
}

export function AdminPage() {
  const { isPlatformAdmin, isPublic } = useSession();
  if (!isPlatformAdmin || !isPublic) return <Result status="404" title="Page not found" />;
  return (
    <div>
      <Typography.Title level={3} style={{ marginTop: 0 }}>Admin</Typography.Title>
      <Typography.Paragraph type="secondary">
        Everyone on this service. You see accounts and metadata, not scan results or keys.
      </Typography.Paragraph>
      <Tabs
        items={[
          { key: "users", label: "Users", children: <UsersTab /> },
          { key: "workspaces", label: "Workspaces", children: <WorkspacesTab /> },
          { key: "limits", label: "Limits", children: <LimitsTab /> },
        ]}
      />
    </div>
  );
}
