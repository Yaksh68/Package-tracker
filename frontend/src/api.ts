import type {
  AdminUser,
  AdminWorkspace,
  AdminWorkspaceDetail,
  AiTestResult,
  AuditEvent,
  Component,
  DiffResult,
  Invite,
  Me,
  Member,
  Repo,
  RepoCreate,
  RepoUpdate,
  Role,
  ScanLog,
  ScanRequest,
  SecretHint,
  SecretName,
  ServiceInfo,
  SettingsPatch,
  UploadRepoMeta,
  ScanStatus,
  SnapshotSummary,
  Workspace,
  WorkspaceSettings,
} from "./types";

/** A failed API call. `message` is already human-readable: FastAPI's `detail`
 *  (a string, or a 422 list of field errors) rather than the raw response body. */
export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function detailMessage(detail: unknown): string | null {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        if (!d || typeof d !== "object") return null;
        const { loc, msg } = d as { loc?: unknown[]; msg?: string };
        const field = Array.isArray(loc) ? loc.slice(1).join(".") : "";
        return msg ? (field ? `${field}: ${msg}` : msg) : null;
      })
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  return null;
}

export async function parseError(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => "");
  let detail: unknown = undefined;
  try {
    detail = text ? (JSON.parse(text) as { detail?: unknown }).detail : undefined;
  } catch {
    /* not JSON */
  }
  const msg =
    detailMessage(detail) ??
    (res.status >= 500 ? `Server error (${res.status})` : res.statusText || `Request failed (${res.status})`);
  return new ApiError(res.status, msg, detail);
}

/** Sent on every request. In the public mode the backend refuses data-changing
 *  requests without it: another site can't add a custom header without a CORS
 *  preflight, so this proves the request came from our own UI (CSRF). */
export const CSRF_HEADERS = { "X-Requested-With": "packages-tracker" } as const;

// The active workspace, sent as X-Workspace on every request. Each browser
// remembers its own; without one the server uses the personal workspace.
const WORKSPACE_KEY = "packages-tracker.workspace";
let activeWorkspace: string | null = (() => {
  try {
    return localStorage.getItem(WORKSPACE_KEY);
  } catch {
    return null; // storage blocked: the personal workspace it is
  }
})();

export const getActiveWorkspace = () => activeWorkspace;

/** Switch workspaces. Callers also drop cached queries: they're per workspace. */
export function setActiveWorkspace(id: string | null) {
  activeWorkspace = id;
  try {
    if (id) localStorage.setItem(WORKSPACE_KEY, id);
    else localStorage.removeItem(WORKSPACE_KEY);
  } catch {
    /* storage blocked: the choice lasts until the page reloads */
  }
}

// Called on a 401 from any call but /api/me: the session ended (signed out
// elsewhere, expired, or the account was disabled). The session gate re-checks
// /api/me and shows sign-in. A 401 from /api/me is that check's own answer;
// passing it on would re-check forever.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null) {
  onUnauthorized = fn;
}

function baseHeaders(): Record<string, string> {
  return { ...CSRF_HEADERS, ...(activeWorkspace ? { "X-Workspace": activeWorkspace } : {}) };
}

async function send(url: string, init: RequestInit): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    throw new ApiError(0, "Backend unreachable — is packages-tracker-backend running?");
  }
  if (res.status === 401 && url !== "/api/me") onUnauthorized?.();
  if (!res.ok) throw await parseError(res);
  return res;
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await send(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...baseHeaders(), ...(init?.headers ?? {}) },
  });
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

/** multipart/form-data POST — the browser sets the Content-Type boundary. */
async function postForm<T>(url: string, form: FormData): Promise<T> {
  // no Content-Type: the browser sets the multipart boundary itself
  const res = await send(url, { method: "POST", body: form, headers: baseHeaders() });
  return res.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });
const enc = encodeURIComponent;

