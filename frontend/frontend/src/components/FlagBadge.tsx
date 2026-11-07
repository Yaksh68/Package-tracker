import { Space, Tag, Tooltip, Typography } from "antd";
import { flagInfo, flagRank } from "../flags";

export function FlagBadge({ flag, count }: { flag: string | null | undefined; count?: number }) {
  const f = flagInfo(flag);
  if (!f) return <Tag>—</Tag>;
  return (
    <Tooltip title={f.help}>
      <Tag color={f.color} style={{ marginInlineEnd: 0 }}>
        {f.label}
        {count !== undefined && <strong style={{ marginLeft: 6 }}>{count}</strong>}
      </Tag>
    </Tooltip>
  );
}

/** Flag counts as coloured tags, most severe first. OK is summarised as text so
 *  the tags that need attention stand out. */
export function FlagCounts({ counts, empty = "no scan yet" }: { counts?: Record<string, number>; empty?: string }) {
  if (!counts || Object.keys(counts).length === 0) return <Tag>{empty}</Tag>;
  const flags = Object.keys(counts)
    .filter((f) => f !== "OK" && counts[f] > 0)
    .sort((a, b) => flagRank(a) - flagRank(b));
  return (
    <Space wrap size={[4, 4]}>
      {flags.map((f) => (
        <FlagBadge key={f} flag={f} count={counts[f]} />
      ))}
      {counts.OK ? <Typography.Text type="secondary">{counts.OK} OK</Typography.Text> : null}
    </Space>
  );
}
