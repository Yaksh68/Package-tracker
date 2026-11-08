// Roles and permissions on the client. The server makes every real decision;
// these only decide which controls to disable and what their tooltips say. The
// role table comes from the server (/api/me `role_permissions`), so the two
// can't drift apart.
import type { Me, Perm, Role } from "../types";

export const ROLES: Role[] = ["viewer", "editor", "admin", "owner"];

export const ROLE_LABEL: Record<Role, string> = {
  viewer: "Viewer",
  editor: "Editor",
  admin: "Admin",
  owner: "Owner",
};

export const ROLE_HELP: Record<Role, string> = {
  viewer: "Sees repositories, results, history and scan activity, and exports CSV.",
  editor: "Also adds and edits repositories, uploads zips and runs scans (which spend the workspace's AI key).",
  admin: "Also deletes repositories, changes settings and keys, manages members and reads the audit log.",
  owner: "Also manages Owners, and renames, transfers or deletes the workspace.",
};

export type RoleTable = Record<Role, Perm[]>;

export function can(table: RoleTable, role: Role | null | undefined, perm: Perm): boolean {
  return !!role && (table[role] ?? []).includes(perm);
}

/** The lowest role that has `perm`, or null if none does. */
export function minRole(table: RoleTable, perm: Perm): Role | null {
  return ROLES.find((r) => (table[r] ?? []).includes(perm)) ?? null;
}

/** The tooltip for a control `role` can't use, naming the role it needs.
 *  Undefined when the role allows it. */
export function needsRole(table: RoleTable, role: Role | null | undefined, perm: Perm): string | undefined {
  if (can(table, role, perm)) return undefined;
  const need = minRole(table, perm);
  if (!need) return "Not available";
  const what = need === "owner" ? "the Owner role" : `the ${ROLE_LABEL[need]} role or higher`;
  return `Needs ${what}.${role ? ` Your role here is ${ROLE_LABEL[role]}.` : ""}`;
}

/** The signed-in user's role in a workspace: the active one by default. A
 *  repo can be opened from a link while another workspace is active, so its
 *  page asks about the repo's own workspace. */
export function roleIn(me: Me, workspaceId?: string | null): Role | null {
  if (!workspaceId || workspaceId === me.workspace_id) return me.role;
  return me.workspaces.find((w) => w.id === workspaceId)?.role ?? null;
}

/** "an Admin" / "a Viewer" */
export function withArticle(role: Role): string {
  const label = ROLE_LABEL[role];
  return /^[AEIOU]/.test(label) ? `an ${label}` : `a ${label}`;
}
