import { CopyOutlined, UserOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App, Avatar, Button, Card, Input, Modal, Popconfirm, Result, Segmented, Select, Space, Table, Tag, Tooltip,
  Typography,
} from "antd";
import { useState } from "react";
import { api, errorMessage } from "../api";
import { CreateWorkspaceModal } from "../components/CreateWorkspaceModal";
import { fmtDate, fmtRelative } from "../format";
import { ROLES, ROLE_HELP, ROLE_LABEL } from "../lib/permissions";
import { useSession } from "../session";
import type { Invite, Member, Role } from "../types";

function RoleTag({ role }: { role: Role }) {
  return (
    <Tooltip title={ROLE_HELP[role]}>
      <Tag color={role === "owner" ? "gold" : role === "admin" ? "blue" : undefined}>{ROLE_LABEL[role]}</Tag>
    </Tooltip>
  );
}

function MemberCell({ m, you }: { m: Member; you: boolean }) {
  return (
    <Space>
      <Avatar size="small" src={m.avatar_url ?? undefined} icon={<UserOutlined />} />
      <span>
        <Typography.Text strong>{m.login}</Typography.Text>
        {you && <Tag style={{ marginLeft: 8 }}>you</Tag>}
        {m.name && <Typography.Text type="secondary"> · {m.name}</Typography.Text>}
      </span>
    </Space>
  );
}

function MembersCard({ wsId }: { wsId: string }) {
  const { me, can, why } = useSession();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const q = useQuery({ queryKey: ["members", wsId], queryFn: () => api.members(wsId) });
  const members = q.data ?? [];
  const owners = members.filter((m) => m.role === "owner").length;
  const iAmOwner = me.role === "owner";

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["members", wsId] });
    qc.invalidateQueries({ queryKey: ["me"] });
  };
  const roleMut = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: Role }) => api.setRole(wsId, userId, role),
    onSuccess: () => {
      message.success("Role changed");
      refresh();
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  const removeMut = useMutation({
    mutationFn: (userId: string) => api.removeMember(wsId, userId),
    onSuccess: () => {
      message.success("Removed");
      refresh();
    },
    onError: (e) => message.error(errorMessage(e)),
  });

  // Only Owners touch Owners, and the last Owner stays an Owner.
  const lockedReason = (m: Member): string | undefined => {
    if (!can("members")) return why("members");
    if (m.role === "owner" && !iAmOwner) return "Only an Owner can change an Owner";
    if (m.role === "owner" && owners === 1) return "The last Owner: make someone else Owner first";
    return undefined;
  };

  const changeRole = (m: Member, role: Role) => {
    if (role !== "owner") return roleMut.mutate({ userId: m.user_id, role });
    modal.confirm({
      title: `Make ${m.login} an Owner?`,
      content: "Owners can manage other Owners, and rename, transfer or delete the workspace.",
      okText: "Make Owner",
      onOk: () => roleMut.mutateAsync({ userId: m.user_id, role }),
    });
  };

  return (
    <Card title={`Members (${members.length})`} style={{ marginBottom: 16 }} styles={{ body: { padding: 0 } }}>
      <Table<Member>
        rowKey="user_id"
        loading={q.isLoading}
        dataSource={members}
        pagination={false}
        scroll={{ x: 600 }}
        columns={[
          { title: "Member", render: (_, m) => <MemberCell m={m} you={m.user_id === me.user.id} /> },
          {
            title: "Role",
            width: 190,
            render: (_, m) => {
              const locked = lockedReason(m);
              if (locked) return <Tooltip title={locked}><span><RoleTag role={m.role} /></span></Tooltip>;
              return (
                <Select<Role>
                  size="small"
                  style={{ width: 140 }}
                  value={m.role}
                  aria-label={`Role of ${m.login}`}
                  onChange={(r) => changeRole(m, r)}
                  options={ROLES.filter((r) => r !== "owner" || iAmOwner).map((r) => ({ value: r, label: ROLE_LABEL[r] }))}
                />
              );
            },
          },
          { title: "Joined", width: 140, render: (_, m) => fmtDate(m.joined_at) },
          {
            title: "",
            width: 110,
            render: (_, m) => {
              if (m.user_id === me.user.id) return null; // leaving is below
              const locked = lockedReason(m);
              return (
                <Popconfirm
                  title={`Remove ${m.login}?`}
                  description="They lose access to this workspace at once."
                  okText="Remove"
                  okButtonProps={{ danger: true }}
                  disabled={!!locked}
                  onConfirm={() => removeMut.mutate(m.user_id)}
                >
                  <Tooltip title={locked}>
                    <Button type="text" danger size="small" disabled={!!locked}>Remove</Button>
                  </Tooltip>
                </Popconfirm>
              );
            },
          },
        ]}
      />
    </Card>
  );
}

