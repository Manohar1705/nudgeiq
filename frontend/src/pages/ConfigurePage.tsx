import { useState } from "react";
import { api } from "../api/client";
import Stepper, { WIZARD_STEPS } from "../components/Stepper";
import type { LaunchResult } from "../api/types";
import { buildDraftConfig } from "../wizard/payload";
import { validateStep } from "../wizard/validate";
import { useWizard } from "../wizard/WizardContext";
import CohortStep from "./steps/CohortStep";
import ContextStep from "./steps/ContextStep";
import LaunchStep from "./steps/LaunchStep";
import OptionsStep from "./steps/OptionsStep";
import RewardsStep from "./steps/RewardsStep";
import TemplatesStep from "./steps/TemplatesStep";
import VariationsStep from "./steps/VariationsStep";

const VARIATIONS = 5; // 0-based index of the last step before launch
const LAUNCH = 6;

export default function ConfigurePage() {
  const { loading, error, config, draft, draftConfig, ensureDraft, step, setStep } = useWizard();
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState<string | null>(null);
  const [result, setResult] = useState<LaunchResult | null>(null);

  if (loading) return <p className="muted">Loading campaign…</p>;
  if (error || !draft || !config) {
    return <div className="card"><b>Could not load the campaign.</b><p className="muted">{error}</p></div>;
  }

  const errors = validateStep(step, draft);
  const approved = draft.variants.filter((v) => v.status === "approved").length;

  // Save the wizard's choices on the draft, check them, and only then make the draft live.
  // Going live replaces the campaign that is running now.
  async function launch() {
    if (!draft) return;
    setLaunching(true);
    setLaunchError(null);
    try {
      const id = await ensureDraft();
      const base = draftConfig;
      if (!base) throw new Error("The draft campaign is missing. Go back to the templates step.");
      const saved = await api.updateCampaign(id, buildDraftConfig(base, draft));
      if (!saved.validation.can_activate) {
        setResult({ activated: false, campaign: saved.campaign, validation: saved.validation });
        setLaunchError(`${saved.validation.error_count} problem(s) must be fixed before launch.`);
        return;
      }
      const campaign = await api.activateCampaign(id, true);
      setResult({ activated: true, campaign, validation: saved.validation });
      setStep(LAUNCH);
    } catch (e) {
      setLaunchError(e instanceof Error ? e.message : String(e));
    } finally {
      setLaunching(false);
    }
  }

  return (
    <>
      <Stepper current={step} />
      {step === 0 && <CohortStep />}
      {step === 1 && <ContextStep />}
      {step === 2 && <OptionsStep />}
      {step === 3 && <RewardsStep />}
      {step === 4 && <TemplatesStep />}
      {step === 5 && <VariationsStep />}
      {step === LAUNCH && <LaunchStep result={result} />}

      {step < LAUNCH && errors.length > 0 && (
        <ul className="errors">{errors.map((e) => <li key={e}>{e}</li>)}</ul>
      )}
      {launchError && <p className="red">{launchError}</p>}
      {step < LAUNCH && result && !result.activated && (
        <ul className="errors">
          {result.validation.issues.filter((i) => i.severity === "error").map((i) => (
            <li key={i.code + i.path}>{i.message} <span className="muted small">({i.path})</span></li>
          ))}
        </ul>
      )}

      {step < LAUNCH && (
        <div className="nav-row">
          <button className="btn ghost" disabled={step === 0} onClick={() => setStep(step - 1)}>← Back</button>
          {step === VARIATIONS ? (
            <button className="btn launch" disabled={errors.length > 0 || launching} onClick={launch}>
              {launching ? "Launching…" : `Launch Agent (${approved} approved) and replace the live campaign`}
            </button>
          ) : (
            <button className="btn primary" disabled={errors.length > 0 || step >= WIZARD_STEPS.length - 1}
              onClick={() => setStep(step + 1)}>Continue →</button>
          )}
        </div>
      )}
    </>
  );
}
