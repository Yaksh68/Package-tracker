/** One thing that went wrong during a scan, attributed to the stage it broke in. */
export interface ScanIssue {
  stage: string;
  message: string;
  /** True only for the failure that aborted the run; everything else degraded it. */
  fatal: boolean;
}

/** Per-branch scan status/results, returned inline on each repo by GET /api/repos. */
export interface BranchInfo {
  branch: string;
  latest_snapshot: string | null;
  flag_counts: Record<string, number>;
  scan_state: string;
  last_scan: string | null;
  /** When the scheduler will next scan this branch (null if not scheduled). */
  next_scan?: string | null;
  /** The last scan finished but with missing data (survives a backend restart). */
  degraded?: boolean;
  /** Detail behind `degraded` — in-memory, so empty after a backend restart. */
  issues?: ScanIssue[];
}

export interface Repo {
  id: string;
  /** Display name. Repos in the public mode have random ids, so show this first. */
  name?: string | null;
  /** The workspace the repo belongs to. */
  workspace_id?: string;
  /** "upload": scanned from zips uploaded per version (`branches` are version labels). */
  source?: "git" | "upload";
  /** Clone URL, or `upload://<file name>` of the last zip for upload repos. */
  url: string;
  /** Each branch scans and is scheduled independently, with its own snapshot history. */
  branches: string[];
  release_date: string | null;
  interval_days: number;
  inspect_image?: string | null;
  /** Values for compose image-tag variables, e.g. { CORE_TAG: "acme/app:core-1.0" }. */
  image_tags?: Record<string, string>;
  /** Image-tag variables the last scan found in the repo's compose files. */
  detected_tag_vars?: string[];
  token_override: string | null;
  /** Package names flagged Ignored instead of evaluated (e.g. test-only tooling). */
  ignored_packages?: string[];
  created_at: string | null;
  last_scan_by_branch?: Record<string, string>;
  branches_info?: BranchInfo[];
}

export interface RepoCreate {
  url: string;
  branches: string[];
  release_date?: string | null;
  interval_days: number;
  image_tags?: Record<string, string>;
  token_override?: string | null;
  ignored_packages?: string[];
}

/** Settings sent with the zip when adding an upload repo (POST /repos/upload). */
export interface UploadRepoMeta {
  name: string;
  /** Label for this zip; the server uses "upload" when omitted. */
  version?: string;
  release_date?: string | null;
  interval_days: number;
  image_tags?: Record<string, string>;
  ignored_packages?: string[];
}

/** PUT /repos/{id}: only the fields present are changed. `token_override` is
 *  omitted to keep the stored token, a string to replace it, null to remove it. */
export interface RepoUpdate {
  branches?: string[];
  release_date?: string | null;
  interval_days?: number;
  image_tags?: Record<string, string>;
  token_override?: string | null;
  ignored_packages?: string[];
}

/** How a snapshot's optional AI research pass went. Separate from `degraded`:
 *  an AI problem leaves the scan's own results complete. */
export type AiStatus = "off" | "ok" | "partial" | "failed" | "quota" | "no_key";

export interface SnapshotSummary {
  ts: string;
  package_count: number;
  flag_counts: Record<string, number>;
  degraded: boolean;
  /** null for snapshots written before this was recorded */
  ai_status?: AiStatus | null;
  ai_detail?: string | null;
}

export interface CitationSource {
  url: string;
  note: string;
  origin?: "structured" | "web";
}

export interface Component {
  name: string;
  ecosystem: string;
  version: string | null;
  is_direct: boolean;
  source_file: string;
  latest_version: string | null;
  /** Newest non-prerelease release; null when none exists (or not yet re-scanned). */
  latest_stable_version: string | null;
  /** Derived server-side: a newer *stable* release exists than the pinned version. */
  update_available?: boolean;
  verdict: string | null;
  eol_date: string | null;
  eos_date: string | null;
  successor: string | null;
  /** Upstream SECURITY.md verdict for this release line; null = no policy / not covered. */
  security_supported?: boolean | null;
  /** The SECURITY.md table row that decided `security_supported`, e.g. "24.1.1". */
  security_policy_row?: string | null;
  confidence: string | null;
  summary: string | null;
  ai_researched?: boolean;
  sources: CitationSource[];
  flag: string | null;
  reasons: string[];
}

export interface ScanStatus {
  repo_id: string;
  branch: string;
  state: string;
  phase: string;
  started_at: string | null;
  finished_at: string | null;
  message: string;
  snapshot_ts: string | null;
  degraded: boolean;
  issues: ScanIssue[];
}

export interface LogEntry {
  ts: string;
  stage: string;
  level: string;
  message: string;
  data: Record<string, unknown> | null;
}