function InviteCard({ wsId }: { wsId: string }) {
  const { me, can, why } = useSession();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [kind, setKind] = useState<"login" | "link">("login");
  const [login, setLogin] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [linkUrl, setLinkUrl] = useState<string | null>(null);
  const allowed = can("members");

  const mut = useMutation({
    mutationFn: () => api.createInvite(wsId, role, kind === "login" ? login.trim().replace(/^@/, "") : undefined),
    onSuccess: (inv) => {
      qc.invalidateQueries({ queryKey: ["invites", wsId] });
      if (inv.url) setLinkUrl(inv.url);
      else message.success(`Invited ${inv.github_login}. They'll see it when they sign in.`);
      setLogin("");
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  const ready = kind === "link" || /^[A-Za-z0-9-]{1,39}$/.test(login.trim().replace(/^@/, ""));

  return (
    <Card title="Invite people" style={{ marginBottom: 16 }}>
      {!allowed && <Alert type="info" showIcon style={{ marginBottom: 12 }} message={why("members")} />}
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Segmented<"login" | "link">
          disabled={!allowed}
          value={kind}
          onChange={setKind}
          options={[
            { value: "login", label: "By GitHub username" },
            { value: "link", label: "By one-time link" },
          ]}
        />
        <Space wrap>
          {kind === "login" && (
            <Input
              disabled={!allowed}
              style={{ width: 240, maxWidth: "calc(100vw - 72px)" }}
              prefix="@"
              placeholder="github-username"
              value={login}
              onChange={(e) => setLogin(e.target.value)}
              onPressEnter={() => ready && mut.mutate()}
              aria-label="GitHub username"
            />
          )}
          <Select<Role>
            disabled={!allowed}
            style={{ width: 140 }}
            value={role}
            onChange={setRole}
            aria-label="Role"
            options={ROLES.filter((r) => r !== "owner" || me.role === "owner").map((r) => ({ value: r, label: ROLE_LABEL[r] }))}
          />
          <Button type="primary" disabled={!allowed || !ready} loading={mut.isPending} onClick={() => mut.mutate()}>
            {kind === "login" ? "Invite" : "Create link"}
          </Button>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {kind === "login"
            ? "The invite waits until that person signs in with GitHub. "
            : "Anyone with the link can use it, once. "}
          Invites expire after 7 days. {ROLE_HELP[role]}
        </Typography.Text>
      </Space>

      <Modal
        title="Share this invite link"
        open={!!linkUrl}
        onCancel={() => setLinkUrl(null)}
        footer={<Button type="primary" onClick={() => setLinkUrl(null)}>Done</Button>}
      >
        <Typography.Paragraph>
          Send it to the person you're inviting. It works once, for 7 days, and <strong>this is the only time it's
          shown</strong>.
        </Typography.Paragraph>
        <Space.Compact style={{ width: "100%" }}>
          <Input readOnly value={linkUrl ?? ""} onFocus={(e) => e.target.select()} />
          <Button
            icon={<CopyOutlined />}
            onClick={() => navigator.clipboard?.writeText(linkUrl ?? "").then(() => message.success("Copied"))}
          >
            Copy
          </Button>
        </Space.Compact>
      </Modal>
    </Card>
  );
}

function PendingInvitesCard({ wsId }: { wsId: string }) {
  const { can } = useSession();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const q = useQuery({ queryKey: ["invites", wsId], queryFn: () => api.invites(wsId), enabled: can("members") });
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeInvite(wsId, id),
    onSuccess: () => {
      message.success("Invite revoked");
      qc.invalidateQueries({ queryKey: ["invites", wsId] });
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  if (!can("members") || !q.data?.length) return null;
  return (
    <Card title={`Pending invites (${q.data.length})`} style={{ marginBottom: 16 }} styles={{ body: { padding: 0 } }}>
      <Table<Invite>
        rowKey="id"
        dataSource={q.data}
        pagination={false}
        scroll={{ x: 480 }}
        columns={[
          { title: "For", render: (_, i) => (i.github_login ? `@${i.github_login}` : <Tag>one-time link</Tag>) },
          { title: "Role", width: 120, render: (_, i) => <RoleTag role={i.role} /> },
          { title: "Expires", width: 140, render: (_, i) => fmtRelative(i.expires_at) },
          {
            title: "",
            width: 100,
            render: (_, i) => (
              <Popconfirm title="Revoke this invite?" okText="Revoke" onConfirm={() => revoke.mutate(i.id)}>
                <Button type="text" size="small" danger>Revoke</Button>
              </Popconfirm>
            ),
          },
        ]}
      />
    </Card>
  );
}

/** Rename, transfer, delete (Owners), and leave (everyone). */
function WorkspaceCard({ wsId }: { wsId: string }) {
  const { me, workspace, why, can, switchWorkspace } = useSession();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [name, setName] = useState(workspace?.name ?? "");
  const [heir, setHeir] = useState<string>();
  const [confirmName, setConfirmName] = useState("");
  const [deleting, setDeleting] = useState(false);
  const members = useQuery({ queryKey: ["members", wsId], queryFn: () => api.members(wsId) }).data ?? [];
  const owner = can("owner");
  const personalId = me.workspaces.find((w) => w.personal)?.id;
  const lastOwner = me.role === "owner" && members.filter((m) => m.role === "owner").length === 1;

  const renameMut = useMutation({
    mutationFn: () => api.renameWorkspace(wsId, name.trim()),
    onSuccess: () => {
      message.success("Renamed");
      qc.invalidateQueries({ queryKey: ["me"] });
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  const transferMut = useMutation({
    mutationFn: (userId: string) => api.transferWorkspace(wsId, userId),
    onSuccess: () => {
      message.success("Ownership transferred. You're now an Admin.");
      setHeir(undefined);
      qc.invalidateQueries({ queryKey: ["me"] });
      qc.invalidateQueries({ queryKey: ["members", wsId] });
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  const leaveMut = useMutation({
    mutationFn: () => api.leaveWorkspace(wsId),
    onSuccess: () => {
      message.success(`You left ${workspace?.name}`);
      if (personalId) switchWorkspace(personalId);
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  const deleteMut = useMutation({
    mutationFn: () => api.deleteWorkspace(wsId),
    onSuccess: () => {
      message.success(`Deleted ${workspace?.name}`);
      setDeleting(false);
      if (personalId) switchWorkspace(personalId);
    },
    onError: (e) => message.error(errorMessage(e)),
  });

  const heirs = members.filter((m) => m.user_id !== me.user.id && m.role !== "owner");

  return (
    <Card title="Workspace">
      <Space direction="vertical" size={20} style={{ width: "100%" }}>
        <div>
          <Typography.Text strong>Name</Typography.Text>
          <Space.Compact style={{ display: "flex", maxWidth: 420, marginTop: 6 }}>
            <Tooltip title={why("owner")}>
              <Input value={name} maxLength={80} disabled={!owner} onChange={(e) => setName(e.target.value)} />
            </Tooltip>
            <Button
              disabled={!owner || !name.trim() || name.trim() === workspace?.name}
              loading={renameMut.isPending}
              onClick={() => renameMut.mutate()}
            >
              Rename
            </Button>
          </Space.Compact>
        </div>

        <div>
          <Typography.Text strong>Transfer ownership</Typography.Text>
          <Typography.Paragraph type="secondary" style={{ margin: "2px 0 6px" }}>
            Make another member the Owner. You stay on as an Admin.
          </Typography.Paragraph>
          <Space wrap>
            <Tooltip title={why("owner") ?? (heirs.length ? undefined : "Invite someone first")}>
              <Select
                style={{ width: 240 }}
                placeholder="Choose a member"
                disabled={!owner || !heirs.length}
                value={heir}
                onChange={setHeir}
                options={heirs.map((m) => ({ value: m.user_id, label: `${m.login} (${ROLE_LABEL[m.role]})` }))}
              />
            </Tooltip>
            <Button
              disabled={!owner || !heir}
              loading={transferMut.isPending}
              onClick={() => {
                const m = members.find((x) => x.user_id === heir);
                modal.confirm({
                  title: `Make ${m?.login} the Owner?`,
                  content: "You'll become an Admin. Only an Owner can make you Owner again.",
                  okText: "Transfer",
                  onOk: () => transferMut.mutateAsync(heir!),
                });
              }}
            >
              Transfer
            </Button>
          </Space>
        </div>

        <Space wrap>
          <Popconfirm
            title={`Leave ${workspace?.name}?`}
            description="You lose access until someone invites you again."
            okText="Leave"
            okButtonProps={{ danger: true }}
            disabled={lastOwner}
            onConfirm={() => leaveMut.mutate()}
          >
            <Tooltip title={lastOwner ? "You're the last Owner: make someone else Owner first" : undefined}>
              <Button danger disabled={lastOwner} loading={leaveMut.isPending}>Leave workspace</Button>
            </Tooltip>
          </Popconfirm>
          <Tooltip title={why("owner")}>
            <Button danger type="primary" disabled={!owner} onClick={() => setDeleting(true)}>
              Delete workspace
            </Button>
          </Tooltip>
        </Space>
      </Space>

      <Modal
        title={`Delete ${workspace?.name}?`}
        open={deleting}
        onCancel={() => setDeleting(false)}
        okText="Delete for everyone"
        okButtonProps={{ danger: true, disabled: confirmName !== workspace?.name, loading: deleteMut.isPending }}
        onOk={() => deleteMut.mutate()}
        destroyOnHidden
      >
        <Typography.Paragraph>
          This deletes its repositories, their scan history and uploads, its settings and keys, its invites and its
          audit log, for every member. It can't be undone.
        </Typography.Paragraph>
        <Typography.Paragraph>Type <strong>{workspace?.name}</strong> to confirm.</Typography.Paragraph>
        <Input value={confirmName} onChange={(e) => setConfirmName(e.target.value)} aria-label="Workspace name" />
      </Modal>
    </Card>
  );
}

export function MembersPage() {
  const { me, isPublic, workspace } = useSession();
  const [creating, setCreating] = useState(false);

  if (!isPublic) {
    return (
      <Result
        status="info"
        title="The local mode has a single user"
        subTitle="Team workspaces, members and invites come with GitHub sign-in (AUTH_MODE=github)."
      />
    );
  }
  if (workspace?.personal) {
    return (
      <>
        <Result
          icon={<UserOutlined />}
          title="This is your personal workspace"
          subTitle="It's just yours. To work with other people, create a team workspace and invite them to it."
          extra={<Button type="primary" onClick={() => setCreating(true)}>New team workspace</Button>}
        />
        <CreateWorkspaceModal open={creating} onClose={() => setCreating(false)} />
      </>
    );
  }
  return (
    <div style={{ maxWidth: 900 }}>
      <Typography.Title level={3} style={{ marginTop: 0 }}>Members</Typography.Title>
      <Typography.Paragraph type="secondary">
        Who can use <Typography.Text strong>{workspace?.name}</Typography.Text>, and what they can do. Your role here
        is {ROLE_LABEL[me.role]}.
      </Typography.Paragraph>
      <MembersCard wsId={me.workspace_id} />
      <InviteCard wsId={me.workspace_id} />
      <PendingInvitesCard wsId={me.workspace_id} />
      <WorkspaceCard wsId={me.workspace_id} />
    </div>
  );
}
