import { describe, expect, it } from "vitest";
import { chartTheme } from "./chartTheme";
import {
  byteTicks, change, fmtBytes, fmtCount, fmtDay, fmtSecs, fmtUsd, lastDays, rangeDays, toCsv,
} from "./analyticsFormat";

describe("numbers", () => {
  it("counts in full below 10,000 and compact above", () => {
    expect(fmtCount(1284)).toBe("1,284");
    expect(fmtCount(12_900)).toBe("12.9K");
    expect(fmtCount(3_400_000)).toBe("3.4M");
    expect(fmtCount(null)).toBe("—");
  });

  it("money, bytes and durations", () => {
    expect(fmtUsd(0)).toBe("$0");
    expect(fmtUsd(0.0021)).toBe("$0.0021");
    expect(fmtUsd(1.5)).toBe("$1.50");
    expect(fmtUsd(12345)).toBe("$12,345");
    expect(fmtBytes(1536)).toBe("1.5 KB");
    expect(fmtBytes(5 * 1024 ** 3)).toBe("5 GB");
    expect(fmtSecs(4.25)).toBe("4.3 s");
    expect(fmtSecs(42)).toBe("42 s");
    expect(fmtSecs(180)).toBe("3 min");
    expect(fmtSecs(5400)).toBe("1.5 h");
  });

  it("days are UTC days", () => {
    expect(fmtDay("2026-11-09")).toBe("9 Nov");
  });
});

describe("change since the previous period", () => {
  it("is good news when it moves the right way", () => {
    expect(change(5, 3)).toEqual({ text: "+2", tone: "good", direction: "up" });
    expect(change(1, 3, { better: "lower" })).toEqual({ text: "−2", tone: "good", direction: "down" });
    expect(change(4, 3, { better: "lower" }).tone).toBe("bad");
    expect(change(0.75, 0, { better: "none", format: fmtUsd })).toEqual(
      { text: "+$0.75", tone: "neutral", direction: "up" });
    expect(change(3, 3)).toEqual({ text: "no change", tone: "neutral", direction: "flat" });
  });
});

describe("CSV export", () => {
  it("quotes what needs quoting and defuses formulas", () => {
    const csv = toCsv(
      [{ key: "name", title: "Name" }, { key: "n", title: "Count" }, { key: "x", title: "Upper", value: (r) => r.name.toUpperCase() }],
      [{ name: 'say "hi", ok', n: 3 }, { name: "=HYPERLINK(1)", n: -2 }],
    );
    expect(csv).toBe(
      'Name,Count,Upper\r\n"say ""hi"", ok",3,"SAY ""HI"", OK"\r\n\'=HYPERLINK(1),-2,\'=HYPERLINK(1)\r\n');
  });
});

describe("date ranges", () => {
  it("the last N days end today, in UTC", () => {
    const now = new Date("2026-11-09T22:30:00Z");
    expect(lastDays(7, now)).toEqual({ start: "2026-11-03", end: "2026-11-09" });
    expect(rangeDays(lastDays(30, now))).toBe(30);
  });
});

describe("byte axes", () => {
  it("tick in round steps of the unit shown", () => {
    const GB = 1024 ** 3;
    expect(byteTicks(375.5 * GB)).toEqual([0, 100, 200, 300, 400].map((n) => n * GB));
    expect(byteTicks(45 * 1024 ** 2)).toEqual([0, 10, 20, 30, 40, 50].map((n) => n * 1024 ** 2));
    expect(byteTicks(0)).toEqual([0]);
  });
});

describe("chart theme", () => {
  it("retention cells recede towards the surface near zero, and text stays readable", () => {
    const light = chartTheme("light");
    const dark = chartTheme("dark");
    expect(light.heat(0).background).toBe("#cde2fb");
    expect(light.heat(0).color).toBe("#0b0b0b");
    expect(light.heat(1).color).toBe("#ffffff");
    expect(dark.heat(0).background).toBe("#0d366b");
    expect(dark.heat(0).color).toBe("#ffffff");
  });
});
