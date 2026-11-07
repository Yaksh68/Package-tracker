import { DeleteOutlined, EditOutlined, EyeOutlined, WarningOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Col, Empty, Popconfirm, Row, Skeleton, Space, Tag, Tooltip, Typography, theme } from "antd";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, errorMessage } from "../api";
import { FlagCounts } from "../components/FlagBadge";
import { fmtDate, fmtDateTime, fmtDue, fmtRelative } from "../format";
import { RepoFormModal } from "../components/RepoFormModal";
import { ScanStatusPill } from "../components/ScanStatusPill";
import type { BranchInfo, Repo, RepoCreate, RepoUpdate, UploadRepoMeta } from "../types";

const isActive = (s?: string) => s === "running" || s === "queued";

/** Compose repos take their image tags from variables; without values those
 * images can't be tracked at all, so surface how many are still missing. */
function ImageTagSummary({ repo }: { repo: Repo }) {
  const needed = repo.detected_tag_vars ?? [];
  if (needed.length === 0) return null;
  const missing = needed.filter((v) => !repo.image_tags?.[v]);
  return (
    <Tag color={missing.length ? "gold" : "blue"}>
      image tags: {needed.length - missing.length}/{needed.length} set
      {missing.length > 0 && ` · missing ${missing.join(", ")}`}
    </Tag>
  );
}

/** Tooltip body for the "degraded" tag: the actual stage failures when the
 * backend still has them in memory, otherwise a pointer to the trace. */
function degradedDetail(info: BranchInfo) {
  const issues = info.issues ?? [];
  if (issues.length === 0) return "The last scan finished with missing data — open Activity for details.";
  return (
    <ul style={{ margin: 0, paddingLeft: 16 }}>
      {issues.map((i, n) => (
        <li key={n}>
          {i.stage}: {i.message}
        </li>
      ))}
    </ul>
  );
}

/** One row per tracked branch — each scans and reports independently, so this
 * is the "check scan results per branch" view for a repo at a glance. Clicking
 * a row jumps straight to that branch on the repo's detail page. */
function BranchRow({ repoId, info }: { repoId: string; info: BranchInfo }) {
  const { token } = theme.useToken();
  const [hover, setHover] = useState(false);
  return (
    <Link
      to={`/repos/${repoId}?branch=${encodeURIComponent(info.branch)}`}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        color: "inherit",
        display: "block",
        margin: "0 -12px",
        padding: "10px 12px",
        borderTop: `1px solid ${token.colorBorderSecondary}`,
        background: hover ? token.colorFillQuaternary : undefined,
        borderRadius: 4,
      }}
    >
      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        <Space size={10} wrap>
          <Typography.Text strong>{info.branch}</Typography.Text>
          <ScanStatusPill state={info.scan_state} />
          {info.degraded && info.scan_state !== "error" && (
            <Tooltip title={degradedDetail(info)}>
              <Tag color="gold" icon={<WarningOutlined />}>incomplete</Tag>
            </Tooltip>
          )}
        </Space>
        <FlagCounts counts={info.flag_counts} empty={isActive(info.scan_state) ? "first scan running" : "no scan yet"} />
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          <Tooltip title={`Last scan: ${fmtDateTime(info.last_scan)}`}>
            <span>Scanned {info.last_scan ? fmtRelative(info.last_scan) : "never"}</span>
          </Tooltip>
          {info.next_scan && (
            <Tooltip title={`Next scan: ${fmtDateTime(info.next_scan)}`}>
              <span> · next {fmtDue(info.next_scan)}</span>
            </Tooltip>
          )}
        </Typography.Text>
      </Space>
    </Link>
  );
}

/** Write a just-created/updated repo straight into the list cache.
 *
 * POST/PUT return the repo itself but no `branches_info` — that's derived
 * per-branch by the list endpoint — so the rows are seeded here: carried over
 * from what the cache already had, and `state` for branches we haven't seen.
 * Without this the card only appears once the refetch behind
 * `invalidateQueries` comes back, which is exactly when the backend is busiest
 * (POST /repos kicks off the first scan for every branch). */
function cacheRepo(qc: QueryClient, repo: Repo, state: string) {
  qc.setQueryData<Repo[]>(["repos"], (old = []) => {
    const prev = old.find((r) => r.id === repo.id);
    const seen = new Map((prev?.branches_info ?? []).map((i) => [i.branch, i]));
    const next: Repo = {
      ...prev,
      ...repo,
      branches_info: repo.branches.map(
        (b) =>
          seen.get(b) ?? {
            branch: b,
            latest_snapshot: null,
            flag_counts: {},
            scan_state: state,
            last_scan: null,
          },
      ),
    };
    return prev ? old.map((r) => (r.id === repo.id ? next : r)) : [...old, next];
  });
}

