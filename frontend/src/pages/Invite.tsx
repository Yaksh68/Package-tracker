import { TeamOutlined } from "@ant-design/icons";
import { useMutation } from "@tanstack/react-query";
import { App, Button, Result, Space } from "antd";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError, api, errorMessage } from "../api";
import { useSession } from "../session";

/** /invite/:token: a one-time invite link. Signing in comes first (the session
 *  gate sends you to sign-in and back here). */
export function InvitePage() {
  const { token = "" } = useParams();
  const { switchWorkspace, isPublic } = useSession();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const accept = useMutation({
    mutationFn: () => api.acceptInviteLink(token),
    onSuccess: (inv) => {
      message.success("You joined the workspace");
      switchWorkspace(inv.workspace_id);
    },
  });

  if (!isPublic) return <Result status="404" title="Invites need GitHub sign-in" />;
  if (accept.isError) {
    const e = accept.error;
    const gone = e instanceof ApiError && e.status === 410;
    return (
      <Result
        status={gone ? "warning" : "error"}
        title={gone ? "This invite has expired or was already used" : "This invite link didn't work"}
        subTitle={gone ? "Ask whoever invited you for a new link." : errorMessage(e)}
        extra={<Button onClick={() => navigate("/")}>Go to your repositories</Button>}
      />
    );
  }
  return (
    <Result
      icon={<TeamOutlined />}
      title="You've been invited to a workspace"
      subTitle="Accepting adds you with the role chosen in the invite. Invite links work once, for 7 days."
      extra={
        <Space>
          <Button type="primary" loading={accept.isPending} onClick={() => accept.mutate()}>Accept</Button>
          <Button onClick={() => navigate("/")}>Not now</Button>
        </Space>
      }
    />
  );
}