/** Human-readable message for anything thrown by an API call or mutation. */
export function errorMessage(e: unknown): string {
  if (e instanceof Error && e.message) return e.message;
  return typeof e === "string" ? e : "Something went wrong";
}

export const isNotFound = (e: unknown) => e instanceof ApiError && e.status === 404;
export const isUnauthorized = (e: unknown) => e instanceof ApiError && e.status === 401;
export const isClientError = (e: unknown) => e instanceof ApiError && e.status >= 400 && e.status < 500;

export const api = {
  // ---- the service and the signed-in account ----
  info: () => req<ServiceInfo>("/api/info"),
  me: () => req<Me>("/api/me"),
  deleteAccount: () => req<void>("/api/me", { method: "DELETE" }),
  logout: () => req<void>("/api/auth/logout", { method: "POST" }),
  /** Where the "Sign in with GitHub" button goes; `next` is where to land after. */
  loginUrl: (next?: string) =>
    `/api/auth/github/login${next && next !== "/" ? `?${new URLSearchParams({ next })}` : ""}`,

  // ---- the active workspace's settings and secrets ----
  getSettings: () => req<WorkspaceSettings>("/api/settings"),
  updateSettings: (patch: SettingsPatch) =>
    req<WorkspaceSettings>("/api/settings", { method: "PUT", ...json(patch) }),
  setSecret: (name: SecretName, value: string) =>
    req<{ name: SecretName } & SecretHint>(`/api/secrets/${name}`, { method: "PUT", ...json({ value }) }),
  deleteSecret: (name: SecretName) => req<void>(`/api/secrets/${name}`, { method: "DELETE" }),
  /** Test connection: the provider's models with this key (typed, or else the saved one). */
  testAi: (body: { provider: string; base_url?: string; api_key?: string }) =>
    req<AiTestResult>("/api/settings/ai-test", { method: "POST", ...json(body) }),

  // ---- workspaces, members, invites, audit log ----
  workspaces: () => req<Workspace[]>("/api/workspaces"),
  createWorkspace: (name: string) => req<Workspace>("/api/workspaces", { method: "POST", ...json({ name }) }),
  renameWorkspace: (id: string, name: string) =>
    req<{ id: string; name: string }>(`/api/workspaces/${enc(id)}`, { method: "PUT", ...json({ name }) }),
  deleteWorkspace: (id: string) => req<void>(`/api/workspaces/${enc(id)}`, { method: "DELETE" }),
  transferWorkspace: (id: string, userId: string) =>
    req<unknown>(`/api/workspaces/${enc(id)}/transfer`, { method: "POST", ...json({ user_id: userId }) }),
  leaveWorkspace: (id: string) => req<void>(`/api/workspaces/${enc(id)}/leave`, { method: "POST" }),
  members: (id: string) => req<Member[]>(`/api/workspaces/${enc(id)}/members`),
  setRole: (id: string, userId: string, role: Role) =>
    req<unknown>(`/api/workspaces/${enc(id)}/members/${enc(userId)}`, { method: "PUT", ...json({ role }) }),
  removeMember: (id: string, userId: string) =>
    req<void>(`/api/workspaces/${enc(id)}/members/${enc(userId)}`, { method: "DELETE" }),
  invites: (id: string) => req<Invite[]>(`/api/workspaces/${enc(id)}/invites`),
  /** By GitHub login, or (no login) a one-time link whose `url` is returned only now. */
  createInvite: (id: string, role: Role, githubLogin?: string) =>
    req<Invite>(`/api/workspaces/${enc(id)}/invites`, {
      method: "POST",
      ...json({ role, github_login: githubLogin || null }),
    }),
  revokeInvite: (id: string, inviteId: string) =>
    req<void>(`/api/workspaces/${enc(id)}/invites/${enc(inviteId)}`, { method: "DELETE" }),
  myInvites: () => req<Invite[]>("/api/invites"),
  acceptInvite: (inviteId: string) => req<Invite>(`/api/invites/${enc(inviteId)}/accept`, { method: "POST" }),
  declineInvite: (inviteId: string) => req<Invite>(`/api/invites/${enc(inviteId)}/decline`, { method: "POST" }),
  acceptInviteLink: (token: string) => req<Invite>("/api/invites/accept-link", { method: "POST", ...json({ token }) }),
  audit: (id: string, before?: number, limit = 50) =>
    req<AuditEvent[]>(
      `/api/workspaces/${enc(id)}/audit?${new URLSearchParams({ limit: String(limit), ...(before ? { before: String(before) } : {}) })}`,
    ),

  // ---- the platform admin ----
  adminUsers: () => req<AdminUser[]>("/api/admin/users"),
  setUserStatus: (userId: string, status: "active" | "disabled") =>
    req<unknown>(`/api/admin/users/${enc(userId)}`, { method: "PUT", ...json({ status }) }),
  adminDeleteUser: (userId: string) => req<void>(`/api/admin/users/${enc(userId)}`, { method: "DELETE" }),
  adminWorkspaces: () => req<AdminWorkspace[]>("/api/admin/workspaces"),
  adminWorkspace: (id: string) => req<AdminWorkspaceDetail>(`/api/admin/workspaces/${enc(id)}`),

  // ---- repos and scans ----

  listRepos: () => req<Repo[]>("/api/repos"),
  getRepo: (id: string) => req<Repo>(`/api/repos/${id}`),
  createRepo: (body: RepoCreate) =>
    req<Repo>("/api/repos", { method: "POST", body: JSON.stringify(body) }),
  updateRepo: (id: string, body: RepoUpdate) =>
    req<Repo>(`/api/repos/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteRepo: (id: string) =>
    req<void>(`/api/repos/${id}`, { method: "DELETE" }),
  /** Add a repo scanned from a zip; the server starts its first scan. */
  createUploadRepo: (meta: UploadRepoMeta, file: File) => {
    const form = new FormData();
    form.append("meta", JSON.stringify(meta));
    form.append("file", file);
    return postForm<Repo>("/api/repos/upload", form);
  },
  /** Zip for a new version (or replacing one); the server scans it. */
  uploadVersion: (id: string, file: File, version?: string) => {
    const form = new FormData();
    form.append("file", file);
    if (version?.trim()) form.append("version", version.trim());
    return postForm<Repo>(`/api/repos/${id}/upload`, form);
  },

  // Branch names can contain "/" (e.g. release/6.1), so it's always a query
  // param — never a path segment — and URLSearchParams-encoded.
  scanNow: (id: string, branch: string) =>
    req<ScanRequest>(`/api/repos/${id}/scan?${new URLSearchParams({ branch })}`, { method: "POST" }),
  scanStatus: (id: string, branch: string) =>
    req<ScanStatus>(`/api/repos/${id}/scan-status?${new URLSearchParams({ branch })}`),
  scanLog: (id: string, branch: string, ts?: string) =>
    req<ScanLog>(`/api/repos/${id}/scan-log?${new URLSearchParams({ branch, ...(ts ? { ts } : {}) })}`),

  snapshots: (id: string, branch: string) =>
    req<SnapshotSummary[]>(`/api/repos/${id}/snapshots?${new URLSearchParams({ branch })}`),
  components: (id: string, branch: string, ts: string, params: Record<string, string> = {}) =>
    req<Component[]>(
      `/api/repos/${id}/snapshots/${ts}/components?${new URLSearchParams({ branch, ...params })}`,
    ),
  reportCsvUrl: (id: string, branch: string, ts: string) =>
    `/api/repos/${id}/snapshots/${ts}/report.csv?${new URLSearchParams({ branch })}`,
  diff: (id: string, branch: string, from: string, to: string) =>
    req<DiffResult>(`/api/repos/${id}/diff?${new URLSearchParams({ branch, from, to })}`),
};