export function RepoList() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Repo | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const { message } = App.useApp();

  const reposQuery = useQuery({
    queryKey: ["repos"],
    queryFn: api.listRepos,
    // quick refresh while something is scanning, slow otherwise
    refetchInterval: (q) =>
      (q.state.data ?? []).some((r) => (r.branches_info ?? []).some((b) => isActive(b.scan_state))) ? 4000 : 30000,
  });
  const { data: repos = [], isLoading } = reposQuery;

  const closeModal = () => {
    setModalOpen(false);
    setEditing(null);
    setFormError(null);
  };

  const createMut = useMutation({
    mutationFn: (b: RepoCreate) => api.createRepo(b),
    // "running", not "idle": the server has already started the first scan for
    // every branch by the time it responds.
    onSuccess: (repo) => {
      message.success(`Added ${repo.id} — first scan started`);
      cacheRepo(qc, repo, "running");
      qc.invalidateQueries({ queryKey: ["repos"] });
      closeModal();
    },
    onError: (e) => setFormError(errorMessage(e)),
  });

  const createUploadMut = useMutation({
    mutationFn: ({ meta, file }: { meta: UploadRepoMeta; file: File }) => api.createUploadRepo(meta, file),
    onSuccess: (repo) => {
      message.success(`Added ${repo.id} — first scan started`);
      cacheRepo(qc, repo, "running");
      qc.invalidateQueries({ queryKey: ["repos"] });
      closeModal();
    },
    onError: (e) => setFormError(errorMessage(e)),
  });

  const updateMut = useMutation({
    // changes first, then the zip (which may add a version and starts its scan)
    mutationFn: async ({ id, body, upload }: { id: string; body: RepoUpdate; upload?: { file: File; version?: string } }) => {
      let repo = Object.keys(body).length ? await api.updateRepo(id, body) : null;
      if (upload) repo = await api.uploadVersion(id, upload.file, upload.version);
      return repo!;
    },
    onSuccess: (repo, { body, upload }) => {
      message.success(
        upload
          ? `Uploaded to ${repo.id} — scan started`
          : "release_date" in body
            ? `Saved ${repo.id} — results re-checked against the new release date`
            : `Saved ${repo.id}`,
      );
      if (!upload) cacheRepo(qc, repo, "idle"); // an edit starts no scan
      qc.invalidateQueries({ queryKey: ["repos"] });
      qc.invalidateQueries({ queryKey: ["repo", repo.id] });
      if ("release_date" in body) qc.invalidateQueries({ queryKey: ["components", repo.id] });
      closeModal();
    },
    onError: (e) => setFormError(errorMessage(e)),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.deleteRepo(id),
    onSuccess: (_data, id) => {
      message.success(`Deleted ${id}`);
      qc.setQueryData<Repo[]>(["repos"], (old = []) => old.filter((r) => r.id !== id));
      qc.invalidateQueries({ queryKey: ["repos"] });
    },
    onError: (e) => message.error(errorMessage(e)),
  });

  return (
    <div>
      <Space style={{ marginBottom: 16, justifyContent: "space-between", width: "100%" }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          Repositories
        </Typography.Title>
        <Button type="primary" onClick={() => { setEditing(null); setFormError(null); setModalOpen(true); }}>
          Add repository
        </Button>
      </Space>

      {reposQuery.isError ? (
        <Alert
          type="error"
          showIcon
          message="Couldn't load repositories"
          description={errorMessage(reposQuery.error)}
          action={<Button size="small" onClick={() => reposQuery.refetch()}>Retry</Button>}
        />
      ) : isLoading ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : repos.length === 0 ? (
        <Empty description="No repositories yet — add one to start tracking.">
          <Button type="primary" onClick={() => { setEditing(null); setFormError(null); setModalOpen(true); }}>
            Add repository
          </Button>
        </Empty>
      ) : (
        <Row gutter={[16, 16]}>
          {repos.map((r) => (
            <Col key={r.id} xs={24} md={12} lg={8}>
              <Card
                hoverable
                title={<Link to={`/repos/${r.id}`}>{r.id}</Link>}
                actions={[
                  <Button key="view" type="text" icon={<EyeOutlined />} onClick={() => navigate(`/repos/${r.id}`)}>
                    View
                  </Button>,
                  <Button key="edit" type="text" icon={<EditOutlined />} onClick={() => { setEditing(r); setFormError(null); setModalOpen(true); }}>
                    Edit
                  </Button>,
                  <Popconfirm
                    key="del"
                    title="Delete repository"
                    description={`This removes ${r.id} and the scan history of all its branches.`}
                    okText="Delete"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => deleteMut.mutate(r.id)}
                  >
                    <Button type="text" danger icon={<DeleteOutlined />} loading={deleteMut.isPending && deleteMut.variables === r.id}>
                      Delete
                    </Button>
                  </Popconfirm>,
                ]}
              >
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  <Typography.Text type="secondary" ellipsis>
                    {r.source === "upload" ? `Uploaded zip · ${r.url.replace(/^upload:\/\//, "")}` : r.url}
                  </Typography.Text>
                  <Typography.Text type="secondary">
                    {r.release_date ? `Release ${fmtDate(r.release_date)}` : "No release date"} · scanned every{" "}
                    {r.interval_days === 1 ? "day" : `${r.interval_days} days`}
                  </Typography.Text>
                  <ImageTagSummary repo={r} />
                  <div>
                    {(r.branches_info ?? r.branches.map((b) => ({
                      branch: b, latest_snapshot: null, flag_counts: {}, scan_state: "idle", last_scan: null,
                    }))).map((info) => (
                      <BranchRow key={info.branch} repoId={r.id} info={info} />
                    ))}
                  </div>
                </Space>
              </Card>
            </Col>
          ))}
        </Row>
      )}

      <RepoFormModal
        open={modalOpen}
        editing={editing}
        confirmLoading={createMut.isPending || createUploadMut.isPending || updateMut.isPending}
        error={formError}
        onCancel={closeModal}
        onCreate={(b) => createMut.mutate(b)}
        onCreateUpload={(meta, file) => createUploadMut.mutate({ meta, file })}
        onUpdate={(id, body, upload) => {
          if (Object.keys(body).length === 0 && !upload) {
            closeModal();
            message.info("No changes to save");
          } else updateMut.mutate({ id, body, upload });
        }}
      />
    </div>
  );
}
