// The platform admin's area: analytics for the whole service, every user and
// workspace on it, and the limits. It shows totals, accounts and metadata
// only, never scan results or keys. Each tab has its own address
// (/admin/analytics, /admin/users, …).
import { UserOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App, Avatar, Button, Card, Form, InputNumber, Popconfirm, Result, Skeleton, Space, Table, Tabs, Tag,
  Tooltip, Typography,
} from "antd";
import { lazy, Suspense, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api";
import { WorkspaceDrawer } from "../components/WorkspaceDrawer";
import { fmtDate, fmtDateTime, fmtRelative } from "../format";
import { useIsMobile } from "../lib/responsive";
import { useSession } from "../session";
import type { AdminUser, AdminWorkspace, LimitValues } from "../types";

// The charts library loads only for the platform admin, on this tab.
const AnalyticsPage = lazy(() => import("./Analytics"));

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
        scroll={{ x: 860 }}
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
  const mobile = useIsMobile();
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
      <Form
        layout={mobile ? "vertical" : "horizontal"}
        labelCol={mobile ? undefined : { flex: "320px" }}
        labelAlign="left"
        style={{ maxWidth: 640 }}
      >
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

function WorkspacesTab() {
  const q = useQuery({ queryKey: ["admin", "workspaces"], queryFn: api.adminWorkspaces });
  const [open, setOpen] = useState<string | null>(null);
  if (q.isError) return <Alert type="error" showIcon message={errorMessage(q.error)} />;
  return (
    <Card styles={{ body: { padding: 0 } }}>
      <Table<AdminWorkspace>
        rowKey="id"
        size="small"
        scroll={{ x: 620 }}
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

const TABS = ["analytics", "users", "workspaces", "limits"];

export function AdminPage() {
  const { isPlatformAdmin, isPublic } = useSession();
  const { tab = "analytics" } = useParams();
  const navigate = useNavigate();
  if (!isPlatformAdmin || !isPublic || !TABS.includes(tab)) return <Result status="404" title="Page not found" />;
  return (
    <div>
      <Typography.Title level={3} style={{ marginTop: 0 }}>Admin</Typography.Title>
      <Typography.Paragraph type="secondary">
        Everyone on this service. You see totals, accounts and metadata, not scan results or keys.
      </Typography.Paragraph>
      <Tabs
        activeKey={tab}
        onChange={(key) => navigate(`/admin/${key}`)}
        items={[
          {
            key: "analytics",
            label: "Analytics",
            children: (
              <Suspense fallback={<Skeleton active paragraph={{ rows: 8 }} />}>
                <AnalyticsPage />
              </Suspense>
            ),
          },
          { key: "users", label: "Users", children: <UsersTab /> },
          { key: "workspaces", label: "Workspaces", children: <WorkspacesTab /> },
          { key: "limits", label: "Limits", children: <LimitsTab /> },
        ]}
      />
    </div>
  );
}
