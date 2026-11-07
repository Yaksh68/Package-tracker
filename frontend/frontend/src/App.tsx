import { MoonOutlined, SunOutlined } from "@ant-design/icons";
import { Button, Layout, Result, Tooltip, theme } from "antd";
import { Link, Route, Routes } from "react-router-dom";
import { SettingsToggle } from "./components/SettingsToggle";
import { RepoDetail } from "./pages/RepoDetail";
import { RepoList } from "./pages/RepoList";
import { ScanActivity } from "./pages/ScanActivity";
import { SnapshotHistory } from "./pages/SnapshotHistory";
import { useThemeMode } from "./theme";

function Brand() {
  const { token } = theme.useToken();
  return (
    <Link to="/" style={{ display: "inline-flex", alignItems: "center", gap: 10, color: token.colorText }}>
      <svg width="22" height="22" viewBox="0 0 16 16" aria-hidden="true">
        <rect width="16" height="16" rx="3.5" fill={token.colorPrimary} />
        <path d="M4 4.5v7M12 4.5v7M4 8h8" stroke={token.colorBgContainer} strokeWidth="1.6" strokeLinecap="round" fill="none" />
      </svg>
      <span style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.01em" }}>Packages Tracker</span>
    </Link>
  );
}

function ThemeToggle() {
  const { mode, toggle } = useThemeMode();
  const label = mode === "dark" ? "Switch to light mode" : "Switch to dark mode";
  return (
    <Tooltip title={label}>
      <Button type="text" aria-label={label} icon={mode === "dark" ? <SunOutlined /> : <MoonOutlined />} onClick={toggle} />
    </Tooltip>
  );
}

export default function App() {
  const { token } = theme.useToken();
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
          position: "sticky",
          top: 0,
          zIndex: 10,
        }}
      >
        <Brand />
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <SettingsToggle />
          <ThemeToggle />
        </div>
      </Layout.Header>
      <Layout.Content style={{ padding: 24, maxWidth: 1400, margin: "0 auto", width: "100%" }}>
        <Routes>
          <Route path="/" element={<RepoList />} />
          <Route path="/repos/:id" element={<RepoDetail />} />
          <Route path="/repos/:id/activity" element={<ScanActivity />} />
          <Route path="/repos/:id/history" element={<SnapshotHistory />} />
          <Route
            path="*"
            element={<Result status="404" title="Page not found" extra={<Link to="/">Back to repositories</Link>} />}
          />
        </Routes>
      </Layout.Content>
    </Layout>
  );
}
