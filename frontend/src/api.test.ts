import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, parseError, setActiveWorkspace, setUnauthorizedHandler } from "./api";

describe("requests", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setActiveWorkspace(null);
    setUnauthorizedHandler(null);
  });

  const stubFetch = (status: number, body: unknown = {}) => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  };
  const headersOf = (m: ReturnType<typeof stubFetch>) =>
    (m.mock.calls[0] as unknown as [string, RequestInit])[1].headers as Record<string, string>;

  it("sends the CSRF header, and the active workspace once one is chosen", async () => {
    const m = stubFetch(200, []);
    await api.listRepos();
    expect(headersOf(m)["X-Requested-With"]).toBe("packages-tracker");
    expect(headersOf(m)["X-Workspace"]).toBeUndefined();

    setActiveWorkspace("ws_team");
    const m2 = stubFetch(200, []);
    await api.listRepos();
    expect(headersOf(m2)["X-Workspace"]).toBe("ws_team");
  });

  it("reports an ended session to the session gate", async () => {
    stubFetch(401, { detail: "sign in first" });
    const ended = vi.fn();
    setUnauthorizedHandler(ended);
    await expect(api.listRepos()).rejects.toMatchObject({ status: 401 });
    expect(ended).toHaveBeenCalledOnce();
  });

  it("doesn't pass on /api/me's own 401: re-checking it would loop forever", async () => {
    const m = stubFetch(401, { detail: "sign in first" });
    const ended = vi.fn();
    setUnauthorizedHandler(ended);
    await expect(api.me()).rejects.toMatchObject({ status: 401 });
    expect(ended).not.toHaveBeenCalled();
    expect(m).toHaveBeenCalledOnce();
  });

  it("doesn't report other errors as an ended session", async () => {
    stubFetch(403, { detail: "this needs the editor role or higher" });
    const ended = vi.fn();
    setUnauthorizedHandler(ended);
    await expect(api.scanNow("r", "main")).rejects.toMatchObject({ status: 403 });
    expect(ended).not.toHaveBeenCalled();
  });

  it("builds the sign-in URL with where to return", () => {
    expect(api.loginUrl()).toBe("/api/auth/github/login");
    expect(api.loginUrl("/invite/tok")).toBe("/api/auth/github/login?next=%2Finvite%2Ftok");
  });
});

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
