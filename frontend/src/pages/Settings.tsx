import { CheckCircleFilled, LinkOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App, AutoComplete, Button, Card, Descriptions, Form, Input, InputNumber, Progress, Result, Select, Skeleton,
  Space, Switch, Typography,
} from "antd";
import { useEffect, useState, type ReactNode } from "react";
import { api, errorMessage } from "../api";
import { fmtRelative } from "../format";
import {
  AI_PROVIDERS, SEARCH_PROVIDERS, aiProblems, providerInfo, settingsPatch, type AiFormValues, type Field,
} from "../lib/aiSettings";
import { useSession } from "../session";
import type { AiTestResult, SecretHint, WorkspaceSettings } from "../types";

/** A write-only key field: what's saved shows only as a hint, never the value. */
function SecretInput(props: {
  hint?: SecretHint;
  value: string;
  onChange: (v: string) => void;
  removed?: boolean;
  onRemovedChange?: (v: boolean) => void;
  placeholder?: string;
  disabled?: boolean;
  id?: string;
}) {
  const { hint, value, onChange, removed, onRemovedChange, placeholder, disabled, id } = props;
  const saved = !!hint?.set;
  return (
    <Space direction="vertical" size={2} style={{ width: "100%" }}>
      <Input.Password
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={saved && !removed ? "Type a new key to replace the saved one" : placeholder}
        disabled={disabled || removed}
        autoComplete="new-password"
        spellCheck={false}
      />
      {saved && (
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {removed
            ? "The saved key will be removed when you save."
            : `Saved${hint?.hint ? ` (${hint.hint})` : ""}${hint?.updated_at ? `, ${fmtRelative(hint.updated_at)}` : ""}. Leave blank to keep it.`}
          {!disabled && onRemovedChange && (
            <Button type="link" size="small" onClick={() => onRemovedChange(!removed)}>
              {removed ? "Keep it" : "Remove"}
            </Button>
          )}
        </Typography.Text>
      )}
    </Space>
  );
}

function KeyLink({ href, children }: { href?: string; children: ReactNode }) {
  if (!href) return null;
  return (
    <a href={href} target="_blank" rel="noreferrer">
      {children} <LinkOutlined />
    </a>
  );
}

function fromSettings(s: WorkspaceSettings): AiFormValues {
  return {
    enabled: s.use_enrichment_agent,
    provider: s.ai_provider,
    model: s.ai_model,
    baseUrl: s.ai_base_url,
    searchProvider: s.search_provider,
    reuseDays: s.ai_reuse_days ?? 7,
    aiKey: "",
    searchKey: "",
    removeAiKey: false,
    removeSearchKey: false,
  };
}

