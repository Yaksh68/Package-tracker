import { describe, expect, it } from "vitest";
import { ordinal, scanStateText } from "./ScanStatusPill";

describe("scan state wording", () => {
  it("says where a queued scan is in line", () => {
    expect(scanStateText("queued", "", 1)).toBe("Queued · next");
    expect(scanStateText("queued", "", 3)).toBe("Queued · 3rd in line");
    expect(scanStateText("queued")).toBe("Queued");
  });

  it("shows the phase while running", () => {
    expect(scanStateText("running", "enrich")).toBe("Scanning · enrich");
    expect(scanStateText(undefined)).toBe("Idle");
  });

  it("writes ordinals", () => {
    expect([1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111].map(ordinal)).toEqual(
      ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st", "111th"],
    );
  });
});
