// The AI research form on the Settings page: the providers it offers, and
// what's missing or invalid before research can run. Pure functions, so
// they're tested without rendering anything.

export interface ProviderInfo {
  value: string;
  label: string;
  /** It refuses requests without a key. */
  needsKey: boolean;
  /** It has no built-in web search, so research runs Tavily or Exa itself. */
  needsSearch: boolean;
  /** No sensible default: free model names change too often. */
  needsModel: boolean;
  defaultModel?: string;
  /** Where to create a key. */
  keyUrl?: string;
  keyPlaceholder?: string;
  /** Has a free tier, whose terms may let the provider learn from prompts. */
  freeTier?: boolean;
  /** Only on a machine with Claude Code signed in (the local mode). */
  localOnly?: boolean;
  /** Any OpenAI-compatible endpoint, at a base URL the user gives. */
  custom?: boolean;
}

export const AI_PROVIDERS: ProviderInfo[] = [
  {
    value: "claude-cli", label: "Claude Code on this machine", needsKey: false, needsSearch: false,
    needsModel: false, localOnly: true,
  },
  {
    value: "anthropic", label: "Anthropic (Claude)", needsKey: true, needsSearch: false, needsModel: false,
    defaultModel: "claude-haiku-5-5", keyUrl: "https://console.anthropic.com/settings/keys",
    keyPlaceholder: "sk-ant-…",
  },
  {
    value: "openai", label: "OpenAI", needsKey: true, needsSearch: true, needsModel: true,
    keyUrl: "https://platform.openai.com/api-keys", keyPlaceholder: "sk-…",
  },
  {
    value: "gemini", label: "Google Gemini", needsKey: true, needsSearch: true, needsModel: true, freeTier: true,
    keyUrl: "https://aistudio.google.com/apikey", keyPlaceholder: "AIza…",
  },
  {
    value: "groq", label: "Groq", needsKey: true, needsSearch: true, needsModel: true, freeTier: true,
    keyUrl: "https://console.groq.com/keys", keyPlaceholder: "gsk_…",
  },
  {
    value: "openrouter", label: "OpenRouter", needsKey: true, needsSearch: true, needsModel: true, freeTier: true,
    keyUrl: "https://openrouter.ai/settings/keys", keyPlaceholder: "sk-or-…",
  },
  {
    value: "openai-compatible", label: "Another OpenAI-compatible API", needsKey: false, needsSearch: true,
    needsModel: true, custom: true, keyPlaceholder: "Only if the API needs one",
  },
];

export const SEARCH_PROVIDERS = [
  { value: "tavily", label: "Tavily", keyUrl: "https://app.tavily.com/home", keyPlaceholder: "tvly-…" },
  { value: "exa", label: "Exa", keyUrl: "https://dashboard.exa.ai/api-keys", keyPlaceholder: "Your Exa key" },
];

export const providerInfo = (value: string): ProviderInfo | undefined =>
  AI_PROVIDERS.find((p) => p.value === value);

export const providerLabel = (value: string): string =>
  providerInfo(value)?.label ?? SEARCH_PROVIDERS.find((p) => p.value === value)?.label ?? value;

export interface AiFormValues {
  enabled: boolean;
  provider: string;
  model: string;
  baseUrl: string;
  searchProvider: string;
  /** Typed into the form and not saved yet; "" keeps the saved key. */
  aiKey: string;
  searchKey: string;
  removeAiKey?: boolean;
  removeSearchKey?: boolean;
}

/** What's saved now, to judge the form against. */
export interface SavedAi {
  provider: string;
  searchProvider: string;
  aiKey: boolean;
  searchKey: boolean;
}

export type Field = "provider" | "model" | "baseUrl" | "aiKey" | "searchProvider" | "searchKey";

export interface Problem {
  field: Field;
  message: string;
  /** "invalid" blocks saving. "missing" saves fine, but research won't run
   *  until it's filled in (scans still run without it). */
  severity: "invalid" | "missing";
}

