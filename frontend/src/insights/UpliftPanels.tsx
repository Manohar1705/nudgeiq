import { useMemo } from "react";
import { api } from "../api/client";
import { CHANNEL_LABELS, humanize } from "../wizard/format";
import { normalizeGuardrails } from "./normalize";
import { loadSlices, type DimSlice } from "./slices";
import { useLoad } from "./useLoad";
import "./insights.css";

const LOW_SAMPLE = 30;
const MAX_SEGMENTS = 6;

const pct = (x: number, digits = 1) => `${(x * 100).toFixed(digits)}%`;
const signedPct = (x: number) => `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%`;
const signedPp = (x: number) => `${x >= 0 ? "+" : ""}${(x * 100).toFixed(2)} pp`;

const sliceName = (s: DimSlice) => (s.dimension === "channel" ? CHANNEL_LABELS[s.key] ?? humanize(s.key) : humanize(s.key));

/* ---------- Uplift by segment ---------- */

export function SegmentCard() {
  const { data, loading } = useLoad(loadSlices, "uplift analytics", 0);
  const top = useMemo(
    () => (data?.rows ?? [])
      .filter((r) => r.uplift !== null)
      .sort((a, b) => (b.uplift ?? 0) - (a.uplift ?? 0))
      .slice(0, MAX_SEGMENTS),
    [data],
  );
  const max = Math.max(...top.map((r) => Math.abs(r.uplift ?? 0)), 0.000001);

  return (
    <div className="card ua-card">
      <h3>Uplift by segment</h3>
      <p className="muted ua-sub">Offer, timing and channel slices, best first. Right column: holdout → treated conversion.</p>
      {loading && !data ? <p className="muted">Loading…</p>
        : data && data.failed.length === 3 ? <p className="muted">The uplift analytics endpoints are not available yet.</p>
        : top.length === 0 ? <p className="muted">No measurable segments yet. They appear once there are decisions with outcomes in both groups.</p>
        : top.map((r) => {
          const v = r.uplift ?? 0;
          const rough = r.resolved < LOW_SAMPLE;
          return (
            <div className={`seg-row ${rough ? "rough" : ""}`} key={`${r.dimension}:${r.key}`}>
              <span className="seg-name">
                {sliceName(r)} <span className="msg-tag">{r.dimension}</span>{rough && <span className="msg-tag warn">low sample</span>}
              </span>
              <div className="seg-track"><div className={`seg-bar ${v >= 0 ? "pos" : "neg"}`} style={{ width: `${(Math.abs(v) / max) * 100}%` }} /></div>
              <b className={v >= 0 ? "good-text" : "bad-text"}>{signedPct(v)}</b>
              <span className="seg-conv muted">
                {r.holdout_conversion !== null ? pct(r.holdout_conversion) : "n/a"} → {pct(r.conversion)}
              </span>
            </div>
          );
        })}
    </div>
  );
}

/* ---------- Guardrails ---------- */

async function loadWindow() {
  const [customers, campaigns] = await Promise.all([api.listCustomers(500), api.listCampaigns()]);
  const active = campaigns.find((c) => c.active);
  const cfg = active ? await api.getCampaign(active.use_case_id) : null;
  const avg = customers.length ? customers.reduce((s, c) => s + c.contacts_in_window, 0) / customers.length : null;
  return { avg, cap: cfg?.guardrails.max_contacts_per_window ?? null, windowDays: cfg?.guardrails.window_days ?? null };
}

function Tile({ label, value, sub, tone }: { label: string; value: string; sub: string; tone?: "good" | "bad" }) {
  return (
    <div className="gtile">
      <span className="small muted">{label}</span>
      <b>{value}</b>
      <span className={`gsub ${tone ?? "muted"}`}>{sub}</span>
    </div>
  );
}

export function GuardrailsCard() {
  const win = useLoad(loadWindow, "customers", 0);
  const g = useLoad(() => api.getGuardrails(), "guardrails", 0);
  const groups = useMemo(() => (g.data ? normalizeGuardrails(g.data) : null), [g.data]);

  // Rate with a comparison to holdout. lowerIsBetter flips which direction counts as good.
  const rateTile = (label: string, pick: (x: { sent: number; unsubscribed: number; refunded: number; app_opened: number }) => number, lowerIsBetter: boolean) => {
    if (!groups || groups.treated.sent === 0) return <Tile key={label} label={label} value="n/a" sub="no data yet" />;
    const t = pick(groups.treated) / groups.treated.sent;
    const h = groups.holdout && groups.holdout.sent > 0 ? pick(groups.holdout) / groups.holdout.sent : null;
    if (h === null) return <Tile key={label} label={label} value={pct(t, 2)} sub="no holdout baseline" />;
    const d = t - h;
    const tone = d === 0 ? undefined : (d < 0) === lowerIsBetter ? "good" : "bad";
    return <Tile key={label} label={label} value={pct(t, 2)} sub={`${signedPp(d)} vs holdout`} tone={tone} />;
  };

  const avg = win.data?.avg ?? null;
  const cap = win.data?.cap ?? null;

  return (
    <div className="card ua-card">
      <h3>Guardrails</h3>
      <p className="muted ua-sub">Side effects of nudging, compared with the holdout group.</p>
      <div className="gtiles">
        <Tile
          label="Messages per customer"
          value={avg === null ? "n/a" : avg.toFixed(1)}
          sub={cap === null ? "cap unknown" : `Cap = ${cap}${win.data?.windowDays ? ` per ${win.data.windowDays} days` : ""}`}
          tone={avg !== null && cap !== null ? (avg <= cap ? "good" : "bad") : undefined}
        />
        {rateTile("Unsubscribe rate", (x) => x.unsubscribed, true)}
        {rateTile("Return rate", (x) => x.refunded, true)}
        {rateTile("App opens", (x) => x.app_opened, false)}
      </div>
      {g.error && <p className="muted small gnote">{g.error.replace("guardrails", "guardrails (GET /insights/guardrails)")} Unsubscribe, return and app-open rates need it.</p>}
      {!g.error && g.data !== null && !groups &&<p className="muted small gnote">The guardrails response was not in the expected shape.</p>}
      {win.error && <p className="muted small gnote">{win.error}</p>}
    </div>
  );
}
