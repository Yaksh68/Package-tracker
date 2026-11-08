import { describe, expect, it } from "vitest";
import { loginPath, safeNext } from "./session";

describe("returning after sign-in", () => {
  it("keeps paths on this site", () => {
    expect(safeNext("/repos/r_1?branch=main")).toBe("/repos/r_1?branch=main");
    expect(safeNext("/invite/abc")).toBe("/invite/abc");
  });

  it.each([null, "", "//evil.example", "/\\evil.example", "https://evil.example", "/api/me", "/login", "/a b"])(
    "refuses %s",
    (next) => expect(safeNext(next)).toBe("/"),
  );

  it("builds the sign-in path", () => {
    expect(loginPath("/")).toBe("/login");
    expect(loginPath("/settings")).toBe("/login?next=%2Fsettings");
    expect(loginPath("/login?next=%2Fx")).toBe("/login");
  });
});
