import type { OutcomeType } from "../../api/types";
import { useWizard } from "../../wizard/WizardContext";
import { OUTCOME_LABELS } from "../../wizard/format";
import { NEGATIVE_OUTCOMES, POSITIVE_OUTCOMES, rewardTotals } from "../../wizard/validate";

function Row({ k }: { k: OutcomeType }) {
  const { draft, patch } = useWizard();
  if (!draft) return null;
  const val = draft.rewards[k] ?? 0;
  const neg = NEGATIVE_OUTCOMES.includes(k);
  return (
    <div className="reward-row">
      <span>{OUTCOME_LABELS[k] ?? k}</span>
      <span className="reward-input">
        <input
          type="number" value={Number.isNaN(val) ? "" : val}
          className={neg ? "neg" : "pos"}
          onChange={(e) => patch({ rewards: { ...draft.rewards, [k]: e.target.value === "" ? NaN : Number(e.target.value) } })}
        />
        <span className="muted">pts</span>
      </span>
    </div>
  );
}

export default function RewardsStep() {
  const { draft } = useWizard();
  if (!draft) return null;
  const t = rewardTotals(draft.rewards);
  return (
    <section>
      <h2 className="step-title">Teach the agent what "win" means</h2>
      <p className="muted">Positive points shape the policy toward the outcome. Negative points penalise unwanted ones.</p>

      <h3 className="eyebrow green">Terminal reward</h3>
      <div className="card"><Row k="order_placed" /></div>

      <h3 className="eyebrow blue">Interim shaping rewards</h3>
      <div className="card">
        {POSITIVE_OUTCOMES.filter((k) => k !== "order_placed").map((k) => <Row key={k} k={k} />)}
      </div>

      <h3 className="eyebrow red">Penalties</h3>
      <div className="card">{NEGATIVE_OUTCOMES.map((k) => <Row key={k} k={k} />)}</div>

      <div className="summary-bar">
        Positive total <b className="green">+{t.positive}</b> · Penalty total <b className="red">{t.penalty}</b>
      </div>
    </section>
  );
}
