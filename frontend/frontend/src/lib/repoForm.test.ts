import { describe, expect, it } from "vitest";
import type { Repo } from "../types";
import { buildCreatePayload, buildUpdatePayload, buildUploadMeta, repoIdFromUrl, textToTags } from "./repoForm";

const repo: Repo = {
  id: "app", url: "https://gitlab.com/x/app.git", branches: ["main"], release_date: "2026-11-01",
  interval_days: 1, image_tags: { CORE_TAG: "a:1" }, token_override: "****", created_at: null,
};
const same = { branches: ["main"], release_date: "2026-11-01", interval_days: 1, image_tags_text: "CORE_TAG=a:1" };

describe("repo form payloads", () => {
  it("never sends the masked token back", () => {
    expect(buildUpdatePayload({ ...same, token: "" }, repo)).toEqual({});
    expect(buildUpdatePayload({ ...same, token: "****" }, repo)).toEqual({});
  });
  it("sends a new token, or null to remove the stored one", () => {
    expect(buildUpdatePayload({ ...same, token: " glpat-new " }, repo)).toEqual({ token_override: "glpat-new" });
    expect(buildUpdatePayload({ ...same, remove_token: true }, repo)).toEqual({ token_override: null });
  });
  it("only includes fields that changed (release date triggers a re-flag)", () => {
    expect(buildUpdatePayload({ ...same, interval_days: 7 }, repo)).toEqual({ interval_days: 7 });
    expect(buildUpdatePayload({ ...same, release_date: null }, repo)).toEqual({ release_date: null });
    expect(buildUpdatePayload({ ...same, image_tags_text: "CORE_TAG=a:2" }, repo)).toEqual({ image_tags: { CORE_TAG: "a:2" } });
    expect(buildUpdatePayload({ ...same, ignored_packages: ["enzyme"] }, repo)).toEqual({ ignored_packages: ["enzyme"] });
    expect(buildUpdatePayload({ ...same, ignored_packages: [] }, repo)).toEqual({});
  });
  it("omits an empty token on create", () => {
    const p = buildCreatePayload({ url: " https://x/y ", branches: ["main"], release_date: null, interval_days: 1, token: "" });
    expect(p).toEqual({ url: "https://x/y", branches: ["main"], interval_days: 1, release_date: null, image_tags: {}, ignored_packages: [] });
  });
  it("builds upload metadata; a blank version is left to the server", () => {
    const base = { name: " App UI ", branches: [], release_date: null, interval_days: 2, ignored_packages: ["enzyme"] };
    expect(buildUploadMeta({ ...base, version: "  " })).toEqual({
      name: "App UI", interval_days: 2, release_date: null, image_tags: {}, ignored_packages: ["enzyme"],
    });
    expect(buildUploadMeta({ ...base, version: " 6.1.0 " }).version).toBe("6.1.0");
  });
  it("parses image tags and derives the repo id", () => {
    expect(textToTags("# c\nCORE_TAG=a:1\nUI_TAG=\n")).toEqual({ CORE_TAG: "a:1" });
    expect(() => textToTags("bad line")).toThrow(/VAR=image:tag/);
    expect(repoIdFromUrl("https://gitlab.com/acme/My-Service.git")).toBe("my-service");
  });
});
