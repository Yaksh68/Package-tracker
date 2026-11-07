import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Space, Switch, Tooltip, Typography } from "antd";
import { api, errorMessage } from "../api";

export function SettingsToggle() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const { data, isError } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });

  const mut = useMutation({
    mutationFn: (v: boolean) => api.updateSettings({ use_enrichment_agent: v }),
    onSuccess: (s) => {
      qc.setQueryData(["settings"], s);
      message.success(`AI research ${s.use_enrichment_agent ? "on" : "off"} — applies from the next scan`);
    },
    onError: (e) => message.error(errorMessage(e)),
  });

  return (
    <Tooltip title="Researches packages with no clear end-of-life answer on the web. Applies to the next scan of every repository.">
      <Space size={8}>
        <Typography.Text type="secondary" id="ai-research-label">
          AI research
        </Typography.Text>
        <Switch
          size="small"
          aria-labelledby="ai-research-label"
          checked={!!data?.use_enrichment_agent}
          disabled={isError}
          loading={mut.isPending}
          onChange={(v) => mut.mutate(v)}
        />
      </Space>
    </Tooltip>
  );
}