function AiResearchCard({ settings, editable }: { settings: WorkspaceSettings; editable: boolean }) {
  const { me } = useSession();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [v, setV] = useState<AiFormValues>(() => fromSettings(settings));
  useEffect(() => setV(fromSettings(settings)), [settings]);
  const set = (patch: Partial<AiFormValues>) => setV((old) => ({ ...old, ...patch }));

  const p = providerInfo(v.provider);
  const providers = AI_PROVIDERS.filter((x) => !x.localOnly || me.mode === "local");
  const search = SEARCH_PROVIDERS.find((s) => s.value === v.searchProvider);
  const problems = aiProblems(
    v,
    {
      provider: settings.ai_provider,
      searchProvider: settings.search_provider,
      aiKey: settings.secrets.ai_api_key.set,
      searchKey: settings.secrets.search_api_key.set,
    },
    me.mode,
  );
  const invalid = problems.some((x) => x.severity === "invalid");
  // Invalid values always show. Missing ones only matter once research is on,
  // so they don't nag while it's off.
  const problemOf = (f: Field) =>
    problems.find((x) => x.field === f && (x.severity === "invalid" || v.enabled));
  const status = (f: Field) => {
    const x = problemOf(f);
    return x ? (x.severity === "invalid" ? "error" : "warning") : undefined;
  };

  // A test result belongs to the provider, URL and key it ran with.
  const [test, setTest] = useState<AiTestResult | null>(null);
  useEffect(() => setTest(null), [v.provider, v.baseUrl, v.aiKey]);
  const testMut = useMutation({
    mutationFn: () =>
      api.testAi({
        provider: v.provider,
        base_url: p?.custom ? v.baseUrl.trim() : undefined,
        api_key: v.aiKey.trim() || undefined,
      }),
    onSuccess: setTest,
    onError: (e) => setTest({ ok: false, models: [], message: errorMessage(e) }),
  });

  const saveMut = useMutation({
    mutationFn: async () => {
      if (v.aiKey.trim()) await api.setSecret("ai_api_key", v.aiKey.trim());
      else if (v.removeAiKey) await api.deleteSecret("ai_api_key");
      if (v.searchKey.trim()) await api.setSecret("search_api_key", v.searchKey.trim());
      else if (v.removeSearchKey) await api.deleteSecret("search_api_key");
      const patch = settingsPatch(v, settings);
      if (Object.keys(patch).length) await api.updateSettings(patch);
      return api.getSettings();
    },
    onSuccess: (s) => {
      qc.setQueryData(["settings"], s);
      message.success("Saved. It applies from the next scan.");
    },
    onError: (e) => message.error(errorMessage(e)),
  });

  const ready = problems.length === 0 && !!(v.provider || me.mode === "local");
  const models = test?.ok ? test.models : [];

  return (
    <Card title="AI research" style={{ marginBottom: 16 }}>
      <Typography.Paragraph type="secondary">
        Every scan runs on structured sources (package registries, endoflife.date and the projects' own repositories).
        AI research only looks into the packages they can't answer, and it's optional: a scan never fails because of it.
      </Typography.Paragraph>

      <Form layout="vertical" disabled={!editable} requiredMark={false}>
        <Form.Item>
          <Space>
            <Switch id="ai-enabled" checked={v.enabled} onChange={(enabled) => set({ enabled })} />
            <label htmlFor="ai-enabled">Research ambiguous packages with AI</label>
          </Space>
        </Form.Item>

        {!v.enabled && !ready && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message="Add a key to enable AI research"
            description={editable
              ? "Choose a provider and add your own API key below. Several providers have free tiers."
              : "An Admin or Owner of this workspace can choose a provider and add a key."}
          />
        )}
        {v.enabled && problems.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="AI research is on, but it won't run until this is done:"
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {problems.map((x) => <li key={x.field}>{x.message}</li>)}
              </ul>
            }
          />
        )}

        <Form.Item label="Provider" validateStatus={status("provider")} help={problemOf("provider")?.message}>
          <Select
            value={v.provider || undefined}
            placeholder={me.mode === "local" ? "Claude Code on this machine (the default)" : "Choose a provider"}
            onChange={(provider) => set({ provider, model: "" })}
            options={providers.map((x) => ({
              value: x.value,
              label: x.freeTier ? `${x.label} (has a free tier)` : x.label,
            }))}
          />
        </Form.Item>

        {p?.freeTier && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message="About free tiers"
            description="Some free tiers (Gemini's, for example) let the provider use what you send to improve its products. This app's prompts contain only package names and versions."
          />
        )}

        {p?.custom && (
          <Form.Item
            label="Base URL"
            validateStatus={status("baseUrl")}
            help={problemOf("baseUrl")?.message}
            extra="The API's OpenAI-compatible endpoint, e.g. https://api.mistral.ai/v1"
          >
            <Input value={v.baseUrl} onChange={(e) => set({ baseUrl: e.target.value })} placeholder="https://…/v1" />
          </Form.Item>
        )}

        {p && !p.localOnly && (
          <Form.Item
            label="API key"
            htmlFor="ai-key"
            validateStatus={status("aiKey")}
            help={problemOf("aiKey")?.message}
            extra={<KeyLink href={p.keyUrl}>Get a {p.label} key</KeyLink>}
          >
            <SecretInput
              id="ai-key"
              hint={settings.secrets.ai_api_key}
              value={v.aiKey}
              onChange={(aiKey) => set({ aiKey })}
              removed={v.removeAiKey}
              onRemovedChange={(removeAiKey) => set({ removeAiKey })}
              placeholder={p.keyPlaceholder}
              disabled={!editable}
            />
          </Form.Item>
        )}

        {p && (
          <Form.Item>
            <Space wrap>
              <Button onClick={() => testMut.mutate()} loading={testMut.isPending} disabled={!editable}>
                Test connection
              </Button>
              {test?.ok && (
                <Typography.Text type="success">
                  <CheckCircleFilled />{" "}
                  {p.localOnly
                    ? "Claude Code is installed on this machine."
                    : `Connected. ${test.models.length} model${test.models.length === 1 ? "" : "s"} available.`}
                </Typography.Text>
              )}
            </Space>
            {test && !test.ok && (
              <Alert type="error" showIcon style={{ marginTop: 8 }} message={test.message ?? "The test failed"} />
            )}
          </Form.Item>
        )}

        {p && !p.localOnly && (
          <Form.Item
            label="Model"
            validateStatus={status("model")}
            help={problemOf("model")?.message}
            extra={p.defaultModel && !v.model ? `Leave blank for ${p.defaultModel}.` : undefined}
          >
            <AutoComplete
              value={v.model}
              onChange={(model) => set({ model })}
              placeholder={p.defaultModel ?? "Use Test connection to list the models"}
              options={models.map((m) => ({ value: m }))}
              filterOption={(input, o) => String(o?.value ?? "").toLowerCase().includes(input.toLowerCase())}
            />
          </Form.Item>
        )}

        {p?.needsSearch && (
          <>
            <Form.Item
              label="Web search"
              validateStatus={status("searchProvider")}
              help={problemOf("searchProvider")?.message}
              extra="This provider has no built-in web search, so research searches through your own key."
            >
              <Select
                value={v.searchProvider || undefined}
                placeholder="Choose a search provider"
                onChange={(searchProvider) => set({ searchProvider })}
                options={SEARCH_PROVIDERS.map((s) => ({ value: s.value, label: s.label }))}
              />
            </Form.Item>
            {search && (
              <Form.Item
                label={`${search.label} API key`}
                htmlFor="search-key"
                validateStatus={status("searchKey")}
                help={problemOf("searchKey")?.message}
                extra={<KeyLink href={search.keyUrl}>Get a {search.label} key</KeyLink>}
              >
                <SecretInput
                  id="search-key"
                  hint={settings.secrets.search_api_key}
                  value={v.searchKey}
                  onChange={(searchKey) => set({ searchKey })}
                  removed={v.removeSearchKey}
                  onRemovedChange={(removeSearchKey) => set({ removeSearchKey })}
                  placeholder={search.keyPlaceholder}
                  disabled={!editable}
                />
              </Form.Item>
            )}
          </>
        )}

        <Form.Item
          label="Reuse findings for"
          extra="A package version the AI already researched in this workspace within this many days isn't researched again, which saves your quota. 0 asks the AI every time."
        >
          <Space>
            <InputNumber min={0} max={90} precision={0} value={v.reuseDays} style={{ width: 90 }}
              onChange={(n) => set({ reuseDays: n ?? 0 })} aria-label="Reuse findings for (days)" />
            <span>days</span>
          </Space>
        </Form.Item>

        <Button type="primary" onClick={() => saveMut.mutate()} loading={saveMut.isPending} disabled={!editable || invalid}>
          Save
        </Button>
      </Form>
    </Card>
  );
}

