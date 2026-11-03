import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { CustomerState, Decision, OutcomeType, RespondResult, ScoredAction } from "../api/types";
import PolicyDiagram from "../live/PolicyDiagram";
import { actionSub, actionTitle, sameAction, signed } from "../live/labels";
import { CHANNEL_LABELS, OUTCOME_LABELS, humanize } from "../wizard/format";
import { NEGATIVE_OUTCOMES, POSITIVE_OUTCOMES } from "../wizard/validate";
import { useWizard } from "../wizard/WizardContext";

const STEPS = ["Load state", "Forward pass", "argmax", "Send action", "Observe response", "Compute reward", "Policy update"];

// Guardrails use UTC send windows (09-21) and send time = "now" minus abandoned minutes + the timing
// offset. Evaluating at 20:00 UTC lets the most demo customers get a legal send time.
const asOf = () => {
  const d = new Date();
  d.setUTCHours(20, 0, 0, 0);
  return d.toISOString();
};

async function pickCustomer(): Promise<CustomerState> {
  let customers = await api.listCustomers(500);
  if (customers.length === 0) {
    await api.seedDemo(200, Math.floor(Math.random() * 1000));
    customers = await api.listCustomers(500);
  }
  const decided = new Set((await api.listDecisions(500)).map((d) => d.customer_id));
  const usable = customers.filter((c) => !c.is_holdout && c.contactable);
  const fresh = usable.filter((c) => !decided.has(c.customer_id));
  const pool = fresh.length ? fresh : usable;
  if (pool.length === 0) throw new Error("No contactable customers. Seed more with POST /demo/seed.");
  return pool[Math.floor(Math.random() * pool.length)];
}

function shownCandidates(d: Decision): ScoredAction[] {
  const ranked = [...d.candidates].sort((a, b) => b.score - a.score);
  const top = ranked.slice(0, 4);
  if (!d.chosen.channel) return top;
  if (!top.some((c) => sameAction(c.action, d.chosen))) {
    top.push(ranked.find((c) => sameAction(c.action, d.chosen)) ?? { action: d.chosen, score: NaN });
  }
  return top;
}

