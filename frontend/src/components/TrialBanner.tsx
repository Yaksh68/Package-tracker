// Shown to a visitor looking at their free scan (no account): what it is,
// when it goes, and that signing in keeps it.
import { GithubOutlined } from "@ant-design/icons";
import { Alert, Button, Space } from "antd";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import { fmtDateTime, fmtRelative } from "../format";
import { useSession } from "../session";

/** Where signing in should return to: the page the guest is on. */
export function useSignInHere(): string {
  const location = useLocation();
  return api.loginUrl(location.pathname + location.search);
}

export function TrialBanner() {
  const { me, isGuest } = useSession();
  const signIn = useSignInHere();
  if (!isGuest) return null;
  const expires = me.trial?.expires_at;
  return (
    <Alert
      type="info"
      showIcon
      style={{ marginBottom: 16 }}
      message="A free scan, without an account"
      description={
        <Space direction="vertical" size={8}>
          <span>
            It's deleted <span title={fmtDateTime(expires)}>{fmtRelative(expires)}</span> unless you sign in. Sign in
            with GitHub to keep it in your own workspace: then you can scan it again, schedule scans, see its history
            and download the report.
          </span>
          <Button type="primary" icon={<GithubOutlined />} href={signIn}>Sign in to keep it</Button>
        </Space>
      }
    />
  );
}