function GitTokensCard({ settings, editable }: { settings: WorkspaceSettings; editable: boolean }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [github, setGithub] = useState("");
  const [gitlab, setGitlab] = useState("");
  const [removeGithub, setRemoveGithub] = useState(false);
  const [removeGitlab, setRemoveGitlab] = useState(false);
  useEffect(() => {
    setGithub("");
    setGitlab("");
    setRemoveGithub(false);
    setRemoveGitlab(false);
  }, [settings]);

  const saveMut = useMutation({
    mutationFn: async () => {
      if (github.trim()) await api.setSecret("github_token", github.trim());
      else if (removeGithub) await api.deleteSecret("github_token");
      if (gitlab.trim()) await api.setSecret("gitlab_token", gitlab.trim());
      else if (removeGitlab) await api.deleteSecret("gitlab_token");
      return api.getSettings();
    },
    onSuccess: (s) => {
      qc.setQueryData(["settings"], s);
      message.success("Saved. It applies from the next scan.");
    },
    onError: (e) => message.error(errorMessage(e)),
  });
  const changed = !!(github.trim() || gitlab.trim() || removeGithub || removeGitlab);

  return (
    <Card title="Repository access">
      <Typography.Paragraph type="secondary">
        Read-only tokens for cloning this workspace's private repositories. A repository's own token, set in its edit
        form, takes precedence. The GitHub token also raises GitHub's rate limit for package lookups.
      </Typography.Paragraph>
      <Form layout="vertical" disabled={!editable}>
        <Form.Item label="GitHub token" htmlFor="github-token">
          <SecretInput
            id="github-token"
            hint={settings.secrets.github_token}
            value={github}
            onChange={setGithub}
            removed={removeGithub}
            onRemovedChange={setRemoveGithub}
            placeholder="github_pat_… (read-only access to contents)"
            disabled={!editable}
          />
        </Form.Item>
        <Form.Item label="GitLab token" htmlFor="gitlab-token">
          <SecretInput
            id="gitlab-token"
            hint={settings.secrets.gitlab_token}
            value={gitlab}
            onChange={setGitlab}
            removed={removeGitlab}
            onRemovedChange={setRemoveGitlab}
            placeholder="glpat-… (read_repository)"
            disabled={!editable}
          />
        </Form.Item>
        <Button type="primary" onClick={() => saveMut.mutate()} loading={saveMut.isPending} disabled={!editable || !changed}>
          Save
        </Button>
      </Form>
    </Card>
  );
}

