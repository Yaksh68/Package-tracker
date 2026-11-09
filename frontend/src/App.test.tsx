// The whole app, rendered against a fake backend: what happens when there's no
// session, when a session ends mid-use, and what a Viewer can't click.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp } from "antd";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { setActiveWorkspace } from "./api";
import { ThemeProvider } from "./theme";
import type { Me } from "./types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
// The first render of the whole antd app in jsdom is slow (several seconds on
// a slow machine), so these tests get far more than the 5 s default.
vi.setConfig({ testTimeout: 60_000 });

const ADMIN_PERMS = ["audit", "delete_repo", "edit", "members", "scan", "secrets", "settings", "view"] as const;
const VIEWER: Me = {
  mode: "github",
  user: {
    id: "u1", github_id: 1, login: "octo", name: null, avatar_url: null, platform_role: "user",
    status: "active", created_at: null, last_seen_at: null,
  },
  workspace_id: "team",
  role: "viewer",
  permissions: ["view"],
  role_permissions: {
    viewer: ["view"],
    editor: ["edit", "scan", "view"],
    admin: [...ADMIN_PERMS],
    owner: [...ADMIN_PERMS, "owner"],
  },
  workspaces: [{ id: "team", name: "Team", personal: false, role: "viewer" }],
};
const INFO = { mode: "github", operator_name: null, contact_email: null };

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function backend(routes: Record<string, () => Response>) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const route = routes[url.split("?")[0]];
    return route ? route() : json(404, { detail: "not found" });
  }));
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

async function render(path: string) {
  container = document.createElement("div");
  document.body.appendChild(container);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root = createRoot(container!);
    root.render(
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <AntApp>
            <MemoryRouter initialEntries={[path]}>
              <App />
            </MemoryRouter>
          </AntApp>
        </ThemeProvider>
      </QueryClientProvider>,
    );
  });
}

