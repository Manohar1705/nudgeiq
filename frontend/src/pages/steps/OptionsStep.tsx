import type { Channel } from "../../api/types";
import { useWizard } from "../../wizard/WizardContext";
import { CHANNEL_LABELS, WEEKDAYS, humanize } from "../../wizard/format";

function toggle<T>(list: T[], item: T): T[] {
  return list.includes(item) ? list.filter((x) => x !== item) : [...list, item];
}

function Chips<T extends string>(props: {
  options: T[]; selected: T[]; label: (v: T) => string; onToggle: (v: T) => void;
}) {
  return (
    <div className="chips">
      {props.options.map((o) => (
        <button key={o} className={props.selected.includes(o) ? "chip on" : "chip"} onClick={() => props.onToggle(o)}>
          {props.label(o)}
        </button>
      ))}
    </div>
  );
}

// Timings are parsed generically by the backend (T+45m, T+1h, T+3d), so extras are safe to offer.
const EXTRA_TIMINGS = ["T+1h", "T+24h", "T+3d", "T+7d"];

export default function OptionsStep() {
  const { config, draft, patch } = useWizard();
  if (!config || !draft) return null;
  const a = config.action_space;
  const timingOptions = Array.from(new Set([...a.timings, ...EXTRA_TIMINGS]));
  const size = draft.levers.length * draft.channels.length * draft.offers.length * draft.timings.length;
  const num = (v: string) => (v === "" ? NaN : Number(v));

  return (
    <section>
      <h2 className="step-title">Action space the agent can choose from</h2>
      <p className="muted">You define the menu of levers, channels, offers and timing. The agent decides which combination to send.</p>

      <div className="grid cols-2">
        <div className="card">
          <h3 className="eyebrow">Recovery levers ({draft.levers.length}/{a.levers.length})</h3>
          <Chips options={a.levers} selected={draft.levers} label={humanize} onToggle={(v) => patch({ levers: toggle(draft.levers, v) })} />
          <h3 className="eyebrow">Channels</h3>
          <Chips<Channel>
            options={a.channels} selected={draft.channels}
            label={(c) => CHANNEL_LABELS[c] ?? c}
            onToggle={(v) => patch({ channels: toggle(draft.channels, v) })}
          />
          <h3 className="eyebrow">Offers</h3>
          <Chips options={a.offers} selected={draft.offers} label={humanize} onToggle={(v) => patch({ offers: toggle(draft.offers, v) })} />
        </div>

        <div className="card">
          <h3 className="eyebrow">Send-time windows (UTC)</h3>
          <div className="weekdays">
            {WEEKDAYS.map((d, i) => (
              <button
                key={i}
                className={draft.weekdays.includes(i) ? "chip round on" : "chip round"}
                onClick={() => patch({ weekdays: toggle(draft.weekdays, i) })}
              >{d}</button>
            ))}
          </div>
          <div className="row">
            <label className="field">From hour
              <input type="number" min={0} max={23} value={draft.startHour}
                onChange={(e) => patch({ startHour: num(e.target.value) })} />
            </label>
            <label className="field">To hour
              <input type="number" min={1} max={24} value={draft.endHour}
                onChange={(e) => patch({ endHour: num(e.target.value) })} />
            </label>
          </div>
          <h3 className="eyebrow">Timing slots</h3>
          <Chips options={timingOptions} selected={draft.timings} label={(t) => t} onToggle={(v) => patch({ timings: toggle(draft.timings, v) })} />
        </div>
      </div>

      <div className="card">
        <h3 className="eyebrow">Pacing and guardrails</h3>
        <div className="row">
          <label className="field">Max contacts per customer
            <input type="number" min={0} value={draft.maxContacts} onChange={(e) => patch({ maxContacts: num(e.target.value) })} />
          </label>
          <label className="field">Window (days)
            <input type="number" min={1} value={draft.windowDays} onChange={(e) => patch({ windowDays: num(e.target.value) })} />
          </label>
          <label className="field">Cool-down (hours)
            <input type="number" min={0} value={draft.cooldownHours} onChange={(e) => patch({ cooldownHours: num(e.target.value) })} />
          </label>
        </div>
      </div>

      <div className="summary-bar">
        Action-space size: <b>{draft.levers.length} × {draft.channels.length} × {draft.offers.length} × {draft.timings.length} = {size}</b> candidate actions per customer
      </div>
    </section>
  );
}
