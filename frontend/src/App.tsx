import { MoonOutlined, SunOutlined } from "@ant-design/icons";
import { Button, Layout, Menu, Result, Tooltip, theme } from "antd";
import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Brand } from "./components/Brand";
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

function Nav() {
  const { isPublic, isPlatformAdmin, can } = useSession();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const items = [
    { key: "/", label: "Repositories" },
    { key: "/settings", label: "Settings" },
    ...(isPublic ? [{ key: "/members", label: "Members" }] : []),
    ...(can("audit") ? [{ key: "/audit", label: "Audit log" }] : []),
    ...(isPlatformAdmin ? [{ key: "/admin", label: "Admin" }] : []),
  ];
  const selected = items.find((i) => i.key !== "/" && pathname.startsWith(i.key))?.key
    ?? (pathname === "/" || pathname.startsWith("/repos") ? "/" : "");
  return (
    <Menu
      mode="horizontal"
      selectedKeys={[selected]}
      items={items}
      onClick={({ key }) => navigate(key)}
      style={{ flex: 1, minWidth: 0, borderBottom: "none", background: "transparent", marginLeft: 16 }}
    />
  );
}

function Shell() {
  const { token } = theme.useToken();
  const { isPublic } = useSession();
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
          position: "sticky",
          top: 0,
          zIndex: 10,
        }}
      >
        <Brand />
        <Nav />
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {isPublic && <WorkspaceSwitcher />}
          {isPublic && <InvitesBell />}
          <ThemeToggle />
          {isPublic && <AccountMenu />}
        </div>
      </Layout.Header>
      <Layout.Content style={{ padding: 24, maxWidth: 1400, margin: "0 auto", width: "100%" }}>
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
