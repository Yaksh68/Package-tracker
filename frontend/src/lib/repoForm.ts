// Pure helpers behind the add/edit repository form.
import type { Repo, RepoCreate, RepoUpdate, UploadRepoMeta } from "../types";

// One `VAR=image:tag` line. Mirrors the server-side validation in models.py.
const TAG_LINE = /^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z0-9][A-Za-z0-9._:/@+-]*)?$/;

/** Every variable the last scan detected (in compose order) with the value set
 *  for it, then any extra tags added by hand. */
export function tagsToText(tags: Record<string, string> = {}, detected: string[] = []): string {
  const names = [...detected, ...Object.keys(tags).filter((k) => !detected.includes(k))];
  return names.map((n) => `${n}=${tags[n] ?? ""}`).join("\n");
}

/** Textarea → {VAR: ref}. Throws on a malformed line; blank values are omitted. */
export function textToTags(text: string | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of (text ?? "").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const m = TAG_LINE.exec(line);
    if (!m) throw new Error(`Expected VAR=image:tag — got "${line}"`);
    if (m[2]) out[m[1]] = m[2];
  }
  return out;
}

export interface RepoFormValues {
  url?: string;
  /** Upload repos: name the repo id derives from (add only). */
  name?: string;
  /** Upload repos: label for the chosen zip; blank = "upload". */
  version?: string;
  branches: string[];
  release_date: string | null; // YYYY-MM-DD
  interval_days: number;
  image_tags_text?: string;
  /** Typed into the token field; blank = keep (edit) / use env token (add). */
  token?: string;
  /** Edit only: remove the stored per-repo token. */
  remove_token?: boolean;
  ignored_packages?: string[];
}

const sameTags = (a: Record<string, string> = {}, b: Record<string, string> = {}) =>
  JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(b).sort());

export function buildCreatePayload(v: RepoFormValues): RepoCreate {
  const token = v.token?.trim();
  return {
    url: (v.url ?? "").trim(),
    branches: v.branches,
    interval_days: v.interval_days,
    release_date: v.release_date || null,
    image_tags: textToTags(v.image_tags_text),
    ignored_packages: v.ignored_packages ?? [],
    ...(token ? { token_override: token } : {}),
  };
}

export function buildUploadMeta(v: RepoFormValues): UploadRepoMeta {
  const version = v.version?.trim();
  return {
    name: (v.name ?? "").trim(),
    ...(version ? { version } : {}),
    interval_days: v.interval_days,
    release_date: v.release_date || null,
    image_tags: textToTags(v.image_tags_text),
    ignored_packages: v.ignored_packages ?? [],
  };
}

/**
 * Only what changed. In particular:
 * - the token is sent only when a new one was typed, or null when removal was
 *   ticked — never the "****" mask the API returns for a stored token;
 * - release_date only when it changed, because the backend re-flags every
 *   branch whenever the field is present.
 */
export function buildUpdatePayload(v: RepoFormValues, editing: Repo): RepoUpdate {
  const out: RepoUpdate = {};
  if (JSON.stringify(v.branches) !== JSON.stringify(editing.branches)) out.branches = v.branches;
  const rd = v.release_date || null;
  if (rd !== (editing.release_date ?? null)) out.release_date = rd;
  if (v.interval_days !== editing.interval_days) out.interval_days = v.interval_days;
  const tags = textToTags(v.image_tags_text);
  if (!sameTags(tags, editing.image_tags)) out.image_tags = tags;
  // changing the list re-flags every branch's latest snapshot, so only when it changed
  const ignored = v.ignored_packages ?? [];
  if (JSON.stringify(ignored) !== JSON.stringify(editing.ignored_packages ?? [])) out.ignored_packages = ignored;
  const token = v.token?.trim();
  if (token && token !== "****") out.token_override = token;
  else if (v.remove_token) out.token_override = null;
  return out;
}

/** Repo id the backend will derive from a URL (mirrors storage.slugify_repo_id). */
export function repoIdFromUrl(url: string): string {
  const name = url.trim().replace(/\/+$/, "").split("/").pop()?.replace(/\.git$/, "") ?? "";
  const slug = name.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  return slug || "repo";
}
