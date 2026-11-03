import { useWizard } from "../../wizard/WizardContext";
import { formatCount } from "../../wizard/format";

export default function CohortStep() {
  const { config, draft, patch } = useWizard();
  if (!config || !draft) return null;
  return (
    <section>
      <h2 className="step-title">Pick a base cohort</h2>
      <p className="muted">Select the customer pool. The agent decides the right offer per individual at run-time.</p>
      <div className="grid cols-3">
        {config.cohorts.map((c) => (
          <button
            key={c.cohort_id}
            className={draft.cohortId === c.cohort_id ? "pick selected" : "pick"}
            onClick={() => patch({ cohortId: c.cohort_id })}
          >
            <span className="pick-title">{c.label}</span>
            <span className="pick-sub">{formatCount(c.size)} customers</span>
          </button>
        ))}
      </div>
    </section>
  );
}
