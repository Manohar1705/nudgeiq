import { useCallback, useMemo, useState } from "react";
import { api } from "../api/client";
import type { MessageFunnelRow, UpliftDimension, UpliftSlice } from "../api/types";
import { normalizeFunnel, normalizeSlices } from "./normalize";
import { useLoad } from "./useLoad";
import "./insights.css";

const LOW_SAMPLE = 30; // below this many resolved decisions (or sent messages), numbers are rough

const pct = (x: number, digits = 1) => `${(x * 100).toFixed(digits)}%`;
const signedPct = (x: number) => `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%`;
const ACRONYMS = new Set(["sms"]);
const pretty = (s: string) => {
  if (ACRONYMS.has(s.toLowerCase())) return s.toUpperCase();
  const t = s.replace(/[_-]+/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
};
const rate = (n: number, d: number) => (d > 0 ? n / d : null);

/* ---------- Offer / Timing / Channel cards ---------- */

function UpliftBars({ rows }: { rows: UpliftSlice[] }) {
  // One shared scale per card, always containing zero, so negative uplift is visible.
  const lo = Math.min(0, ...rows.map((r) => Math.min(r.uplift ?? 0, r.ci_low ?? 0)));
  const hi = Math.max(0, ...rows.map((r) => Math.max(r.uplift ?? 0, r.ci_high ?? 0)));
  const span = hi - lo || 1;
  const at = (v: number) => `${((v - lo) / span) * 100}%`;

  return (
    <div className="uplift-rows">
      {rows.map((r) => {
        const known = r.uplift !== null;
        const significant = known && r.ci_low !== null && r.ci_high !== null && (r.ci_low > 0 || r.ci_high < 0);
        const rough = r.resolved < LOW_SAMPLE;
        const v = r.uplift ?? 0;
        return (
          <div className={`ua-row ${rough ? "rough" : ""}`} key={r.key}>
            <div className="ua-name">
              {pretty(r.key)}
              <span className="ua-n">n = {r.resolved}{rough ? " · low sample" : ""}</span>
            </div>
            <div className="ua-track" role="img"
              aria-label={known ? `${pretty(r.key)} uplift ${signedPct(v)}` : `${pretty(r.key)} uplift not measurable`}>
              <div className="ua-zero" style={{ left: at(0) }} />
              {known && (
                <div className={`ua-bar ${v >= 0 ? "pos" : "neg"}`}
                  style={{ left: at(Math.min(0, v)), width: `${(Math.abs(v) / span) * 100}%` }} />
              )}
              {known && r.ci_low !== null && r.ci_high !== null && (
                <div className="ua-ci" style={{ left: at(r.ci_low), width: `${((r.ci_high - r.ci_low) / span) * 100}%` }} />
              )}
            </div>
            <div className={`ua-val ${!known ? "" : v >= 0 ? "good" : "bad"}`}>
              {known ? signedPct(v) : "n/a"}
              {significant && <span className="ua-sig" title="Confidence interval excludes zero"> ●</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function UpliftCard({ dimension, title, refreshKey }: { dimension: UpliftDimension; title: string; refreshKey: number }) {
  const { data, error, loading } = useLoad(() => api.getUpliftBy(dimension), `${dimension} analytics`, refreshKey);
  const rows = useMemo(() => (data ? normalizeSlices(data) : []), [data]);
  const sorted = useMemo(() => [...rows].sort((a, b) => (b.uplift ?? -Infinity) - (a.uplift ?? -Infinity)), [rows]);

  return (
    <div className="card ua-card">
      <h3>{title}</h3>
      <p className="muted ua-sub">Uplift vs holdout. Line = confidence interval.</p>
      {loading && !data ? <p className="muted">Loading…</p>
        : error ? <p className="muted">{error}</p>
        : sorted.length === 0 ? <p className="muted">No {dimension} data yet. It appears once decisions have outcomes.</p>
        : <UpliftBars rows={sorted} />}
    </div>
  );
}

/* ---------- Message table ---------- */

type SortKey = "sent" | "opened" | "clicked" | "orders" | "avg_reward";
const COLS: { key: SortKey; label: string; hint: string }[] = [
  { key: "sent", label: "Sent", hint: "Messages dispatched" },
  { key: "opened", label: "Opened", hint: "message_opened outcomes" },
  { key: "clicked", label: "Clicked", hint: "cta_clicked outcomes" },
  { key: "orders", label: "Goal", hint: "order_placed outcomes" },
  { key: "avg_reward", label: "Shaping", hint: "Average reward points per sent message" },
];

function MessageTable({ refreshKey }: { refreshKey: number }) {
  const { data, error, loading } = useLoad(() => api.getMessageFunnel(), "message funnel", refreshKey);
  const rows = useMemo(() => (data ? normalizeFunnel(data) : []), [data]);
  const [sortKey, setSortKey] = useState<SortKey>("sent");
  const [desc, setDesc] = useState(true);

  const sorted = useMemo(
    () => [...rows].sort((a, b) => (desc ? 1 : -1) * (b[sortKey] - a[sortKey])),
    [rows, sortKey, desc],
  );
  const toggle = (k: SortKey) => { if (k === sortKey) setDesc(!desc); else { setSortKey(k); setDesc(true); } };

  const cell = (n: number, sent: number) => {
    const r = rate(n, sent);
    return <td><b>{n}</b>{r !== null && <span className="rate">{pct(r)}</span>}</td>;
  };

  return (
    <div className="card">
      <h3 className="ua-title">Messages</h3>
      <p className="muted ua-sub">
        Percentages are of messages sent. Delivery receipts are not recorded, so there is no Delivered column.
      </p>
      {loading && !data ? <p className="muted">Loading…</p>
        : error ? <p className="muted">{error}</p>
        : sorted.length === 0 ? <p className="muted">No messages sent yet. Run decisions in the Live DRL Engine.</p>
        : (
          <div className="table-scroll">
            <table className="uplift-table msg-table">
              <thead>
                <tr>
                  <th>Message</th>
                  {COLS.map((c) => (
                    <th key={c.key} title={c.hint} aria-sort={c.key === sortKey ? (desc ? "descending" : "ascending") : "none"}>
                      <button className="th-btn" onClick={() => toggle(c.key)}>
                        {c.label}{c.key === sortKey ? (desc ? " ↓" : " ↑") : ""}
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sorted.map((m: MessageFunnelRow) => (
                  <tr key={m.variant_id} className={m.sent < LOW_SAMPLE ? "rough" : ""}>
                    <td>
                      <div className="msg-text">{m.text}</div>
                      <div className="msg-meta">
                        {m.channel && <span className="msg-tag">{m.channel}</span>}
                        {m.sent < LOW_SAMPLE && <span className="msg-tag warn">low sample</span>}
                      </div>
                    </td>
                    <td><b>{m.sent}</b></td>
                    {cell(m.opened, m.sent)}
                    {cell(m.clicked, m.sent)}
                    {cell(m.orders, m.sent)}
                    <td>{m.avg_reward.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </div>
  );
}

/* ---------- Tab ---------- */

export default function ActionTab() {
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  return (
    <div className="stack">
      <div className="insight-actions">
        <button className="btn" onClick={refresh}>Refresh</button>
      </div>
      <div className="ua-grid">
        <UpliftCard dimension="offer" title="Offer analysis" refreshKey={refreshKey} />
        <UpliftCard dimension="timing" title="Timing analysis" refreshKey={refreshKey} />
        <UpliftCard dimension="channel" title="Channel analysis" refreshKey={refreshKey} />
      </div>
      <MessageTable refreshKey={refreshKey} />
    </div>
  );
}
