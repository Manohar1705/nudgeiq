import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { GroupStats, UpliftSummary } from "../api/types";
import { GuardrailsCard, SegmentCard } from "./UpliftPanels";
import { CI_LEVEL, formatP, upliftTest } from "./stats";
import "./insights.css";

const LOW_SAMPLE = 30; // below this many resolved decisions, treat the numbers as rough

const pct = (x: number, digits = 1) => `${(x * 100).toFixed(digits)}%`;
const signedPct = (x: number) => `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%`;

function Bars({ title, rows, fmt }: {
  title: string;
  rows: { name: string; cls: string; value: number }[];
  fmt: (v: number) => string;
}) {
  const max = Math.max(...rows.map((r) => r.value), 0.000001);
  return (
    <div className="card compare">
      <h3>{title}</h3>
      {rows.map((r) => (
        <div className="cmp-row" key={r.name}>
          <span>{r.name}</span>
          <div className="cmp-track"><div className={`cmp-bar ${r.cls}`} style={{ width: `${(r.value / max) * 100}%` }} /></div>
          <span className="cmp-val">{fmt(r.value)}</span>
        </div>
      ))}
    </div>
  );
}

/** The backend's own interval on uplift, if it reports one. */
function backendInterval(s: UpliftSummary): [number, number] | null {
  if (Array.isArray(s.ci) && s.ci.length === 2) return [s.ci[0], s.ci[1]];
  return typeof s.ci_low === "number" && typeof s.ci_high === "number" ? [s.ci_low, s.ci_high] : null;
}

function Verdict({ s, ci }: { s: UpliftSummary; ci: [number, number] | null }) {
  if (s.treated.resolved === 0 && s.holdout.resolved === 0)
    return <div className="notice">No finished decisions yet. Run decisions in Live DRL Engine and let the customer respond.</div>;
  if (s.holdout.resolved === 0)
    return <div className="notice">There is no holdout group yet, so uplift cannot be measured. It needs customers who were deliberately not messaged.</div>;
  if (s.uplift === null)
    return <div className="notice">Holdout conversion is 0%, so relative uplift is undefined. Collect more holdout results.</div>;
  if (Math.min(s.treated.resolved, s.holdout.resolved) < LOW_SAMPLE)
    return <div className="notice">Small sample (under {LOW_SAMPLE} resolved decisions in a group). Treat this uplift as a rough signal.</div>;
  if (ci && ci[0] <= 0 && ci[1] >= 0)
    return <div className="notice">The confidence interval includes zero, so this uplift could be noise. More resolved decisions are needed.</div>;
  return null;
}

const row = (name: string, g: GroupStats) => (
  <tr key={name}>
    <td>{name}</td><td>{g.decisions}</td><td>{g.resolved}</td><td>{g.orders}</td>
    <td>{pct(g.conversion)}</td><td>{g.avg_reward.toFixed(2)}</td>
  </tr>
);

export default function UpliftTab() {
  const [data, setData] = useState<UpliftSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    try { setData(await api.getUplift()); setError(null); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const refresh = () => { setRefreshKey((k) => k + 1); void load(); };

  // Significance from the counts, so it works even if the backend sends no interval.
  const test = useMemo(
    () => (data ? upliftTest(data.treated.orders, data.treated.resolved, data.holdout.orders, data.holdout.resolved) : null),
    [data],
  );

  if (error) return <div className="card"><p className="muted">{error}</p><button className="btn" onClick={load}>Retry</button></div>;
  if (!data) return <div className="card muted">Loading…</div>;

  const { treated, holdout } = data;
  const lift = data.uplift;
  const abs = holdout.resolved > 0 ? treated.conversion - holdout.conversion : null;
  const incremental = abs !== null ? Math.round(abs * treated.resolved) : null;

  const reported = backendInterval(data);
  const ci = reported ?? test?.ci ?? null;
  const ciText = ci
    ? `${reported ? "interval" : `${Math.round(CI_LEVEL * 100)}% CI`} ${signedPct(ci[0])} to ${signedPct(ci[1])}`
    : null;
  const sigText = [test ? formatP(test.p) : null, ciText].filter(Boolean).join(" · ") || "treated vs holdout conversion";

  const total = treated.decisions + holdout.decisions;
  const share = (n: number) => (total > 0 ? pct(n / total, 0) : "0%");

  return (
    <div className="stack" key={refreshKey}>
      <div className="insight-actions">
        <button className="btn" onClick={refresh} disabled={loading}>{loading ? "Refreshing…" : "Refresh"}</button>
      </div>
      <Verdict s={data} ci={ci} />

      <div className="kpis">
        <div className="card kpi">
          <span className="label">Decisions</span>
          <b>{total.toLocaleString()}</b>
          <span className="sub">{share(treated.decisions)} treatment · {share(holdout.decisions)} holdout control</span>
        </div>
        <div className={`card kpi ${lift === null ? "" : lift >= 0 ? "good" : "bad"}`}>
          <span className="label">Uplift vs control</span>
          <b>{lift === null ? "n/a" : signedPct(lift)}</b>
          <span className="sub">{sigText}</span>
        </div>
        <div className="card kpi blue">
          <span className="label">Incremental orders</span>
          <b>{incremental === null ? "n/a" : incremental}</b>
          <span className="sub">Above what control would have delivered</span>
        </div>
        <div className="card kpi">
          <span className="label">Absolute lift</span>
          <b>{abs === null ? "n/a" : `${abs >= 0 ? "+" : ""}${(abs * 100).toFixed(1)} pts`}</b>
          <span className="sub">treated minus holdout conversion</span>
        </div>
      </div>

      <div className="up-pair">
        <Bars
          title="Conversion rate"
          fmt={(v) => pct(v)}
          rows={[
            { name: "Treated", cls: "treated", value: treated.conversion },
            { name: "Holdout", cls: "holdout", value: holdout.conversion },
          ]}
        />
        <Bars
          title="Average reward per decision"
          fmt={(v) => v.toFixed(2)}
          rows={[
            { name: "Treated", cls: "treated", value: Math.max(treated.avg_reward, 0) },
            { name: "Holdout", cls: "holdout", value: Math.max(holdout.avg_reward, 0) },
          ]}
        />
      </div>

      <div className="up-pair wide-left">
        <SegmentCard key={`s${refreshKey}`} />
        <GuardrailsCard key={`g${refreshKey}`} />
      </div>

      <div className="card">
        <div className="table-scroll">
          <table className="uplift-table">
            <thead><tr><th>Group</th><th>Decisions</th><th>Resolved</th><th>Orders</th><th>Conversion</th><th>Avg reward</th></tr></thead>
            <tbody>{row("Treated", treated)}{row("Holdout", holdout)}</tbody>
          </table>
        </div>
        <p className="muted small" style={{ marginTop: 12 }}>
          {data.waited} decisions waited (no message sent). For one customer's decisions, see User Analytics.
        </p>
      </div>
    </div>
  );
}
