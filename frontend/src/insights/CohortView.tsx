import { useMemo } from "react";
import { api } from "../api/client";
import type { CustomerState } from "../api/types";
import { CHANNEL_LABELS, humanize } from "../wizard/format";
import { DIMENSIONS as DIMS, loadSlices } from "./slices";
import { useLoad } from "./useLoad";
import "./insights.css";

const LOW_SAMPLE = 30;
const FETCH_LIMIT = 500;
const signedPct = (x: number) => `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%`;

/* ---------- distributions, computed from the customer list ---------- */

interface Dist { title: string; sub?: string; rows: { label: string; count: number }[]; ordered?: boolean }

const BANDS: [string, (v: number) => boolean][] = [
  ["Under $25", (v) => v < 25], ["$25 to $50", (v) => v < 50], ["$50 to $100", (v) => v < 100],
  ["$100 to $200", (v) => v < 200], ["$200 and over", () => true],
];

function tally(values: string[]): { label: string; count: number }[] {
  const m = new Map<string, number>();
  for (const v of values) m.set(v, (m.get(v) ?? 0) + 1);
  return [...m].map(([label, count]) => ({ label, count })).sort((a, b) => b.count - a.count);
}

function buildDistributions(cs: CustomerState[]): Dist[] {
  const out: Dist[] = [];

  out.push({
    title: "Cart value", ordered: true,
    rows: BANDS.map(([label, test], i) => ({
      label, count: cs.filter((c) => test(c.cart_value) && (i === 0 || !BANDS[i - 1][1](c.cart_value))).length,
    })),
  });
  out.push({ title: "Preferred channel", rows: tally(cs.map((c) => (c.preferred_channel ? CHANNEL_LABELS[c.preferred_channel] ?? c.preferred_channel : "None"))) });
  out.push({ title: "Cohort", rows: tally(cs.map((c) => humanize(c.cohort_id))) });
  out.push({
    title: "Reachability", sub: "Not exclusive: a customer can have several.", ordered: true,
    rows: [
      { label: "Has app", count: cs.filter((c) => c.has_app).length },
      { label: "SMS opt-in", count: cs.filter((c) => c.sms_opt_in).length },
      { label: "Email opt-in", count: cs.filter((c) => c.email_opt_in).length },
      { label: "Contactable now", count: cs.filter((c) => c.contactable && !c.dnd).length },
    ],
  });

  // Any text or yes/no signal with 2 to 8 distinct values becomes its own card, so signals such as
  // loyalty tier or fulfilment type appear without code changes. Numeric signals are skipped.
  const keys = [...new Set(cs.flatMap((c) => Object.keys(c.signals)))].sort();
  for (const k of keys) {
    const vals = cs.map((c) => c.signals[k]).filter((v) => v !== undefined && v !== null);
    if (!vals.every((v) => typeof v === "string" || typeof v === "boolean")) continue;
    const labels = vals.map((v) => (v === true ? "Yes" : v === false ? "No" : humanize(String(v))));
    const rows = tally(labels);
    if (rows.length < 2 || rows.length > 8) continue;
    out.push({ title: humanize(k), sub: vals.length < cs.length ? `${cs.length - vals.length} customers have no value.` : undefined, rows });
  }
  return out;
}

function DistCard({ d, total }: { d: Dist; total: number }) {
  return (
    <div className="card ua-card">
      <h3>{d.title}</h3>
      <p className="muted ua-sub">{d.sub ?? " "}</p>
      {d.rows.map((r) => {
        const share = total > 0 ? r.count / total : 0;
        return (
          <div className="dist-row" key={r.label}>
            <span className="dist-label">{r.label}</span>
            <div className="dist-track"><div className="dist-bar" style={{ width: `${share * 100}%` }} /></div>
            <span className="dist-val">{r.count} <span className="muted">({(share * 100).toFixed(0)}%)</span></span>
          </div>
        );
      })}
    </div>
  );
}

/* ---------- findings, from measured uplift ---------- */

function Findings() {
  const { data, loading } = useLoad(loadSlices, "uplift analytics", 0);
  const { significant, total } = useMemo(() => {
    const rows = data?.rows ?? [];
    const significant = rows
      .filter((r) => r.uplift !== null && r.ci_low !== null && r.ci_high !== null && (r.ci_low > 0 || r.ci_high < 0) && r.resolved >= LOW_SAMPLE)
      .sort((a, b) => Math.abs(b.uplift ?? 0) - Math.abs(a.uplift ?? 0));
    return { significant, total: rows.length };
  }, [data]);

  return (
    <div className="card">
      <h3 className="ua-title">Findings</h3>
      <p className="muted ua-sub">
        Slices where the confidence interval excludes zero and there are at least {LOW_SAMPLE} resolved decisions.
        These are measured differences versus holdout, not explanations.
      </p>
      {loading && !data ? <p className="muted">Loading…</p>
        : data && data.failed.length === DIMS.length ? <p className="muted">The uplift analytics endpoints are not available yet.</p>
        : significant.length === 0
          ? <p className="muted">{total === 0 ? "No uplift data yet." : "No offer, timing or channel has a clear effect yet. More resolved decisions are needed."}</p>
          : (
            <ul className="findings">
              {significant.map((f) => (
                <li key={`${f.dimension}:${f.key}`}>
                  <span className="msg-tag">{f.dimension}</span>
                  <b>{f.dimension === "channel" ? CHANNEL_LABELS[f.key] ?? humanize(f.key) : humanize(f.key)}</b>
                  <span className={(f.uplift ?? 0) >= 0 ? "good-text" : "bad-text"}>{signedPct(f.uplift ?? 0)}</span>
                  <span className="muted small">
                    interval {signedPct(f.ci_low ?? 0)} to {signedPct(f.ci_high ?? 0)} · n = {f.resolved}
                  </span>
                </li>
              ))}
            </ul>
          )}
      {data && data.failed.length > 0 && data.failed.length < DIMS.length && (
        <p className="muted small">Not available for: {data.failed.join(", ")}.</p>
      )}
    </div>
  );
}

/* ---------- view ---------- */

export default function CohortView() {
  const customers = useLoad(() => api.listCustomers(FETCH_LIMIT), "customers", 0);
  const dists = useMemo(() => (customers.data ? buildDistributions(customers.data) : []), [customers.data]);

  if (customers.error) return <div className="card"><p className="muted">{customers.error}</p></div>;
  if (!customers.data) return <div className="card muted">Loading customers…</div>;
  const n = customers.data.length;
  if (n === 0) return <div className="card muted">No customers yet. Seed demo customers from the Live DRL Engine.</div>;

  return (
    <div className="stack">
      <p className="muted small">
        Based on {n} customers{n >= FETCH_LIMIT ? ` (the first ${FETCH_LIMIT}; there may be more)` : ""}.
        Traits the system does not record, such as drop-off stage, top category or device, are not shown.
      </p>
      <Findings />
      <div className="ua-grid">
        {dists.map((d) => <DistCard key={d.title} d={d} total={n} />)}
      </div>
    </div>
  );
}
