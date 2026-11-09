import { GithubOutlined, LockOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Collapse, Divider, Form, Input, Result, Space, Spin, Typography, theme } from "antd";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, errorMessage } from "../api";
import { BrandMark } from "../components/Brand";
import { safeNext } from "../session";

/** What /api/auth/github/callback's ?error= codes mean. */
export const SIGN_IN_ERRORS: Record<string, string> = {
  cancelled: "Sign-in was cancelled on GitHub.",
  expired: "That sign-in attempt expired or wasn't valid. Please try again.",
  github: "GitHub didn't complete the sign-in. Please try again.",
  disabled: "This account is disabled. Contact whoever runs this service if you think that's a mistake.",
};

interface TrialForm {
  url: string;
  branch?: string;
  token?: string;
}

function repoPath(repoId: string, branch: string) {
  return `/repos/${encodeURIComponent(repoId)}?branch=${encodeURIComponent(branch)}`;
}

/** Where this browser's earlier free scan is, when the server says it has one. */
function earlierScan(e: unknown): { repo_id: string; branch: string } | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const body = e.body as { repo_id?: string; branch?: string } | undefined;
  return body?.repo_id && body.branch ? { repo_id: body.repo_id, branch: body.branch } : null;
}

/** Scan one repository without an account: the results open right away, and
 *  signing in keeps them. */
function FreeScan() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [form] = Form.useForm<TrialForm>();
  const start = useMutation({
    mutationFn: (v: TrialForm) => api.startTrial({
      url: v.url.trim(),
      branch: v.branch?.trim() || undefined,
      token: v.token?.trim() || undefined,
    }),
    onSuccess: async (r) => {
      // the guest session's cookie is set now: who we are has changed
      qc.setQueryData(["me"], await api.me());
      navigate(repoPath(r.repo_id, r.branch));
    },
    onError: async (e) => {
      const earlier = earlierScan(e);
      if (earlier) {
        qc.setQueryData(["me"], await api.me().catch(() => undefined));
        navigate(repoPath(earlier.repo_id, earlier.branch));
      }
    },
  });
  return (
    <Form form={form} layout="vertical" requiredMark={false} onFinish={(v) => start.mutate(v)}>
      <Typography.Title level={5} style={{ marginTop: 0 }}>Scan a repository, no account needed</Typography.Title>
      <Form.Item
        name="url"
        label="Repository address"
        rules={[{ required: true, whitespace: true, message: "Paste a repository's address" }]}
        style={{ marginBottom: 12 }}
      >
        <Input placeholder="https://github.com/owner/repo" inputMode="url" autoComplete="off" />
      </Form.Item>
      <Form.Item name="branch" label="Branch" extra="Leave empty for the repository's default branch."
        style={{ marginBottom: 12 }}>
        <Input placeholder="main" autoComplete="off" />
      </Form.Item>
      <Collapse
        ghost
        size="small"
        style={{ marginBottom: 12, marginInline: -16 }}
        items={[{
          key: "token",
          label: <span><LockOutlined /> A private repository? Add a token</span>,
          children: (
            <Form.Item
              name="token"
              style={{ marginBottom: 0 }}
              extra="A read-only token that can read this repository. It's used for this one scan and never stored."
            >
              <Input.Password placeholder="Token" autoComplete="off" />
            </Form.Item>
          ),
        }]}
      />
      {start.isError && !earlierScan(start.error) && (
        <Alert type="warning" showIcon message={errorMessage(start.error)} style={{ marginBottom: 12 }} />
      )}
      <Button type="primary" size="large" block htmlType="submit" loading={start.isPending}>
        {start.isPending ? "Checking the repository…" : "Scan for free"}
      </Button>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13, margin: "8px 0 0" }}>
        One free scan, kept for a day. It lists every dependency with its end-of-life date. Sign in to keep it,
        scan more repositories and schedule scans.
      </Typography.Paragraph>
    </Form>
  );
}

export function LoginPage() {
  const { token } = theme.useToken();
  const [params] = useSearchParams();
  const error = params.get("error");
  const next = safeNext(params.get("next"));
  const info = useQuery({ queryKey: ["info"], queryFn: api.info, staleTime: Infinity });
  // already signed in (or the local mode, which has no sign-in): carry on.
  // isSuccess, not data: after a session ends, the query still holds the old
  // user next to the 401, and trusting it would bounce straight back.
  // A guest (a free scan) stays: they can sign in here, and starting their
  // free scan must not race this redirect.
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false });

  if ((me.isSuccess && !me.data.guest) || info.data?.mode === "local") return <Navigate to={next} replace />;
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
    <Card style={{ width: "100%", maxWidth: 440 }} styles={{ body: { padding: 28 } }}>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <Space size={12} align="center">
          <BrandMark size={32} />
          <Typography.Title level={3} style={{ margin: 0 }}>Packages Tracker</Typography.Title>
        </Space>
        <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
          Find the dependencies in your repositories that have reached, or will soon reach, end of life.
        </Typography.Paragraph>
        {error && <Alert type="error" showIcon message={SIGN_IN_ERRORS[error] ?? "Sign-in didn't work. Please try again."} />}
        <FreeScan />
        <Divider plain style={{ margin: "4px 0" }}>or</Divider>
        <Button size="large" block icon={<GithubOutlined />} href={api.loginUrl(next)}>
          Sign in with GitHub
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          Signing in reads only your public GitHub profile. By scanning or signing in you agree to the{" "}
          <Link to="/terms">terms</Link> and the <Link to="/privacy">privacy policy</Link>.
        </Typography.Text>
      </Space>
    </Card>,
  );
}
