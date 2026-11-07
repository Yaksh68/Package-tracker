import type { AiStatus } from "../types";

export interface AiStatusNotice {
  text: string;
  tooltip: string;
}

const COMPLETE = "AI research is optional: the scan's own results are complete.";

/** What to show for a snapshot's AI research outcome, or null when there's
 *  nothing to flag. "off" and "ok" need no badge, and none of these means the
 *  scan itself is incomplete: that's what "degraded" is for. */
export function aiStatusNotice(status?: AiStatus | null, detail?: string | null): AiStatusNotice | null {
  const why = detail ? `${detail}. ` : "";
  switch (status) {
    case "partial":
      return { text: "AI: partial", tooltip: `${why}Packages in the failed batches kept their structured verdicts. ${COMPLETE}` };
    case "failed":
      return { text: "AI: failed", tooltip: `${why}Every package kept its structured verdict. ${COMPLETE}` };
    case "quota":
      return { text: "AI: quota reached", tooltip: `${why}The AI provider's rate limit or quota stopped the research. ${COMPLETE}` };
    case "no_key":
      return { text: "AI: no key", tooltip: `Add an AI key in Settings to research ambiguous packages. ${COMPLETE}` };
    default:
      return null;
  }
}
