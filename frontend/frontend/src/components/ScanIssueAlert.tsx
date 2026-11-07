import { Alert, Space, Tag, Typography } from "antd";
import type { ReactNode } from "react";
import type { ScanIssue } from "../types";

/** Human-readable cause + next step for the failures users actually hit, keyed
 * by a substring of the backend message. Without this a scan just says
 * "pull access denied" and leaves the reader to work out whether it matters. */
const HINTS: { match: RegExp; hint: string }[] = [
  {
    match: /pull access denied|repository does not exist|manifest unknown|not found: manifest/i,
    hint: "That image isn't in any registry the host can reach — it's usually a locally built tag or a private repo needing `docker login`. The rest of the scan is unaffected.",
  },
  {
    match: /docker is not installed|not on PATH|Cannot connect to the Docker daemon/i,
    hint: "Docker isn't available to the backend, so OS-package inventory was skipped. Everything parsed from manifests is still complete.",
  },
  { match: /timed out/i, hint: "Raise the timeout or pre-pull the image on the host, then re-scan." },
  {
    match: /apt-get update could not reach/i,
    hint: "The container had no network for `apt-get update`, so unpinned apt versions stayed unresolved.",
  },
  {
    // real HTTP auth failures only — not any "401"/"403" digits in a digest or version
    match: /Authentication failed|could not read Username|\b401 Unauthorized\b|\b403 Forbidden\b|\b(?:HTTP|status|error|returned|code)\b\D{0,12}\b(?:401|403)\b/i,
    hint: "The clone token is missing, expired, or lacks read access to this repo.",
  },
  {
    match: /couldn't find remote ref|Remote branch .* not found/i,
    hint: "That branch no longer exists on the remote — edit the repo to drop or rename it.",
  },
  { match: /enrichment degraded/i, hint: "EOL data couldn't be fetched, so components are reported as UNKNOWN rather than cleared." },
];

function hintFor(message: string): string | undefined {
  return HINTS.find((h) => h.match.test(message))?.hint;
}

/** The one place scan failures are explained. Renders nothing when a scan is
 * clean, so callers can drop it in unconditionally. */
export function ScanIssueAlert({
  issues = [],
  degraded,
  state,
  fallbackMessage,
  action,
}: {
  issues?: ScanIssue[];
  degraded?: boolean;
  state?: string;
  /** Shown when the run degraded but the issue list is empty (e.g. the detail
   * was lost to a backend restart, or the snapshot predates issue tracking). */
  fallbackMessage?: string;
  action?: ReactNode;
}) {
  const running = state === "running" || state === "queued";
  const failed = !running && (state === "error" || issues.some((i) => i.fatal));
  if (issues.length === 0 && !degraded && !fallbackMessage) return null;

  const title = running
    ? `Scan in progress — ${issues.length || "some"} issue${issues.length === 1 ? "" : "s"} so far`
    : failed
    ? "Scan failed"
    : `Scan completed with ${issues.length || "some"} issue${issues.length === 1 ? "" : "s"} — results may be incomplete`;

  return (
    <Alert
      type={failed ? "error" : "warning"}
      showIcon
      style={{ marginBottom: 16 }}
      message={title}
      action={action}
      description={
        issues.length > 0 ? (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            {issues.map((i, n) => {
              const hint = hintFor(i.message);
              return (
                <div key={n}>
                  <Tag>{i.stage}</Tag>
                  <Typography.Text code style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
                    {i.message}
                  </Typography.Text>
                  {hint && (
                    <Typography.Paragraph type="secondary" style={{ margin: "4px 0 0 4px", fontSize: 12 }}>
                      {hint}
                    </Typography.Paragraph>
                  )}
                </div>
              );
            })}
          </Space>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {fallbackMessage || "See the activity trace for what went wrong."}
          </Typography.Text>
        )
      }
    />
  );
}
