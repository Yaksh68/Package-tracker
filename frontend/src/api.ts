import type {
  Component,
  DiffResult,
  Repo,
  RepoCreate,
  RepoUpdate,
  ScanLog,
  UploadRepoMeta,
  ScanStatus,
  SnapshotSummary,
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

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...init });
  } catch {
    throw new ApiError(0, "Backend unreachable — is packages-tracker-backend running?");
  }
  if (!res.ok) throw await parseError(res);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

/** multipart/form-data POST — the browser sets the Content-Type boundary. */
async function postForm<T>(url: string, form: FormData): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { method: "POST", body: form });
  } catch {
    throw new ApiError(0, "Backend unreachable — is packages-tracker-backend running?");
  }
  if (!res.ok) throw await parseError(res);
  return res.json() as Promise<T>;
}

/** Human-readable message for anything thrown by an API call or mutation. */
export function errorMessage(e: unknown): string {
  if (e instanceof Error && e.message) return e.message;
  return typeof e === "string" ? e : "Something went wrong";
}

export const isNotFound = (e: unknown) => e instanceof ApiError && e.status === 404;
export const isClientError = (e: unknown) => e instanceof ApiError && e.status >= 400 && e.status < 500;

export interface AppSettings {
  use_enrichment_agent: boolean;
  use_extraction_agent: boolean;
}

export const api = {
  getSettings: () => req<AppSettings>("/api/settings"),
  updateSettings: (patch: Partial<AppSettings>) =>
    req<AppSettings>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),

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
    req<{ status: string }>(`/api/repos/${id}/scan?${new URLSearchParams({ branch })}`, { method: "POST" }),
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
