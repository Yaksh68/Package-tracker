import { Descriptions, Drawer, List, Space, Tag, Tooltip, Typography } from "antd";
import { fmtDate, fmtSnapshotTs } from "../format";
import type { CitationSource, Component } from "../types";
import { FlagBadge } from "./FlagBadge";

const { Paragraph, Link, Text } = Typography;

function CitationList({ sources }: { sources: CitationSource[] }) {
  return (
    <List
      size="small"
      dataSource={sources}
      locale={{ emptyText: "No citations" }}
      renderItem={(s) => (
        <List.Item>
          <Link href={s.url} target="_blank" rel="noreferrer">
            {s.note || s.url}
          </Link>
        </List.Item>
      )}
    />
  );
}

export function CitationsDrawer({
  component,
  onClose,
}: {
  component: Component | null;
  onClose: () => void;
}) {
  return (
    <Drawer
      title={component ? `${component.name} ${component.version ?? ""}` : ""}
      open={!!component}
      onClose={onClose}
      width={520}
    >
      {component && (
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <Space wrap>
            <FlagBadge flag={component.flag} />
            <Text type="secondary">{component.ecosystem}</Text>
            {component.confidence && <Text type="secondary">confidence: {component.confidence}</Text>}
            {component.ai_researched && (
              <Tooltip title={component.ai_reused_from
                ? `Researched in the scan of ${fmtSnapshotTs(component.ai_reused_from)} and reused, instead of asking the AI again. Change how long findings are reused in Settings.`
                : undefined}>
                <Tag>{component.ai_reused_from ? "AI researched (reused)" : "AI researched"}</Tag>
              </Tooltip>
            )}
          </Space>

          {component.reasons.length > 0 && (
            <div>
              <Text strong>Why it's flagged</Text>
              <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
                {component.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </div>
          )}

          <div>
            <Text strong>{component.ai_researched ? "AI research summary" : "Summary"}</Text>
            <Paragraph style={{ marginTop: 4 }}>
              {component.summary || "No summary available."}
            </Paragraph>
          </div>

          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="Version">{component.version ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="Latest stable">
              <Space size={6}>
                <span>{component.latest_stable_version ?? "—"}</span>
                {component.update_available && <Tag color="green">↑ update available</Tag>}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="Verdict">{component.verdict ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="End of life">{component.eol_date ? fmtDate(component.eol_date) : "—"}</Descriptions.Item>
            <Descriptions.Item label="Successor">{component.successor ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="Upstream supported versions">
              {component.security_supported == null ? (
                "—"
              ) : (
                <Space size={6}>
                  <Tag color={component.security_supported ? "green" : "red"}>
                    {component.security_supported ? "supported" : "unsupported"}
                  </Tag>
                  <Text type="secondary">SECURITY.md row “{component.security_policy_row}”</Text>
                </Space>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="Source file">{component.source_file}</Descriptions.Item>
          </Descriptions>


          <div>
            <Text strong>Structured sources</Text>
            <Text type="secondary"> (endoflife.date / PyPI / npm / GitHub repo + SECURITY.md)</Text>
            <CitationList sources={component.sources.filter((s) => s.origin !== "web")} />
          </div>

          {component.sources.some((s) => s.origin === "web") && (
            <div>
              <Space>
                <Text strong>AI web research</Text>
                <Tag color="volcano">web</Tag>
              </Space>
              <Text type="secondary" style={{ display: "block" }}>
                Pages the AI agent found/fetched to reach its verdict.
              </Text>
              <CitationList sources={component.sources.filter((s) => s.origin === "web")} />
            </div>
          )}
        </Space>
      )}
    </Drawer>
  );
}
