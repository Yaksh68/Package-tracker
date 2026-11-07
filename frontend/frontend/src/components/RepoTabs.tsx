import { Tabs } from "antd";
import { useLocation, useNavigate } from "react-router-dom";

/** Packages | Activity | History for one repo. Switching tabs keeps the branch. */
export function RepoTabs({ id, branch }: { id: string; branch?: string }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const active = pathname.endsWith("/activity") ? "activity" : pathname.endsWith("/history") ? "history" : "packages";
  const q = branch ? `?branch=${encodeURIComponent(branch)}` : "";
  const base = `/repos/${encodeURIComponent(id)}`;
  return (
    <Tabs
      activeKey={active}
      onChange={(k) => navigate(`${base}${k === "packages" ? "" : `/${k}`}${q}`)}
      style={{ marginBottom: 8 }}
      items={[
        { key: "packages", label: "Packages" },
        { key: "activity", label: "Activity" },
        { key: "history", label: "History" },
      ]}
    />
  );
}