// limits a workspace uses up, and the fixed ones (per repo, per scan, per zip)
const METERED = ["repos_per_workspace", "members_per_workspace", "upload_storage_mb", "manual_scans_per_hour"];
const FIXED = ["branches_per_repo", "min_interval_days", "zip_mb", "unpacked_mb", "snapshots_per_branch"];

function Meter({ label, used, limit, unit = "" }: { label: string; used: number; limit: number; unit?: string }) {
  const pct = Math.min(100, Math.round((used / limit) * 100));
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <Typography.Text>{label}</Typography.Text>
        <Typography.Text type={used >= limit ? "danger" : "secondary"}>
          {used}{unit} of {limit}{unit}
        </Typography.Text>
      </div>
      <Progress percent={pct} showInfo={false} size="small" status={used >= limit ? "exception" : "normal"} />
    </div>
  );
}

/** What the workspace uses against its limits (public mode). */
function UsageCard() {
  const { isPublic } = useSession();
  const q = useQuery({ queryKey: ["limits"], queryFn: api.limits, enabled: isPublic });
  const d = q.data;
  if (!isPublic || !d?.enabled || !d.limits || !d.usage || !d.labels) return null;
  const { limits, usage, labels } = d;
  return (
    <Card title="Usage and limits" style={{ marginTop: 16 }}>
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        {METERED.map((name) => (
          <Meter key={name} label={labels[name]} used={usage[name] ?? 0} limit={limits[name]}
            unit={name.endsWith("_mb") ? " MB" : ""} />
        ))}
      </Space>
      <Descriptions
        size="small"
        column={1}
        style={{ marginTop: 16 }}
        items={FIXED.map((name) => ({ key: name, label: labels[name], children: limits[name] }))}
      />
      <Typography.Paragraph type="secondary" style={{ margin: "12px 0 0" }}>
        Whoever runs this service sets these limits.
      </Typography.Paragraph>
    </Card>
  );
}

export function SettingsPage() {
  const { can, why, workspace, isPublic } = useSession();
  const q = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const editable = can("settings") && can("secrets");

  return (
    <div style={{ maxWidth: 760 }}>
      <Typography.Title level={3} style={{ marginTop: 0 }}>Settings</Typography.Title>
      {isPublic && workspace && (
        <Typography.Paragraph type="secondary">
          For the <Typography.Text strong>{workspace.name}</Typography.Text> workspace. Keys are stored encrypted and
          are never shown again after saving.
        </Typography.Paragraph>
      )}
      {!editable && (
        <Alert type="info" showIcon style={{ marginBottom: 16 }} message="Read only" description={why("settings")} />
      )}
      {q.isError ? (
        <Result status="error" title="Couldn't load the settings" subTitle={errorMessage(q.error)}
          extra={<Button onClick={() => q.refetch()}>Retry</Button>} />
      ) : !q.data ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : (
        <>
          <AiResearchCard settings={q.data} editable={editable} />
          <GitTokensCard settings={q.data} editable={editable} />
          <UsageCard />
        </>
      )}
    </div>
  );
}
