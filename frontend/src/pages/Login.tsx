import { GithubOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Result, Space, Spin, Typography, theme } from "antd";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { BrandMark } from "../components/Brand";
import { safeNext } from "../session";

/** What /api/auth/github/callback's ?error= codes mean. */
export const SIGN_IN_ERRORS: Record<string, string> = {
  cancelled: "Sign-in was cancelled on GitHub.",
  expired: "That sign-in attempt expired or wasn't valid. Please try again.",
  github: "GitHub didn't complete the sign-in. Please try again.",
  disabled: "This account is disabled. Contact whoever runs this service if you think that's a mistake.",
};

export function LoginPage() {
  const { token } = theme.useToken();
  const [params] = useSearchParams();
  const error = params.get("error");
  const next = safeNext(params.get("next"));
  const info = useQuery({ queryKey: ["info"], queryFn: api.info, staleTime: Infinity });
  // already signed in (or the local mode, which has no sign-in): carry on.
  // isSuccess, not data: after a session ends, the query still holds the old
  // user next to the 401, and trusting it would bounce straight back.
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false });

  if (me.isSuccess || info.data?.mode === "local") return <Navigate to={next} replace />;
  if (me.isPending && info.isPending) {
    return <div style={{ display: "grid", placeItems: "center", minHeight: "100vh" }}><Spin size="large" /></div>;
  }

  const page = (children: React.ReactNode) => (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 16, background: token.colorBgLayout }}>
      {children}
    </div>
  );

  if (error === "full") {
    return page(
      <Result
        status="info"
        title="Sign-ups are full right now"
        subTitle="This service has reached its limit of users, so it can't take new accounts at the moment. If you already have an account, you can still sign in."
        extra={<Button href={api.loginUrl(next)} icon={<GithubOutlined />}>Sign in with an existing account</Button>}
      />,
    );
  }

  return page(
    <Card style={{ width: "100%", maxWidth: 400 }} styles={{ body: { padding: 32 } }}>
      <Space direction="vertical" size={20} style={{ width: "100%" }}>
        <Space size={12} align="center">
          <BrandMark size={32} />
          <Typography.Title level={3} style={{ margin: 0 }}>Packages Tracker</Typography.Title>
        </Space>
        <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
          Find the dependencies in your repositories that have reached, or will soon reach, end of life.
        </Typography.Paragraph>
        {error && <Alert type="error" showIcon message={SIGN_IN_ERRORS[error] ?? "Sign-in didn't work. Please try again."} />}
        <Button type="primary" size="large" block icon={<GithubOutlined />} href={api.loginUrl(next)}>
          Sign in with GitHub
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          Signing in reads only your public GitHub profile. By signing in you agree to the{" "}
          <Link to="/terms">terms</Link> and the <Link to="/privacy">privacy policy</Link>.
        </Typography.Text>
      </Space>
    </Card>,
  );
}
