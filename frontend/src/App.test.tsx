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
    // a Viewer can't read the audit log, so it isn't in the menu
    expect(text()).not.toContain("Audit log");
  });
});
