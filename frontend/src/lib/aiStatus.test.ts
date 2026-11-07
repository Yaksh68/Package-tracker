import { describe, expect, it } from "vitest";
import { aiStatusNotice } from "./aiStatus";

describe("aiStatusNotice", () => {
  it("shows nothing when AI is off or went fine", () => {
    expect(aiStatusNotice("off")).toBeNull();
    expect(aiStatusNotice("ok", "2 batch(es) researched")).toBeNull();
    expect(aiStatusNotice(undefined)).toBeNull();
    expect(aiStatusNotice(null)).toBeNull(); // snapshots from before the field existed
  });

  it("labels each problem and keeps the backend's reason in the tooltip", () => {
    const failed = aiStatusNotice("failed", "3 of 3 batch(es) failed: timed out");
    expect(failed?.text).toBe("AI: failed");
    expect(failed?.tooltip).toContain("timed out");
    expect(aiStatusNotice("partial", "1 of 3 batch(es) failed")?.text).toBe("AI: partial");
    expect(aiStatusNotice("quota")?.text).toBe("AI: quota reached");
    expect(aiStatusNotice("no_key")?.tooltip).toContain("Settings");
  });

  it("always says the scan's own results are complete", () => {
    for (const s of ["partial", "failed", "quota", "no_key"] as const) {
      expect(aiStatusNotice(s)?.tooltip).toContain("results are complete");
    }
  });
});