/** Everything that stops AI research from running with these values. */
export function aiProblems(v: AiFormValues, saved: SavedAi, mode: "local" | "github"): Problem[] {
  const out: Problem[] = [];
  const missing = (field: Field, message: string) => out.push({ field, message, severity: "missing" });
  const invalid = (field: Field, message: string) => out.push({ field, message, severity: "invalid" });
  const provider = v.provider.trim();

  if (!provider) {
    // the local mode's default is Claude Code with this machine's login
    if (mode === "github") missing("provider", "Choose an AI provider");
    return out;
  }
  const p = providerInfo(provider);
  if (!p) {
    invalid("provider", `Unknown provider "${provider}"`);
    return out;
  }
  if (p.localOnly && mode === "github") {
    invalid("provider", "Claude Code only works on a machine signed in to it: choose a provider and add its key");
    return out;
  }

  // A saved key belongs to the provider it was saved for: the hint can't tell
  // which, so switching providers needs a new key.
  // The local mode can also take keys from backend/.env, which the page can't see.
  const orEnv = mode === "local" ? " (or set it in backend/.env)" : "";
  const typedKey = v.aiKey.trim() !== "";
  const keptKey = saved.aiKey && !v.removeAiKey && provider === saved.provider;
  if (p.needsKey && !typedKey && !keptKey) {
    missing("aiKey", saved.aiKey && !v.removeAiKey && provider !== saved.provider
      ? `Add your ${p.label} API key (the saved key is for ${providerLabel(saved.provider) || "another provider"})`
      : `Add your ${p.label} API key${orEnv}`);
  }

  if (p.custom) {
    const url = v.baseUrl.trim();
    if (!url) missing("baseUrl", "Enter the API's base URL");
    else {
      const problem = baseUrlProblem(url, mode);
      if (problem) invalid("baseUrl", problem);
    }
  }

  if (p.needsModel && !v.model.trim()) missing("model", "Choose a model: Test connection lists them");

  if (p.needsSearch) {
    const search = v.searchProvider.trim();
    const s = SEARCH_PROVIDERS.find((x) => x.value === search);
    if (!s) missing("searchProvider", `${p.label} has no built-in web search: choose Tavily or Exa`);
    else {
      const keptSearch = saved.searchKey && !v.removeSearchKey && search === saved.searchProvider;
      if (!v.searchKey.trim() && !keptSearch) missing("searchKey", `Add your ${s.label} API key${orEnv}`);
    }
  }
  return out;
}

/** Why a base URL won't be accepted, or null. The server checks again,
 *  including where the hostname resolves. */
export function baseUrlProblem(url: string, mode: "local" | "github"): string | null {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return "That isn't a valid URL";
  }
  if (u.protocol !== "https:" && u.protocol !== "http:") return "The base URL must start with https://";
  if (mode === "github") {
    if (u.protocol !== "https:") return "The base URL must start with https://";
    if (u.username || u.password) return "Put the key in the API key field, not in the URL";
    if (/^[\d.]+$/.test(u.hostname) || u.hostname.startsWith("[")) {
      return "Use the API's hostname, not an IP address";
    }
  }
  return null;
}

/** The settings patch for PUT /api/settings: only what changed. */
export function settingsPatch(
  v: AiFormValues,
  current: { use_enrichment_agent: boolean; ai_provider: string; ai_model: string; ai_base_url: string; search_provider: string },
): Record<string, string | boolean> {
  const p = providerInfo(v.provider.trim());
  const next: Record<string, string | boolean> = {
    use_enrichment_agent: v.enabled,
    ai_provider: v.provider.trim(),
    ai_model: v.model.trim(),
    // a preset's URL is built in, and only a custom endpoint uses one
    ai_base_url: p?.custom ? v.baseUrl.trim() : current.ai_base_url,
    search_provider: p?.needsSearch ? v.searchProvider.trim() : current.search_provider,
  };
  return Object.fromEntries(
    Object.entries(next).filter(([k, val]) => val !== current[k as keyof typeof current]),
  );
}
