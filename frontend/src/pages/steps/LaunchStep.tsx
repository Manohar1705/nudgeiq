import type { LaunchResult } from "../../api/types";
import { useWizard } from "../../wizard/WizardContext";
import { formatCount, humanize } from "../../wizard/format";
import { rewardTotals } from "../../wizard/validate";

export default function LaunchStep({ result }: { result: LaunchResult | null }) {
  const { config, draft, reset } = useWizard();
  if (!config || !draft) return null;
  const cohort = config.cohorts.find((c) => c.cohort_id === draft.cohortId);
  const approved = draft.variants.filter((v) => v.status === "approved").length;
  const signals = Object.values(draft.signals).filter(Boolean).length;
  const size = draft.levers.length * draft.channels.length * draft.offers.length * draft.timings.length;
  const t = rewardTotals(draft.rewards);

  return (
    <section className="launch">
      <h2 className="step-title">Configuration complete</h2>
      <p className="muted">
        <b>{cohort?.label}</b> is set up with {formatCount(cohort?.size ?? 0)} eligible customers.
      </p>
      {result?.campaign && (
        <p className="notice">
          Live now as <b>{result.campaign.config.name}</b> (id {result.campaign.campaign_id}, version {result.campaign.version}).
          The learner for this campaign starts fresh.
        </p>
      )}
      {(result?.validation.issues ?? []).filter((i) => i.severity === "warning").map((i) => (
        <p key={i.code + i.path} className="small amber">Warning: {i.message}</p>
      ))}

      <div className="stats">
        <div className="card stat"><b>{formatCount(cohort?.size ?? 0)}</b><span>Eligible customers</span></div>
        <div className="card stat"><b>{approved}</b><span>Variations approved</span></div>
        <div className="card stat"><b>{signals}</b><span>Context signals</span></div>
        <div className="card stat"><b>{size}</b><span>Candidate actions</span></div>
      </div>

      <div className="card summary">
        <div><span className="muted">Levers</span> {draft.levers.map(humanize).join(", ")}</div>
        <div><span className="muted">Channels</span> {draft.channels.join(", ")}</div>
        <div><span className="muted">Offers</span> {draft.offers.map(humanize).join(", ")}</div>
        <div><span className="muted">Timings</span> {draft.timings.join(", ")}</div>
        <div><span className="muted">Pacing</span> {draft.maxContacts} contacts per {draft.windowDays} days, {draft.cooldownHours}h cool-down</div>
        <div><span className="muted">Rewards</span> +{t.positive} positive, {t.penalty} penalty</div>
      </div>

      <button className="btn ghost" onClick={reset}>↻ Configure another agent</button>
    </section>
  );
}
