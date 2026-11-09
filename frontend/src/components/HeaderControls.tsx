// The header's account controls (the public mode): which workspace you're in,
// invites waiting for you, and your account menu.
import { DownOutlined, MailOutlined, PlusOutlined, UserOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App, Avatar, Badge, Button, Divider, Dropdown, Input, List, Modal, Popover, Select, Space, Tag, Typography,
} from "antd";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorMessage, setActiveWorkspace } from "../api";
import { ROLE_LABEL } from "../lib/permissions";
import { useSession } from "../session";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";

/** `block`: full width, for the phone menu drawer. */
export function WorkspaceSwitcher({ block = false }: { block?: boolean }) {
  const { me, switchWorkspace } = useSession();
  const [creating, setCreating] = useState(false);
  return (
    <>
      <Select
        aria-label="Workspace"
        value={me.workspace_id}
        onChange={(id) => switchWorkspace(id)}
        style={block ? { width: "100%" } : { minWidth: 180, maxWidth: 260 }}
        popupMatchSelectWidth={false}
        options={me.workspaces.map((w) => ({
          value: w.id,
          label: (
            <Space size={6}>
              <span>{w.name}</span>
              {w.role && <Typography.Text type="secondary" style={{ fontSize: 12 }}>{ROLE_LABEL[w.role]}</Typography.Text>}
            </Space>
          ),
        }))}
        popupRender={(menu) => (
          <>
            {menu}
            <Divider style={{ margin: "4px 0" }} />
            <Button type="text" block icon={<PlusOutlined />} style={{ textAlign: "left" }} onClick={() => setCreating(true)}>
              New team workspace
            </Button>
          </>
        )}
      />
      <CreateWorkspaceModal open={creating} onClose={() => setCreating(false)} />
    </>
  );
}

/** Invites addressed to your GitHub username. Link invites arrive by link. */
export function InvitesBell() {
  const { switchWorkspace } = useSession();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const q = useQuery({ queryKey: ["my-invites"], queryFn: api.myInvites, refetchInterval: 5 * 60_000 });
  const accept = useMutation({
    mutationFn: (id: string) => api.acceptInvite(id),
    onSuccess: (inv) => {
      message.success(`You joined ${inv.workspace_name ?? "the workspace"}`);
      switchWorkspace(inv.workspace_id);
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  const decline = useMutation({
    mutationFn: (id: string) => api.declineInvite(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-invites"] }),
    onError: (e) => message.error(errorMessage(e)),
  });
  const invites = q.data ?? [];
  if (!invites.length) return null;
  return (
    <Popover
      trigger="click"
      placement="bottomRight"
      title="Invites for you"
      content={
        <List
          style={{ width: 320 }}
          dataSource={invites}
          renderItem={(inv) => (
            <List.Item
              actions={[
                <Button key="a" type="primary" size="small" loading={accept.isPending && accept.variables === inv.id}
                  onClick={() => accept.mutate(inv.id)}>Accept</Button>,
                <Button key="d" size="small" loading={decline.isPending && decline.variables === inv.id}
                  onClick={() => decline.mutate(inv.id)}>Decline</Button>,
              ]}
            >
              <List.Item.Meta title={inv.workspace_name ?? "A workspace"} description={`as ${ROLE_LABEL[inv.role]}`} />
            </List.Item>
          )}
        />
      }
    >
      <Badge count={invites.length} size="small">
        <Button type="text" icon={<MailOutlined />} aria-label={`${invites.length} invites for you`} />
      </Badge>
    </Popover>
  );
}

function DeleteAccountModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { me } = useSession();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [typed, setTyped] = useState("");
  const mut = useMutation({
    mutationFn: api.deleteAccount,
    onSuccess: () => {
      setActiveWorkspace(null);
      qc.clear();
      navigate("/login");
    },
  });
  return (
    <Modal
      title="Delete your account?"
      open={open}
      onCancel={onClose}
      okText="Delete my account"
      okButtonProps={{ danger: true, disabled: typed !== me.user.login, loading: mut.isPending }}
      onOk={() => mut.mutate()}
      destroyOnHidden
    >
      <Typography.Paragraph>
        This deletes your account, your personal workspace with its repositories, history and keys, and any team
        workspace where you're the only member. You'll leave every other workspace. It can't be undone.
      </Typography.Paragraph>
      {mut.isError && (
        <Typography.Paragraph type="danger">{errorMessage(mut.error)}</Typography.Paragraph>
      )}
      <Typography.Paragraph>Type your GitHub username, <strong>{me.user.login}</strong>, to confirm.</Typography.Paragraph>
      <Input value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="GitHub username" autoComplete="off" />
    </Modal>
  );
}

export function AccountMenu() {
  const { me, isPlatformAdmin, signOut } = useSession();
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState(false);
  return (
    <>
      <Dropdown
        trigger={["click"]}
        menu={{
          items: [
            {
              key: "who",
              disabled: true,
              label: (
                <Space direction="vertical" size={0}>
                  <Typography.Text strong>{me.user.name || me.user.login}</Typography.Text>
                  <Typography.Text type="secondary">@{me.user.login}</Typography.Text>
                  {isPlatformAdmin && <Tag color="purple" style={{ marginTop: 4 }}>platform admin</Tag>}
                </Space>
              ),
            },
            { type: "divider" },
            ...(isPlatformAdmin ? [{ key: "admin", label: "Admin" }] : []),
            { key: "privacy", label: "Privacy policy" },
            { key: "terms", label: "Terms" },
            { type: "divider" },
            { key: "delete", label: "Delete account…", danger: true },
            { key: "signout", label: "Sign out" },
          ],
          onClick: ({ key }) => {
            if (key === "admin") navigate("/admin");
            else if (key === "privacy" || key === "terms") navigate(`/${key}`);
            else if (key === "delete") setDeleting(true);
            else if (key === "signout") void signOut();
          },
        }}
      >
        <Button type="text" aria-label="Account" style={{ paddingInline: 6 }}>
          <Space size={6}>
            <Avatar size={26} src={me.user.avatar_url ?? undefined} icon={<UserOutlined />} />
            <DownOutlined style={{ fontSize: 10 }} />
          </Space>
        </Button>
      </Dropdown>
      <DeleteAccountModal open={deleting} onClose={() => setDeleting(false)} />
    </>
  );
}
