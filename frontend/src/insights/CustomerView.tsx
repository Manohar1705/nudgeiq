import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { CustomerState, DecisionDetail } from "../api/types";
import { actionSub, actionTitle, sameAction, signed } from "../live/labels";
import { CHANNEL_LABELS, OUTCOME_LABELS, humanize } from "../wizard/format";
import { useLoad } from "./useLoad";
import "./insights.css";

const MAX_TRAIL = 12; // newest decisions shown (each needs one detail request)
const GOOD = new Set(["order_placed", "cart_reopened", "checkout_restarted", "app_opened", "cta_clicked", "message_opened"]);
const BAD = new Set(["refunded", "unsubscribed", "bought_elsewhere", "ignored"]);

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");
const yesNo = (b: boolean) => (b ? "Yes" : "No");
const sigVal = (v: unknown) => (v === true ? "Yes" : v === false ? "No" : typeof v === "string" ? humanize(v) : String(v));

function Profile({ c }: { c: CustomerState }) {
  const rows: [string, string][] = [
    ["Customer", c.customer_id],
    ["Cohort", humanize(c.cohort_id)],
    ["Cart value", `$${c.cart_value.toFixed(2)}`],
    ["Abandoned", `${c.abandoned_minutes} min ago`],
    ["Preferred channel", c.preferred_channel ? CHANNEL_LABELS[c.preferred_channel] ?? c.preferred_channel : "None"],
    ["Has app", yesNo(c.has_app)],
    ["SMS opt-in", yesNo(c.sms_opt_in)],
    ["Email opt-in", yesNo(c.email_opt_in)],
    ["Contactable", yesNo(c.contactable && !c.dnd)],
    ["Contacts in window", String(c.contacts_in_window)],
    ["Last contact", when(c.last_contact_at)],
    ["Holdout", yesNo(c.is_holdout)],
    ...Object.entries(c.signals).map(([k, v]): [string, string] => [humanize(k), sigVal(v)]),
  ];
  return (
    <div className="card profile">
      <h3 className="ua-title">Profile</h3>
      <p className="muted ua-sub">Current state of this customer.</p>
      {rows.map(([k, v]) => (
        <div key={k} className="sv"><span className="small muted">{k}</span><b>{v}</b></div>
      ))}
    </div>
  );
}

function Summary({ details }: { details: DecisionDetail[] }) {
  const s = useMemo(() => {
    let sent = 0, waited = 0, holdout = 0, orders = 0, reward = 0;
    for (const { decision: d, outcomes, reward: r } of details) {
      if (d.is_holdout) holdout++;
      else if (!d.chosen.channel) waited++;
      else sent++;
      if (outcomes.some((o) => o.outcome_type === "order_placed")) orders++;
      reward += r?.points ?? 0;
    }
    return { sent, waited, holdout, orders, reward };
  }, [details]);
  const items: [string, string][] = [
    ["Decisions", String(details.length)], ["Nudges sent", String(s.sent)], ["Waited", String(s.waited)],
    ["Holdout", String(s.holdout)], ["Orders", String(s.orders)], ["Total reward", `${signed(s.reward)} pts`],
  ];
  return (
    <div className="cust-summary">
      {items.map(([k, v]) => (
        <div className="card kpi" key={k}><span className="label">{k}</span><b>{v}</b></div>
      ))}
    </div>
  );
}

