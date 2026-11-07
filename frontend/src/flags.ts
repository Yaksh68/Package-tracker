// One definition of every flag — label, tag colour and meaning — so the list,
// the table, the drawer and history all show flags the same way.
import { FLAG_ORDER } from "./types";

export interface FlagInfo {
  label: string;
  color: string; // antd Tag preset; readable in both themes
  help: string;
}

export const FLAGS: Record<string, FlagInfo> = {
  ALREADY_EOL: { label: "End of life", color: "red", help: "Past its end-of-life date, or its maintainers list this version as unsupported. Replace or upgrade now." },
  EOL_BEFORE_RELEASE: { label: "EOL before release", color: "volcano", help: "Reaches end of life on or before the planned release date. Fix before shipping." },
  EOL_SOON_AFTER_RELEASE: { label: "EOL soon after release", color: "orange", help: "Reaches end of life within a few months after the release. Plan the upgrade." },
  DEPRECATED: { label: "Deprecated", color: "gold", help: "Deprecated, and a newer version or a replacement exists." },
  ARCHIVED: { label: "Archived", color: "cyan", help: "The project's repository is archived and no longer maintained." },
  EOS_PASSED: { label: "Support ended", color: "gold", help: "Support has ended (older snapshots only)." },
  STALE: { label: "Stale", color: "default", help: "No release in over 2 years. May be fine, may need replacing." },
  UNKNOWN: { label: "Unknown", color: "default", help: "No end-of-life information was found." },
  OK: { label: "OK", color: "green", help: "Supported; nothing due." },
  IGNORED: { label: "Ignored", color: "default", help: "On this repository's ignore list (e.g. test-only packages). Not counted as a risk." },
};

export function flagInfo(flag: string | null | undefined): FlagInfo | null {
  if (!flag) return null;
  return FLAGS[flag] ?? { label: flag.replace(/_/g, " ").toLowerCase(), color: "default", help: "" };
}

/** 0 = most severe; missing or unknown flags sort last. */
export function flagRank(flag: string | null | undefined): number {
  const i = flag ? (FLAG_ORDER as readonly string[]).indexOf(flag) : -1;
  return i === -1 ? FLAG_ORDER.length : i;
}

/** Flags that mean "needs attention" (everything but Unknown, OK and Ignored). */
export const RISK_FLAGS = FLAG_ORDER.filter((f) => f !== "UNKNOWN" && f !== "OK" && f !== "IGNORED");
