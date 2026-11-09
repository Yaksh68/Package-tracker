import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Result, Space, Table, Tag, Tooltip, Typography } from "antd";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api";
import { fmtDateTime, fmtRelative, repoName } from "../format";
import { useSession } from "../session";
import type { AuditEvent } from "../types";

const PAGE = 50;

const ACTIONS: Record<string, string> = {
  "repo.create": "Added a repository",
  "repo.update": "Changed a repository",
  "repo.delete": "Deleted a repository",
  "scan.manual": "Started a scan",
  "scan.scheduled": "Ran a scheduled scan",
  "zip.upload": "Uploaded a zip",
  "settings.update": "Changed settings",
  "secret.set": "Saved a key",
  "secret.remove": "Removed a key",
  "workspace.create": "Created the workspace",
  "workspace.rename": "Renamed the workspace",
  "workspace.transfer": "Transferred ownership",
  "member.role_change": "Changed a role",
  "member.remove": "Removed a member",
  "member.leave": "Left the workspace",
  "invite.create": "Invited someone",
  "invite.revoke": "Revoked an invite",
  "invite.accept": "Accepted an invite",
  "invite.decline": "Declined an invite",
  "admin.view_workspace": "Viewed this workspace (platform admin)",
  "admin.limits": "Changed this workspace's limits (platform admin)",
  "workspace.scans_paused": "Paused scheduled scans: nobody had used the workspace for 30 days",
  "workspace.scans_resumed": "Came back, which resumed scheduled scans",
};

const SECRET_LABEL: Record<string, string> = {
  ai_api_key: "AI API key",
  search_api_key: "search API key",
  github_token: "GitHub token",
  gitlab_token: "GitLab token",
};

export const describeAction = (action: string) => ACTIONS[action] ?? action;

function Actor({ e }: { e: AuditEvent }) {
  if (e.actor.kind === "scheduler") return <Tag>scheduler</Tag>;
  const who = e.actor.login ? e.actor.login : "deleted user";
  return e.actor.kind === "platform_admin" ? (
    <Space size={4}>
      <span>{who}</span>
      <Tag color="purple">platform admin</Tag>
    </Space>
  ) : (
    <span>{who}</span>
  );
}

function Target({ e, logins, repos }: { e: AuditEvent; logins: Map<string, string>; repos: Map<string, string> }) {
  if (!e.target_id) return null;
  if (e.target_type === "repo") {
    const name = repos.get(e.target_id);
    if (name) return <Link to={`/repos/${encodeURIComponent(e.target_id)}`}>{name}</Link>;
    // a deleted repo has no page left: its name as the entry recorded it
    return <span>{typeof e.details.name === "string" ? e.details.name : e.target_id}</span>;
  }
  if (e.target_type === "secret") return <span>{SECRET_LABEL[e.target_id] ?? e.target_id}</span>;
  if (e.target_type === "user") return <span>{logins.get(e.target_id) ?? "a former member"}</span>;
  if (e.target_type === "workspace" || e.target_type === "invite") return null;
  return <span>{e.target_id}</span>;
}

function Details({ details }: { details: Record<string, unknown> }) {
  const entries = Object.entries(details);
  if (!entries.length) return null;
  return (
    <Space size={[4, 4]} wrap>
      {entries.map(([k, v]) => (
        <Tag key={k} style={{ marginInlineEnd: 0 }}>
          {k}: {typeof v === "string" ? v : JSON.stringify(v)}
        </Tag>
      ))}
    </Space>
  );
}

export function AuditPage() {
  const { me, can, why, workspace, isPublic } = useSession();
  const allowed = can("audit");
  const wsId = me.workspace_id;
  const q = useInfiniteQuery({
    queryKey: ["audit", wsId],
    queryFn: ({ pageParam }) => api.audit(wsId, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => (last.length === PAGE ? last[last.length - 1].id : undefined),
    enabled: allowed,
  });
  // the audit log has user ids; current members give them names
  const members = useQuery({ queryKey: ["members", wsId], queryFn: () => api.members(wsId), enabled: allowed });
  const logins = new Map((members.data ?? []).map((m) => [m.user_id, m.login]));
  // and repo ids, their names
  const repoList = useQuery({ queryKey: ["repos"], queryFn: api.listRepos, enabled: allowed });
  const repos = new Map((repoList.data ?? []).map((r) => [r.id, repoName(r)]));

  if (!allowed) return <Result status="403" title="Audit log" subTitle={why("audit")} />;
  const events = q.data?.pages.flat() ?? [];

  return (
    <div>
      <Typography.Title level={3} style={{ marginTop: 0 }}>Audit log</Typography.Title>
      <Typography.Paragraph type="secondary">
        Who changed what{isPublic && workspace ? ` in ${workspace.name}` : ""}, newest first. Key values are never
        recorded, only that a key was saved or removed.
      </Typography.Paragraph>
      {q.isError && <Alert type="error" showIcon message={errorMessage(q.error)} style={{ marginBottom: 16 }} />}
      <Card styles={{ body: { padding: 0 } }}>
        <Table<AuditEvent>
          rowKey="id"
          size="small"
          loading={q.isLoading}
          dataSource={events}
          pagination={false}
          locale={{ emptyText: "Nothing recorded yet" }}
          scroll={{ x: 900 }}
          columns={[
            {
              title: "When",
              width: 170,
              render: (_, e) => <Tooltip title={fmtDateTime(e.at)}>{fmtRelative(e.at)}</Tooltip>,
            },
            { title: "Who", width: 200, render: (_, e) => <Actor e={e} /> },
            { title: "What", render: (_, e) => describeAction(e.action) },
            { title: "Target", width: 200, render: (_, e) => <Target e={e} logins={logins} repos={repos} /> },
            { title: "Details", render: (_, e) => <Details details={e.details} /> },
          ]}
        />
      </Card>
      {q.hasNextPage && (
        <div style={{ textAlign: "center", marginTop: 16 }}>
          <Button onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Load older entries</Button>
        </div>
      )}
    </div>
  );
}
