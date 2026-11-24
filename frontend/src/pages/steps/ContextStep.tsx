import { useWizard } from "../../wizard/WizardContext";

export default function ContextStep() {
  const { config, draft, patch } = useWizard();
  if (!config || !draft) return null;
  const total = config.context_signals.length;
  const on = Object.values(draft.signals).filter(Boolean).length;

  return (
    <section>
      <h2 className="step-title">Wire up the context the agent gets to see</h2>
      <p className="muted">{on} of {total} signals selected</p>
      <div className="grid cols-3">
        {config.context_signals.map((s) => {
          const checked = !!draft.signals[s.signal_id];
          return (
            <label key={s.signal_id} className={checked ? "pick selected" : "pick"}>
              <input
                type="checkbox"
                className="check"
                checked={checked}
                onChange={() => patch({ signals: { ...draft.signals, [s.signal_id]: !checked } })}
              />
              <span className="pick-title">{s.label}</span>
              <span className="pick-sub">{s.fields.length ? s.fields.join(", ") : "built-in"}</span>
            </label>
          );
        })}
      </div>
    </section>
  );
}
