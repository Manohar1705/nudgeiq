import { NavLink, Outlet } from "react-router-dom";


const TABS = [
  { to: "/configure", label: "Configure Agent" },
  { to: "/live", label: "Live DRL Engine" },
  { to: "/insights", label: "Insights" },
  { to: "/admin", label: "Admin" },
];

export default function Layout() {

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
          <select className="usecase" onChange={() => {}} >
          </select>
          <span className={"badge live"}>
            <span className="dot" /> Live
          </span>
        </div>
      </header>


      <main className="content"><Outlet /></main>
    </div>
  );
}