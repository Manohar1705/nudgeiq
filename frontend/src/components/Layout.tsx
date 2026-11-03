import { NavLink, Outlet } from "react-router-dom";
import { api } from "../api/client";
import { useApi } from "../hooks/useApi";

const TABS = [
  { to: "/configure", label: "Configure Agent" },
  { to: "/live", label: "Live DRL Engine" },
  { to: "/insights", label: "Insights" },
];

export default function Layout() {
  const health = useApi(api.health);
  const campaigns = useApi(api.listCampaigns);
  const online = health.data?.status === "ok";
  const list = campaigns.data ?? [];
  const activeId = list.find((c) => c.active)?.use_case_id ?? "";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><span className="logo">✦</span> NudgeIQ</div>

        <nav className="tabs">
          {TABS.map((t) => (
            <NavLink key={t.to} to={t.to} className={({ isActive }) => (isActive ? "tab active" : "tab")}>
              {t.label}
            </NavLink>
          ))}
        </nav>

        <div className="topbar-right">
          <span className="muted small">USE CASE</span>
          {/* The server runs one use case at a time, so this is read-only until 5B. */}
          <select className="usecase" value={activeId} onChange={() => {}} disabled={list.length < 2}>
            {list.map((c) => (
              <option key={c.use_case_id} value={c.use_case_id}>{c.name}</option>
            ))}
            {list.length === 0 && <option value="">{campaigns.loading ? "Loading…" : "Unavailable"}</option>}
          </select>
          <span className={online ? "badge live" : "badge off"}>
            <span className="dot" /> {health.loading ? "Checking" : online ? "Live" : "Offline"}
          </span>
        </div>
      </header>

      {health.error && <div className="banner">{health.error}</div>}

      <main className="content"><Outlet /></main>
    </div>
  );
}