async function waitFor(check: () => boolean, ms = 30_000) {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out; the page says: ${document.body.textContent?.slice(0, 300)}`);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
  }
}

const text = () => document.body.textContent ?? "";
const signInLink = () => document.querySelector<HTMLAnchorElement>('a[href^="/api/auth/github/login"]');

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = null;
  vi.unstubAllGlobals();
  setActiveWorkspace(null);
});

describe("sessions", () => {
  it("sends a signed-out visitor to sign in, and back to where they were going", async () => {
    backend({ "/api/me": () => json(401, { detail: "sign in first" }), "/api/info": () => json(200, INFO) });
    await render("/repos/r_1?branch=main");
    await waitFor(() => !!signInLink());
    expect(signInLink()!.getAttribute("href")).toBe("/api/auth/github/login?next=%2Frepos%2Fr_1%3Fbranch%3Dmain");
  });

  it("goes back to sign-in when the session ends mid-use, without bouncing", async () => {
    let signedIn = true;
    backend({
      "/api/me": () => (signedIn ? json(200, VIEWER) : json(401, { detail: "sign in first" })),
      "/api/info": () => json(200, INFO),
      "/api/invites": () => json(200, []),
      "/api/repos": () => {
        signedIn = false; // signed out elsewhere, or the session expired
        return json(401, { detail: "sign in first" });
      },
    });
    await render("/");
    await waitFor(() => text().includes("Sign in with GitHub"));
    // and it stays there
    await act(async () => {
      await new Promise((r) => setTimeout(r, 200));
    });
    expect(text()).toContain("Sign in with GitHub");
  });

  it("explains a sign-up cap", async () => {
    backend({ "/api/me": () => json(401, { detail: "sign in first" }), "/api/info": () => json(200, INFO) });
    await render("/login?error=full");
    await waitFor(() => text().includes("Sign-ups are full right now"));
  });
});

describe("roles", () => {
  it("disables what a Viewer can't do", async () => {
    backend({
      "/api/me": () => json(200, VIEWER),
      "/api/repos": () => json(200, []),
      "/api/invites": () => json(200, []),
    });
    await render("/");
    await waitFor(() => text().includes("No repositories yet."));
    const add = [...document.querySelectorAll("button")].filter((b) => b.textContent?.includes("Add repository"));
    expect(add).toHaveLength(1);
    expect(add[0].disabled).toBe(true);
    // the desktop menu is there, but a Viewer can't read the audit log
    expect(text()).toContain("Settings");
    expect(text()).not.toContain("Audit log");
  });
});

describe("the platform admin's analytics", () => {
  const RANGE = { start: "2026-10-11", end: "2026-11-09", days: 30, previous: { start: "2026-09-11", end: "2026-10-10" } };
  const at = { range: RANGE, generated_at: "2026-11-09T10:00:00Z" };
  const GB = 1024 ** 3;
  const PANELS: Record<string, unknown> = {
    overview: { ...at, tiles: [
      { key: "signups", label: "Sign-ups", value: 12, previous: 9, trend: [{ day: "2026-11-09", value: 2 }] },
      { key: "failed_scans", label: "Failed scans", value: 5, previous: 1, trend: [], better: "lower" },
      { key: "ai_cost_usd", label: "AI spend (estimated)", value: 1.5, previous: 0, trend: [], unit: "usd" },
    ] },
    growth: { ...at, days: [{ day: "2026-11-09", users: 40, signups: 2, active: 7, active_7d: 15, workspaces_created: 1, repos_added: 3 }] },
    activation: { ...at, cohort: 12, steps: [{ key: "signed_up", label: "Signed up", users: 12, pct: 100 }] },
    retention: { ...at, cohorts: [{ week: "2026-11-02", users: 4, weeks: [100, 50] }] },
    scans: { ...at, totals: { runs: 30, done: 25, degraded: 3, error: 2, success_pct: 93.3 },
      days: [{ day: "2026-11-09", done: 5, degraded: 1, error: 0 }], triggers: [{ trigger: "scheduled", runs: 20 }],
      stages: [{ stage: "clone", runs: 30, median: 4.2, p90: 9 }], duration: { median: 61, p90: 200 },
      queue_wait: { median: 2, p90: 30 }, failures: [], upstream_errors: [] },
    content: { ...at, branches: 10, ecosystems: [{ ecosystem: "pypi", packages: 4321, repos: 8 }],
      flags: [{ status: "ALREADY_EOL", packages: 12 }], common_eol: [], common_min_workspaces: 5 },
    ai: { ...at, totals: { runs: 0, tokens_in: 0, tokens_out: 0, searches: 0, cost_usd: 0, scans: 30, unpriced_runs: 0 },
      statuses: [{ status: "off", runs: 30 }], providers: [], days: [], errors: [], results: [] },
    system: { ...at, workers: 2, per: "day", samples: [], now: {
      disk_used_bytes: 5 * GB, disk_total_bytes: 45 * GB, db_bytes: 20 * 1024 ** 2, uploads_bytes: 0,
      rss_bytes: 0.5 * GB, mem_total_bytes: 24 * GB, queue_waiting: 0, scans_running: 1 } },
    workspaces: { ...at, workspaces: [{ id: "w1", name: "Acme team", personal: false, created_at: "2026-10-01T00:00:00Z",
      members: 3, active_members: 2, repos: 4, scans: 12, failed_scans: 1, ai_cost_usd: 0.5,
      last_scan_at: "2026-11-09T08:00:00Z", paused: false }] },
  };
  const ROOT: Me = {
    ...VIEWER,
    user: { ...VIEWER.user, login: "root", platform_role: "admin" },
    role: "owner",
    permissions: [...ADMIN_PERMS, "owner"],
    workspaces: [{ id: "team", name: "Team", personal: false, role: "owner" }],
  };

  it("shows the headline numbers, and each chart's values in its table view", async () => {
    backend({
      "/api/me": () => json(200, ROOT),
      "/api/invites": () => json(200, []),
      ...Object.fromEntries(Object.entries(PANELS).map(([name, body]) => [`/api/admin/analytics/${name}`, () => json(200, body)])),
    });
    await render("/admin/analytics");
    await waitFor(() => text().includes("Acme team") && text().includes("Sign-ups"));
    expect(text()).toContain("+3 vs the 30 days before"); // 12 sign-ups, 9 the period before
    expect(text()).toContain("+4 vs the 30 days before"); // 5 failed scans, 1 the period before
    expect(text()).toContain("$1.50");
    expect(text()).toContain("5 GB of 45 GB");
    expect(text()).toContain("3 (2 active)");

    // a chart's numbers are one click away, without hovering
    const card = [...document.querySelectorAll(".ant-card")].find(
      (c) => c.querySelector(".ant-card-head-title")?.textContent === "Packages by ecosystem")!;
    expect(card.textContent).not.toContain("4,321");
    const tableSwitch = [...card.querySelectorAll<HTMLElement>(".ant-segmented-item")].find((e) => e.textContent === "Table")!;
    await act(async () => {
      tableSwitch.click();
    });
    await waitFor(() => card.textContent!.includes("4321") && card.textContent!.includes("pypi"));
  });

  it("isn't there for anyone else", async () => {
    backend({ "/api/me": () => json(200, VIEWER), "/api/invites": () => json(200, []) });
    await render("/admin/analytics");
    await waitFor(() => text().includes("Page not found"));
  });
});

describe("phones", () => {
  afterEach(() => {
    window.__screenWidth = undefined;
  });

  it("puts the menu and the workspace switcher in a drawer", async () => {
    window.__screenWidth = 390;
    backend({
      "/api/me": () => json(200, VIEWER),
      "/api/repos": () => json(200, []),
      "/api/invites": () => json(200, []),
    });
    await render("/");
    await waitFor(() => text().includes("No repositories yet."));
    const menuButton = document.querySelector<HTMLButtonElement>('button[aria-label="Menu"]');
    expect(menuButton).not.toBeNull();
    expect(document.querySelector(".ant-menu-horizontal")).toBeNull(); // no menu bar on a phone
    await act(async () => {
      menuButton!.click();
    });
    await waitFor(() => !!document.querySelector(".ant-drawer .ant-menu-inline"));
    const drawer = document.querySelector(".ant-drawer")!;
    expect(drawer.textContent).toContain("Repositories");
    expect(drawer.textContent).toContain("Settings");
    expect(drawer.querySelector('[aria-label="Workspace"]')).not.toBeNull();
  });
});
