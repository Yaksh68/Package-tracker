import { describe, expect, it } from "vitest";
import { ApiError, parseError } from "./api";

const res = (status: number, body: string) => new Response(body, { status, statusText: "X" });

describe("api errors", () => {
  it("uses FastAPI's string detail", async () => {
    const e = await parseError(res(409, JSON.stringify({ detail: "a scan is already in progress" })));
    expect(e).toBeInstanceOf(ApiError);
    expect([e.status, e.message]).toEqual([409, "a scan is already in progress"]);
  });
  it("joins 422 field errors", async () => {
    const body = { detail: [{ loc: ["body", "branches"], msg: "at least one branch is required" }] };
    expect((await parseError(res(422, JSON.stringify(body)))).message).toBe("branches: at least one branch is required");
  });
  it("falls back for non-JSON bodies", async () => {
    expect((await parseError(res(502, "<html>bad gateway</html>"))).message).toBe("Server error (502)");
  });
});
