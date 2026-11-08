// Who's signed in, and in which workspace. Everything behind SessionGate can
// call useSession(); pages before sign-in (login, privacy, terms) can't.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Result, Spin } from "antd";
import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import {
  api,
  errorMessage,
  getActiveWorkspace,
  isNotFound,
  isUnauthorized,
  setActiveWorkspace,
  setUnauthorizedHandler,
} from "./api";
import { can, needsRole, roleIn } from "./lib/permissions";
import type { Me, Perm, Workspace } from "./types";

export interface Session {
  me: Me;
  /** The public mode: GitHub sign-in, team workspaces, members. */
  isPublic: boolean;
  isPlatformAdmin: boolean;
  /** The active workspace. */
  workspace: Workspace | undefined;
  /** May the user do this, in the active workspace or in `workspaceId`? */
  can: (perm: Perm, workspaceId?: string | null) => boolean;
  /** The tooltip for a control the user can't use; undefined when they can. */
  why: (perm: Perm, workspaceId?: string | null) => string | undefined;
  switchWorkspace: (id: string, then?: string) => void;
  signOut: () => Promise<void>;
}

const Ctx = createContext<Session | null>(null);

export function useSession(): Session {
  const s = useContext(Ctx);
  if (!s) throw new Error("useSession outside SessionGate");
  return s;
}

export function FullPageSpin() {
  return (
    <div style={{ display: "grid", placeItems: "center", minHeight: "60vh" }}>
      <Spin size="large" />
    </div>
  );
}

/** Where to sign in so that the user comes back to `path` afterwards. */
export function loginPath(path: string): string {
  return path && path !== "/" && !path.startsWith("/login") ? `/login?${new URLSearchParams({ next: path })}` : "/login";
}

/** A page on this site to go to after signing in; anything else is "/". The
 *  server checks its copy of `next` the same way. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || /[\\\s]/.test(next)) return "/";
  if (next.startsWith("/api/") || next.startsWith("/login")) return "/";
  return next;
}

export function SessionGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const meQuery = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false, staleTime: 60_000 });
  const staleWorkspace = isNotFound(meQuery.error) && !!getActiveWorkspace();

  // Any 401 means the session ended: re-check, and the gate shows sign-in.
  useEffect(() => {
    setUnauthorizedHandler(() => qc.invalidateQueries({ queryKey: ["me"] }));
    return () => setUnauthorizedHandler(null);
  }, [qc]);

  // A remembered workspace the user has since left (or that was deleted):
  // forget it, and the server falls back to the personal workspace.
  useEffect(() => {
    if (staleWorkspace) {
      setActiveWorkspace(null);
      qc.clear();
    }
  }, [staleWorkspace, qc]);

  const switchWorkspace = useCallback(
    (id: string, then = "/") => {
      setActiveWorkspace(id);
      qc.clear(); // every cached query belongs to the old workspace
      navigate(then);
    },
    [qc, navigate],
  );

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setActiveWorkspace(null);
      qc.clear();
      navigate("/login");
    }
  }, [qc, navigate]);

  const me = meQuery.data;
  const session = useMemo<Session | null>(() => {
    if (!me) return null;
    return {
      me,
      isPublic: me.mode === "github",
      isPlatformAdmin: me.user.platform_role === "admin",
      workspace: me.workspaces.find((w) => w.id === me.workspace_id),
      can: (perm, ws) => can(me.role_permissions, roleIn(me, ws), perm),
      why: (perm, ws) => needsRole(me.role_permissions, roleIn(me, ws), perm),
      switchWorkspace,
      signOut,
    };
  }, [me, switchWorkspace, signOut]);

  if (isUnauthorized(meQuery.error)) {
    return <Navigate to={loginPath(location.pathname + location.search)} replace />;
  }
  if (staleWorkspace || meQuery.isPending) return <FullPageSpin />;
  if (!session) {
    return (
      <Result
        status="error"
        title="Couldn't reach Packages Tracker"
        subTitle={errorMessage(meQuery.error)}
        extra={<Button onClick={() => meQuery.refetch()}>Retry</Button>}
      />
    );
  }
  return <Ctx.Provider value={session}>{children}</Ctx.Provider>;
}