function TrailItem({ item, open }: { item: DecisionDetail; open: boolean }) {
  const { decision: d, outcomes, reward } = item;
  const ranked = [...d.candidates].sort((a, b) => b.score - a.score).slice(0, 4);
  const waited = !d.chosen.channel;
  return (
    <div className={`card trail-item ${d.is_holdout ? "holdout" : waited ? "wait" : ""}`}>
      <div className="trail-head">
        <div>
          <b>{d.is_holdout ? "Holdout (not messaged)" : actionTitle(d.chosen)}</b>
          {!d.is_holdout && <div className="small muted">{actionSub(d.chosen)}</div>}
        </div>
        <div className="trail-when muted small">{when(d.created_at)}</div>
      </div>

      <div className="ut-pills">
        {d.is_holdout && <span className="ut-pill">Holdout</span>}
        {d.explored && <span className="ut-pill warn">Explored (random pick)</span>}
        {waited && !d.is_holdout && <span className="ut-pill">Waited</span>}
        {outcomes.length === 0
          ? <span className="ut-pill muted">{waited || d.is_holdout ? "No response expected" : "No response recorded yet"}</span>
          : outcomes.map((o, i) => (
            <span key={i} className={`ut-pill ${o.outcome_type === "order_placed" ? "strong" : GOOD.has(o.outcome_type) ? "good" : BAD.has(o.outcome_type) ? "bad" : ""}`}>
              {OUTCOME_LABELS[o.outcome_type] ?? humanize(o.outcome_type)}
            </span>
          ))}
        {reward && <span className={`ut-pill ${reward.points >= 0 ? "good" : "bad"}`} title={reward.reason}>{signed(reward.points)} pts</span>}
      </div>

      {d.message && <p className="trail-msg">{d.message}</p>}

      <details open={open} className="why">
        <summary>Why this action</summary>
        {d.steps.length > 0 && d.steps.map((s, i) => (
          <div key={i} className="trace-row"><b>{humanize(s.agent)}</b><span className="muted">{s.note}</span></div>
        ))}
        {ranked.length > 0 && (
          <table className="uplift-table cand-table">
            <thead><tr><th>Scored candidate</th><th>Score</th></tr></thead>
            <tbody>
              {ranked.map((c, i) => (
                <tr key={i} className={sameAction(c.action, d.chosen) ? "chosen" : ""}>
                  <td>{actionTitle(c.action)} <span className="muted small">{actionSub(c.action)}</span>{sameAction(c.action, d.chosen) && <span className="ut-pill strong picked">chosen</span>}</td>
                  <td>{c.score.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {d.blocked.length > 0 && <p className="muted small">{d.blocked.length} action(s) blocked by guardrails.</p>}
        {d.steps.length === 0 && ranked.length === 0 && <p className="muted small">No reasoning was recorded for this decision.</p>}
      </details>
    </div>
  );
}

export default function CustomerView() {
  const customers = useLoad(() => api.listCustomers(500), "customers", 0);
  const [picked, setPicked] = useState<string | null>(null);
  const list = customers.data ?? [];
  const id = picked ?? list[0]?.customer_id ?? "";
  const customer = list.find((c) => c.customer_id === id) ?? null;

  const trail = useLoad(async () => {
    if (!id) return { total: 0, details: [] as DecisionDetail[] };
    const ds = await api.listDecisions(50, id);
    const newest = [...ds].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, MAX_TRAIL);
    const details = await Promise.all(newest.map((d) => api.getDecision(d.decision_id)));
    return { total: ds.length, details };
  }, "decision history", id);

  if (customers.error) return <div className="card"><p className="muted">{customers.error}</p></div>;
  if (!customers.data) return <div className="card muted">Loading customers…</div>;
  if (list.length === 0) return <div className="card muted">No customers yet. Seed demo customers from the Live DRL Engine.</div>;

  return (
    <div className="stack">
      <div className="card cust-picker">
        <label className="small muted" htmlFor="cust-select">Customer</label>
        <select id="cust-select" className="usecase" value={id} onChange={(e) => setPicked(e.target.value)}>
          {list.map((c) => (
            <option key={c.customer_id} value={c.customer_id}>
              {c.customer_id} · {humanize(c.cohort_id)} · ${c.cart_value.toFixed(0)}
            </option>
          ))}
        </select>
      </div>

      <div className="cust-layout">
        {customer && <Profile c={customer} />}
        <div className="stack">
          {trail.loading ? <div className="card muted">Loading decisions…</div>
            : trail.error ? <div className="card"><p className="muted">{trail.error}</p></div>
            : !trail.data || trail.data.details.length === 0
              ? <div className="card muted">No decisions for this customer yet. Run one in the Live DRL Engine.</div>
              : (
                <>
                  <Summary details={trail.data.details} />
                  <h3 className="eyebrow">Journey audit trail</h3>
                  {trail.data.total > MAX_TRAIL && (
                    <p className="muted small">Showing the latest {MAX_TRAIL} of {trail.data.total} decisions.</p>
                  )}
                  {trail.data.details.map((item, i) => (
                    <TrailItem key={item.decision.decision_id} item={item} open={i === 0} />
                  ))}
                </>
              )}
        </div>
      </div>
    </div>
  );
}
