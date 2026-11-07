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

export interface SnapshotSummary {
  ts: string;
  package_count: number;
  flag_counts: Record<string, number>;
  degraded: boolean;
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
