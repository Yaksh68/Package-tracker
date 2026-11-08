import { useMutation } from "@tanstack/react-query";
import { App, Input, Modal, Typography } from "antd";
import { useState } from "react";
import { api, errorMessage } from "../api";
import { useSession } from "../session";

/** Create a team workspace and switch to it, landing on Members to invite people. */
export function CreateWorkspaceModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { switchWorkspace } = useSession();
  const { message } = App.useApp();
  const [name, setName] = useState("");
  const mut = useMutation({
    mutationFn: () => api.createWorkspace(name.trim()),
    onSuccess: (ws) => {
      message.success(`Created ${ws.name}`);
      setName("");
      onClose();
      switchWorkspace(ws.id, "/members");
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  return (
    <Modal
      title="New team workspace"
      open={open}
      onCancel={onClose}
      onOk={() => mut.mutate()}
      okText="Create"
      okButtonProps={{ disabled: !name.trim(), loading: mut.isPending }}
      destroyOnHidden
    >
      <Typography.Paragraph type="secondary">
        A team workspace has its own repositories, settings and keys, and you can invite people to it. You'll be its
        Owner.
      </Typography.Paragraph>
      <Input
        autoFocus
        maxLength={80}
        placeholder="Workspace name, e.g. Platform team"
        value={name}
        onChange={(e) => setName(e.target.value)}
        onPressEnter={() => name.trim() && mut.mutate()}
      />
    </Modal>
  );
}