export interface ScanLog {
  repo_id: string;
  branch: string;
  snapshot_ts: string | null;
  state: string;
  entries: LogEntry[];
  /** What this scan's AI research used; null while it runs, and for old scans. */
  ai_usage?: AiUsage | null;
}

export interface AiUsage {
  status: AiStatus | null;
  detail: string | null;
  provider: string | null;
  model: string | null;
  batches: number;
  failed_batches: number;
  tokens_in: number;
  tokens_out: number;
  searches: number;
  /** An estimate; null when the provider's prices aren't known. */
  cost_usd: number | null;
  seconds: number | null;
}

// --------------------------------------------------------------------------
// Accounts, workspaces and roles (the public mode; the local mode has one
// built-in user who owns everything)
// --------------------------------------------------------------------------
export type Role = "viewer" | "editor" | "admin" | "owner";
export type Perm =
  | "view" | "edit" | "scan" | "delete_repo" | "settings" | "secrets" | "members" | "audit" | "owner";

export interface User {
  id: string;
  github_id: number | null;
  login: string;
  name: string | null;
  avatar_url: string | null;
  platform_role: "user" | "admin";
  status: "active" | "disabled";
  created_at: string | null;
  last_seen_at: string | null;
}

export interface Workspace {
  id: string;
  name: string;
  personal: boolean;
  /** The signed-in user's role in it. */
  role: Role | null;
}

/** GET /api/me */
export interface Me {
  mode: "local" | "github";
  user: User;
  /** The active workspace: the X-Workspace header, else the personal one. */
  workspace_id: string;
  role: Role;
  permissions: Perm[];
  /** What each role may do (the server's table). */
  role_permissions: Record<Role, Perm[]>;
  workspaces: Workspace[];
}

/** GET /api/info: what anyone may know, signed in or not. */
export interface ServiceInfo {
  mode: "local" | "github";
  operator_name: string | null;
  contact_email: string | null;
}

export interface Member {
  user_id: string;
  login: string;
  name: string | null;
  avatar_url: string | null;
  role: Role;
  joined_at: string | null;
}

export interface Invite {
  id: string;
  workspace_id: string;
  workspace_name: string | null;
  role: Role;
  /** null for a link invite */
  github_login: string | null;
  link: boolean;
  created_at: string;
  expires_at: string;
  status: string;
  /** Only in the response that creates a link invite: it's shown once. */
  url?: string;
}

export interface AuditEvent {
  id: number;
  at: string;
  action: string;
  actor: { kind: "user" | "scheduler" | "platform_admin"; user_id: string | null; login: string | null };
  target_type: string | null;
  target_id: string | null;
  details: Record<string, unknown>;
}

export type SecretName = "ai_api_key" | "search_api_key" | "github_token" | "gitlab_token";

/** All the UI ever learns about a secret. `hint` is for Admins and Owners only. */
export interface SecretHint {
  set: boolean;
  hint: string | null;
  updated_at: string | null;
}

/** GET /api/settings: the active workspace's settings. */
export interface WorkspaceSettings {
  use_enrichment_agent: boolean;
  use_extraction_agent: boolean;
  ai_provider: string;
  ai_model: string;
  ai_base_url: string;
  search_provider: string;
  secrets: Record<SecretName, SecretHint>;
}

export type SettingsPatch = Partial<Omit<WorkspaceSettings, "secrets">>;

/** POST /api/settings/ai-test */
export interface AiTestResult {
  ok: boolean;
  models: string[];
  kind?: string;
  message?: string;
}

export interface AdminUser extends User {
  /** How many workspaces they're a member of. */
  workspaces: number;
}

export interface AdminWorkspace {
  id: string;
  name: string;
  personal: boolean;
  created_by: string | null;
  created_at: string;
  members: number;
  repos: number;
}

export interface AdminWorkspaceDetail {
  id: string;
  name: string;
  personal: boolean;
  members: Member[];
  repos: {
    id: string;
    name: string | null;
    url: string;
    source: string;
    branches: string[];
    created_at: string | null;
    last_scan_by_branch: Record<string, string>;
  }[];
}

export interface DiffResult {
  from: string;
  to: string;
  changed: { name: string; from: string; to: string }[];
  added: { name: string; flag: string }[];
  removed: { name: string; flag: string }[];
}

export const FLAG_ORDER = [
  "ALREADY_EOL",
  "EOL_BEFORE_RELEASE",
  "EOL_SOON_AFTER_RELEASE",
  "DEPRECATED",
  "ARCHIVED",
  "EOS_PASSED",
  "STALE",
  "UNKNOWN",
  "OK",
  "IGNORED",
] as const;
