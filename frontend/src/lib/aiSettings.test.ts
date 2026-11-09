import { describe, expect, it } from "vitest";
import { aiProblems, baseUrlProblem, settingsPatch, type AiFormValues, type SavedAi } from "./aiSettings";

const form = (v: Partial<AiFormValues>): AiFormValues => ({
  enabled: true, provider: "", model: "", baseUrl: "", searchProvider: "", reuseDays: 7, aiKey: "", searchKey: "", ...v,
});
const nothingSaved: SavedAi = { provider: "", searchProvider: "", aiKey: false, searchKey: false };
const fields = (v: AiFormValues, saved = nothingSaved, mode: "local" | "github" = "github") =>
  aiProblems(v, saved, mode).map((p) => `${p.field}:${p.severity}`);

describe("AI settings validation", () => {
  it("needs a provider in the public mode, but not locally (Claude Code is the default)", () => {
    expect(fields(form({}))).toEqual(["provider:missing"]);
    expect(fields(form({}), nothingSaved, "local")).toEqual([]);
  });

  it("refuses Claude Code in the public mode", () => {
    expect(fields(form({ provider: "claude-cli" }))).toEqual(["provider:invalid"]);
    expect(fields(form({ provider: "claude-cli" }), nothingSaved, "local")).toEqual([]);
  });

  it("only needs a key for Anthropic, which has search built in and a default model", () => {
    expect(fields(form({ provider: "anthropic" }))).toEqual(["aiKey:missing"]);
    expect(fields(form({ provider: "anthropic", aiKey: "sk-ant-x" }))).toEqual([]);
  });

  it("needs a key, a model and a search provider with its key for the OpenAI-compatible presets", () => {
    expect(fields(form({ provider: "groq" }))).toEqual(["aiKey:missing", "model:missing", "searchProvider:missing"]);
    expect(fields(form({ provider: "groq", aiKey: "gsk_x", model: "m", searchProvider: "tavily" })))
      .toEqual(["searchKey:missing"]);
    expect(fields(form({ provider: "groq", aiKey: "gsk_x", model: "m", searchProvider: "tavily", searchKey: "tvly-x" })))
      .toEqual([]);
  });

  it("keeps a saved key only for the provider it was saved for", () => {
    const saved: SavedAi = { provider: "groq", searchProvider: "tavily", aiKey: true, searchKey: true };
    const groq = form({ provider: "groq", model: "m", searchProvider: "tavily" });
    expect(fields(groq, saved)).toEqual([]);
    const gemini = aiProblems({ ...groq, provider: "gemini" }, saved, "github");
    expect(gemini.map((p) => p.field)).toEqual(["aiKey"]);
    expect(gemini[0].message).toContain("the saved key is for Groq");
    expect(fields({ ...groq, removeAiKey: true }, saved)).toEqual(["aiKey:missing"]);
    expect(fields({ ...groq, searchProvider: "exa" }, saved)).toEqual(["searchKey:missing"]);
  });

  it("checks a custom endpoint's base URL", () => {
    const custom = form({ provider: "openai-compatible", model: "m", searchProvider: "exa", searchKey: "k" });
    expect(fields(custom)).toEqual(["baseUrl:missing"]);
    expect(fields({ ...custom, baseUrl: "https://api.mistral.ai/v1" })).toEqual([]); // no key needed
    expect(fields({ ...custom, baseUrl: "http://localhost:11434/v1" })).toEqual(["baseUrl:invalid"]);
    expect(fields({ ...custom, baseUrl: "http://localhost:11434/v1" }, nothingSaved, "local")).toEqual([]);
  });

  it("explains what's wrong with a base URL", () => {
    expect(baseUrlProblem("not a url", "github")).toBe("That isn't a valid URL");
    expect(baseUrlProblem("ftp://x.example", "local")).toMatch(/https/);
    expect(baseUrlProblem("https://user:pw@x.example/v1", "github")).toMatch(/API key field/);
    expect(baseUrlProblem("https://10.0.0.5/v1", "github")).toMatch(/hostname/);
    expect(baseUrlProblem("https://[::1]/v1", "github")).toMatch(/hostname/);
    expect(baseUrlProblem("https://api.example.com/v1", "github")).toBeNull();
  });
});

describe("the settings patch", () => {
  const current = { use_enrichment_agent: false, ai_provider: "groq", ai_model: "a", ai_base_url: "", search_provider: "tavily", ai_reuse_days: 7 };

  it("sends only what changed", () => {
    const v = form({ enabled: true, provider: "groq", model: "a", searchProvider: "tavily" });
    expect(settingsPatch(v, current)).toEqual({ use_enrichment_agent: true });
  });

  it("leaves the base URL and search provider alone for providers that don't use them", () => {
    const v = form({ enabled: false, provider: "anthropic", model: "", baseUrl: "https://ignored", searchProvider: "" });
    expect(settingsPatch(v, current)).toEqual({ ai_provider: "anthropic", ai_model: "" });
  });
});
