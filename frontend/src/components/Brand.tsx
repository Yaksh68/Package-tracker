import { theme } from "antd";
import { Link } from "react-router-dom";

export function BrandMark({ size = 22 }: { size?: number }) {
  const { token } = theme.useToken();
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true">
      <rect width="16" height="16" rx="3.5" fill={token.colorPrimary} />
      <path d="M4 4.5v7M12 4.5v7M4 8h8" stroke={token.colorBgContainer} strokeWidth="1.6" strokeLinecap="round" fill="none" />
    </svg>
  );
}

export function Brand() {
  const { token } = theme.useToken();
  return (
    <Link to="/" style={{ display: "inline-flex", alignItems: "center", gap: 10, color: token.colorText, flexShrink: 0 }}>
      <BrandMark />
      <span style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.01em", whiteSpace: "nowrap" }}>Packages Tracker</span>
    </Link>
  );
}
