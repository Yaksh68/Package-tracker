import { describe, expect, it } from "vitest";
import type { Me } from "../types";
import { can, minRole, needsRole, roleIn, withArticle, type RoleTable } from "./permissions";

// the server's table (rbac.ROLE_PERMS), as /api/me sends it
const TABLE: RoleTable = {
  viewer: ["view"],
  editor: ["edit", "scan", "view"],
  admin: ["audit", "delete_repo", "edit", "members", "scan", "secrets", "settings", "view"],
  owner: ["audit", "delete_repo", "edit", "members", "owner", "scan", "secrets", "settings", "view"],
};

describe("permissions", () => {
  it("checks a role against the server's table", () => {
    expect(can(TABLE, "editor", "scan")).toBe(true);
    expect(can(TABLE, "editor", "delete_repo")).toBe(false);
    expect(can(TABLE, null, "view")).toBe(false);
  });

  it("finds the lowest role with a permission", () => {
    expect(minRole(TABLE, "view")).toBe("viewer");
    expect(minRole(TABLE, "scan")).toBe("editor");
    expect(minRole(TABLE, "settings")).toBe("admin");
    expect(minRole(TABLE, "owner")).toBe("owner");
  });

  it("names the role a disabled control needs", () => {
    expect(needsRole(TABLE, "admin", "settings")).toBeUndefined();
    expect(needsRole(TABLE, "viewer", "scan")).toBe("Needs the Editor role or higher. Your role here is Viewer.");
    expect(needsRole(TABLE, "admin", "owner")).toBe("Needs the Owner role. Your role here is Admin.");
  });

  it("judges a repo by its own workspace's role", () => {
    const me = {
      workspace_id: "personal",
      role: "owner",
      workspaces: [
        { id: "personal", name: "mine", personal: true, role: "owner" },
        { id: "team", name: "Team", personal: false, role: "viewer" },
      ],
    } as unknown as Me;
    expect(roleIn(me)).toBe("owner");
    expect(roleIn(me, "team")).toBe("viewer");
    expect(roleIn(me, "elsewhere")).toBeNull();
    expect(can(TABLE, roleIn(me, "team"), "scan")).toBe(false);
  });

  it("picks the right article", () => {
    expect(withArticle("admin")).toBe("an Admin");
    expect(withArticle("viewer")).toBe("a Viewer");
  });
});
