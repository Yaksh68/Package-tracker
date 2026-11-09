import { MenuOutlined, MoonOutlined, SunOutlined } from "@ant-design/icons";
import { Button, Drawer, Layout, Menu, Result, Tooltip, theme } from "antd";
import { useEffect, useState } from "react";
import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Brand } from "./components/Brand";
import { useIsMobile } from "./lib/responsive";
import { AccountMenu, InvitesBell, WorkspaceSwitcher } from "./components/HeaderControls";
import { AdminPage } from "./pages/Admin";
import { AuditPage } from "./pages/Audit";
import { InvitePage } from "./pages/Invite";
import { PrivacyPage, TermsPage } from "./pages/Legal";
import { LoginPage } from "./pages/Login";
import { MembersPage } from "./pages/Members";
import { RepoDetail } from "./pages/RepoDetail";
import { RepoList } from "./pages/RepoList";
import { ScanActivity } from "./pages/ScanActivity";
import { SettingsPage } from "./pages/Settings";
import { SnapshotHistory } from "./pages/SnapshotHistory";
import { SessionGate, useSession } from "./session";
import { useThemeMode } from "./theme";

function ThemeToggle() {
  const { mode, toggle } = useThemeMode();
  const label = mode === "dark" ? "Switch to light mode" : "Switch to dark mode";
  return (
    <Tooltip title={label}>
      <Button type="text" aria-label={label} icon={mode === "dark" ? <SunOutlined /> : <MoonOutlined />} onClick={toggle} />
    </Tooltip>
  );
}

function Nav({ mode = "horizontal" }: { mode?: "horizontal" | "inline" }) {
  const { isPublic, isPlatformAdmin, can } = useSession();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const items = [
    { key: "/", label: "Repositories" },
    { key: "/settings", label: "Settings" },
    ...(isPublic ? [{ key: "/members", label: "Members" }] : []),
    ...(can("audit") ? [{ key: "/audit", label: "Audit log" }] : []),
    // the local mode's built-in user owns everything, but there are no accounts to administer
    ...(isPublic && isPlatformAdmin ? [{ key: "/admin", label: "Admin" }] : []),
  ];
  const selected = items.find((i) => i.key !== "/" && pathname.startsWith(i.key))?.key
    ?? (pathname === "/" || pathname.startsWith("/repos") ? "/" : "");
  return (
    <Menu
      mode={mode}
      selectedKeys={[selected]}
      items={items}
      onClick={({ key }) => navigate(key)}
      style={mode === "horizontal"
        ? { flex: 1, minWidth: 0, borderBottom: "none", background: "transparent", marginLeft: 16 }
        : { borderInlineEnd: "none", background: "transparent" }}
    />
  );
}

function Shell() {
  const { token } = theme.useToken();
  const { isPublic } = useSession();
  const mobile = useIsMobile();
  const location = useLocation();
  // On a phone the menu and the workspace switcher live in a drawer, which
  // closes on any navigation (a menu item, or switching workspaces).
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => setMenuOpen(false), [location.key]);

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Header
        style={{
          display: "flex",
          alignItems: "center",
          gap: mobile ? 4 : 12,
          paddingInline: mobile ? 8 : 24,
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
          position: "sticky",
          top: 0,
          zIndex: 10,
        }}
      >
        {mobile && (
          <Button type="text" aria-label="Menu" icon={<MenuOutlined />} onClick={() => setMenuOpen(true)} />
        )}
        <Brand compact={mobile} />
        {mobile ? <div style={{ flex: 1 }} /> : <Nav />}
        <div style={{ display: "flex", alignItems: "center", gap: mobile ? 0 : 8, flexShrink: 0 }}>
          {isPublic && !mobile && <WorkspaceSwitcher />}
          {isPublic && <InvitesBell />}
          <ThemeToggle />
          {isPublic && <AccountMenu />}
        </div>
      </Layout.Header>
      {mobile && (
        <Drawer
          placement="left"
          width={280}
          open={menuOpen}
          onClose={() => setMenuOpen(false)}
          title={<Brand />}
          styles={{ body: { padding: "8px 0" } }}
        >
          {isPublic && (
            <div style={{ padding: "4px 16px 12px" }}>
              <WorkspaceSwitcher block />
            </div>
          )}
          <Nav mode="inline" />
        </Drawer>
      )}
      <Layout.Content style={{ padding: mobile ? 12 : 24, maxWidth: 1400, margin: "0 auto", width: "100%" }}>
        <Routes>
          <Route path="/" element={<RepoList />} />
          <Route path="/repos/:id" element={<RepoDetail />} />
          <Route path="/repos/:id/activity" element={<ScanActivity />} />
          <Route path="/repos/:id/history" element={<SnapshotHistory />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/members" element={<MembersPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="/invite/:token" element={<InvitePage />} />
          <Route
            path="*"
            element={<Result status="404" title="Page not found" extra={<Link to="/">Back to repositories</Link>} />}
          />
        </Routes>
      </Layout.Content>
    </Layout>
  );
}

export default function App() {
  return (
    <Routes>
      {/* open to anyone: signing in, and what signing in means */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/privacy" element={<PrivacyPage />} />
      <Route path="/terms" element={<TermsPage />} />
      <Route
        path="*"
        element={
          <SessionGate>
            <Shell />
          </SessionGate>
        }
      />
    </Routes>
  );
}
