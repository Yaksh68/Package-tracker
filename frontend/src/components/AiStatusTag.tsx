import { Tag, Tooltip } from "antd";
import { aiStatusNotice } from "../lib/aiStatus";
import type { AiStatus } from "../types";

/** A small badge next to the snapshot picker when that snapshot's optional AI
 *  research had a problem. Nothing is shown when AI is off or went fine. */
export function AiStatusTag({ status, detail }: { status?: AiStatus | null; detail?: string | null }) {
  const notice = aiStatusNotice(status, detail);
  if (!notice) return null;
  return (
    <Tooltip title={notice.tooltip}>
      <Tag color="warning" style={{ marginInlineEnd: 0 }}>
        {notice.text}
      </Tag>
    </Tooltip>
  );
}