export default function LivePage() {
  const { config } = useWizard();
  const [phase, setPhase] = useState(0); // 0 idle, 1..7 = step reached
  const [customer, setCustomer] = useState<CustomerState | null>(null);
  const [decision, setDecision] = useState<Decision | null>(null);
  const [result, setResult] = useState<RespondResult | null>(null);
  const [episode, setEpisode] = useState(0);
  const [cumulative, setCumulative] = useState(0);
  const [busy, setBusy] = useState(false);
  const [auto, setAuto] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const advance = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      if (phase === 0 || phase === 7) {
        setCustomer(await pickCustomer());
        setDecision(null);
        setResult(null);
        setPhase(1);
      } else if (phase === 1 && customer) {
        setDecision(await api.decide(customer.customer_id, asOf()));
        setPhase(2);
      } else if (phase === 4 && decision) {
        setResult(await api.respond(decision.decision_id));
        setPhase(5);
      } else if (phase === 6) {
        setCumulative((c) => c + (result?.reward?.points ?? 0));
        setEpisode((e) => e + 1);
        setPhase(7);
      } else {
        setPhase(phase + 1);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setAuto(false);
    } finally {
      setBusy(false);
    }
  }, [busy, phase, customer, decision, result]);

  const advanceRef = useRef(advance);
  advanceRef.current = advance;
  useEffect(() => {
    if (!auto || busy) return;
    const t = setTimeout(() => void advanceRef.current(), 1100);
    return () => clearTimeout(t);
  }, [auto, busy, phase]);

  function reset() {
    setAuto(false); setPhase(0); setCustomer(null); setDecision(null); setResult(null);
    setEpisode(0); setCumulative(0); setError(null);
  }

  const chosen = decision?.chosen ?? null;
  const waited = !!decision && !decision.chosen.channel;
  const list = decision ? shownCandidates(decision) : [];
  const chosenIdx = decision && chosen && phase >= 3 ? list.findIndex((c) => sameAction(c.action, chosen)) : -1;
  const observed = new Set<OutcomeType>(phase >= 5 && result ? result.outcomes : []);
  const mainBtn = phase === 0 ? "Start Episode" : phase === 7 ? "Next Episode" : "Step Forward";
  const points = config?.rewards.points ?? {};

  return (
    <div>
      <div className="live-controls">
        <button className="btn ghost-border" onClick={reset}>↺ Reset</button>
        <button className="btn primary" disabled={busy} onClick={() => void advance()}>{busy ? "Working…" : `${mainBtn} →`}</button>
        <button className={auto ? "btn ghost-border on" : "btn ghost-border"} onClick={() => setAuto(!auto)}>
          {auto ? "❚❚ Stop" : "▷ Auto"}
        </button>
      </div>

      <div className="pills">
        {STEPS.map((s, i) => (
          <span key={s} className={phase === i + 1 ? "pill-step active" : phase > i + 1 ? "pill-step done" : "pill-step"}>
            {i + 1}. {s}
          </span>
        ))}
      </div>
      {error && <p className="red">{error}</p>}

      <div className="grid cols-4">
        <div className="card topcard a">
          <span className="eyebrow">Action selected</span>
          <b className="big green">{phase >= 3 && chosen ? actionTitle(chosen) : "—"}</b>
          <span className="small muted">{phase >= 3 && chosen ? actionSub(chosen) : "waiting for forward pass"}</span>
        </div>
        <div className="card topcard b">
          <span className="eyebrow">Customer response</span>
          <b className="big amber">
            {phase >= 5 && result ? (result.nudged ? result.outcomes.map((o) => OUTCOME_LABELS[o] ?? o).join(", ") : "No nudge sent") : "—"}
          </b>
          <span className="small muted">
            {phase >= 5 && chosen?.channel ? `via ${CHANNEL_LABELS[chosen.channel] ?? chosen.channel}` : "waiting for action"}
          </span>
        </div>
        <div className="card topcard c">
          <span className="eyebrow">Reward signal</span>
          <b className="big blue">{phase >= 6 ? (result?.reward ? `${signed(result.reward.points)} pts` : "0 pts") : "—"}</b>
          <span className="small muted">{phase >= 6 ? result?.reward?.reason ?? "no reward" : "not computed"}</span>
        </div>
        <div className="card topcard d">
          <span className="eyebrow">Policy status</span>
          <b className="big purple">{phase === 0 ? "Idle" : STEPS[phase - 1]}</b>
          <span className="small muted">Episode {episode} · Σ {signed(cumulative)}</span>
        </div>
      </div>

      <div className="live-grid">
        <div className="card">
          <h3 className="eyebrow blue">State vector{customer ? ` · ${customer.customer_id}` : ""}</h3>
          {!customer && <p className="muted small">Start an episode to load a customer.</p>}
          {customer && <StatePanel c={customer} />}
        </div>

        <div className="card">
          <PolicyDiagram active={phase >= 2} chosenIndex={chosenIdx >= 0 ? chosenIdx : null} />
          <p className="small muted center">Illustrative only: the Master agent is a bandit, not a neural network.</p>
        </div>

        <div className="card">
          <h3 className="eyebrow green">Actions · score</h3>
          {phase < 2 && <p className="muted small">Scores appear after the forward pass.</p>}
          {phase >= 2 && waited && <p className="muted small">No legal candidates, so the agent waits. {decision?.blocked.length ?? 0} actions were blocked by guardrails.</p>}
          {phase >= 2 && list.map((c) => {
            const isBest = phase >= 3 && !!chosen && sameAction(c.action, chosen);
            return (
              <div key={`${c.action.channel}|${c.action.timing}|${c.action.offer}|${c.action.lever}`} className={isBest ? "act best" : "act"}>
                <b>{isBest ? "✓ " : ""}{actionTitle(c.action)}</b>
                <span className="small muted">{actionSub(c.action)}</span>
                <span className="mono-sm">Score = {Number.isNaN(c.score) ? "n/a" : c.score.toFixed(4)}</span>
              </div>
            );
          })}
          {phase >= 3 && decision?.explored && <p className="small amber">Exploration: this pick was random, not the top score.</p>}
        </div>

        <div className="card">
          <h3 className="eyebrow amber">Reward function</h3>
          {[...POSITIVE_OUTCOMES, ...NEGATIVE_OUTCOMES].map((k) => (
            <div key={k} className={observed.has(k) ? "rw hit" : "rw"}>
              <span>{OUTCOME_LABELS[k]}</span>
              <b className={(points[k] ?? 0) < 0 ? "red" : "green"}>{signed(points[k] ?? 0)}</b>
            </div>
          ))}
          <div className="rw total"><span>CUMULATIVE</span><b className="green">{signed(cumulative)}</b></div>
        </div>
      </div>

      {decision && phase >= 3 && (
        <div className="card trace">
          <h3 className="eyebrow">Agent trace</h3>
          {decision.steps.map((s, i) => (
            <div key={i} className="trace-row"><b>{humanize(s.agent)}</b><span className="muted">{s.note}</span></div>
          ))}
          {phase >= 4 && decision.message && <pre className="mono">{decision.message}</pre>}
        </div>
      )}
    </div>
  );
}

function StatePanel({ c }: { c: CustomerState }) {
  const rows: [string, string][] = [
    ["Cart value", `$${c.cart_value.toFixed(2)}`],
    ["Abandoned", `${c.abandoned_minutes} min ago`],
    ["Cohort", humanize(c.cohort_id)],
    ["Preferred channel", c.preferred_channel ? CHANNEL_LABELS[c.preferred_channel] ?? c.preferred_channel : "none"],
    ["Contacts in window", String(c.contacts_in_window)],
    ["App / SMS / email", `${c.has_app ? "app" : "no app"} · ${c.sms_opt_in ? "sms" : "no sms"} · ${c.email_opt_in ? "email" : "no email"}`],
    ...Object.entries(c.signals).map(([k, v]): [string, string] => [humanize(k), String(v)]),
  ];
  return (
    <div>
      {rows.map(([k, v]) => (
        <div key={k} className="sv"><span className="small muted">{k}</span><b>{v}</b></div>
      ))}
    </div>
  );
}
