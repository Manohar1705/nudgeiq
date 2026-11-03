import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { Channel, GenerationResult, MessageVariant, OutcomeType, UseCaseConfig } from "../api/types";

export interface Draft {
  cohortId: string | null;
  signals: Record<string, boolean>; // signal_id -> enabled
  levers: string[];
  channels: Channel[];
  offers: string[];
  timings: string[];
  maxContacts: number;
  windowDays: number;
  cooldownHours: number;
  startHour: number; // 0-23 (UTC)
  endHour: number; // 1-24 (UTC)
  weekdays: number[]; // Monday = 0
  rewards: Partial<Record<OutcomeType, number>>;
  templateId: string | null;
  templateChannel: Channel | null;
  variants: MessageVariant[]; // the chosen template's variants, as stored on the draft campaign
  generation: GenerationResult | null; // result of the last "generate" call
}

interface WizardValue {
  config: UseCaseConfig | null; // the live campaign the wizard started from
  draftId: string | null; // the draft campaign created for this run (null until step 5)
  draftConfig: UseCaseConfig | null; // the draft's own config; launch saves onto this
  /** Create the draft campaign (a clone of the live one) once, then return its id. */
  ensureDraft: () => Promise<string>;
  setVariant: (v: MessageVariant) => void; // replace one variant after a review call
  loading: boolean;
  error: string | null;
  draft: Draft | null;
  step: number; // 0-based
  setStep: (n: number) => void;
  patch: (p: Partial<Draft>) => void;
  reset: () => void;
}

export function draftFromConfig(cfg: UseCaseConfig): Draft {
  const g = cfg.guardrails;
  return {
    cohortId: null,
    signals: Object.fromEntries(cfg.context_signals.map((s) => [s.signal_id, s.enabled])),
    levers: [...cfg.action_space.levers],
    channels: [...cfg.action_space.channels],
    offers: [...cfg.action_space.offers],
    timings: [...cfg.action_space.timings],
    maxContacts: g.max_contacts_per_window,
    windowDays: g.window_days,
    cooldownHours: g.cooldown_hours,
    startHour: g.allowed_start_hour,
    endHour: g.allowed_end_hour,
    weekdays: [...g.allowed_weekdays],
    rewards: { ...cfg.rewards.points },
    templateId: null,
    templateChannel: null,
    variants: [],
    generation: null,
  };
}

const Ctx = createContext<WizardValue | null>(null);

export function WizardProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<UseCaseConfig | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [step, setStep] = useState(0);
  const [draftId, setDraftId] = useState<string | null>(null);
  const [draftConfig, setDraftConfig] = useState<UseCaseConfig | null>(null);
  const pending = useRef<Promise<string> | null>(null); // one clone at a time, even if called twice

  const load = useCallback(async (alive: () => boolean = () => true) => {
    try {
      const list = await api.listCampaigns();
      const target = list.find((c) => c.active) ?? list[0];
      if (!target) throw new Error("No campaigns found on the server.");
      const cfg = await api.getCampaign(target.use_case_id);
      if (!alive()) return;
      setConfig(cfg);
      setDraft(draftFromConfig(cfg));
      setError(null);
    } catch (e: unknown) {
      if (alive()) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (alive()) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let live = true;
    void load(() => live);
    return () => { live = false; };
  }, [load]);

  const ensureDraft = useCallback((): Promise<string> => {
    if (draftId) return Promise.resolve(draftId);
    if (!config) return Promise.reject(new Error("The campaign is not loaded yet."));
    if (!pending.current) {
      const newId = `${config.use_case_id}_draft_${Date.now().toString(36)}`;
      pending.current = api
        .cloneCampaign(config.use_case_id, newId, `${config.name} (draft)`)
        .then((detail) => {
          setDraftId(detail.campaign.campaign_id);
          setDraftConfig(detail.campaign.config);
          return detail.campaign.campaign_id;
        })
        .finally(() => { pending.current = null; });
    }
    return pending.current;
  }, [draftId, config]);

  const setVariant = useCallback(
    (v: MessageVariant) =>
      setDraft((d) => (d ? { ...d, variants: d.variants.map((x) => (x.variant_id === v.variant_id ? v : x)) } : d)),
    [],
  );

  const patch = useCallback((p: Partial<Draft>) => setDraft((d) => (d ? { ...d, ...p } : d)), []);
  const reset = useCallback(() => {
    // Start over from whatever is live now (it may be the campaign that was just launched).
    setDraftId(null);
    setDraftConfig(null);
    setStep(0);
    setLoading(true);
    void load();
  }, [load]);

  const value = useMemo(
    () => ({ config, draftId, draftConfig, ensureDraft, setVariant, loading, error, draft, step, setStep, patch, reset }),
    [config, draftId, draftConfig, ensureDraft, setVariant, loading, error, draft, step, patch, reset],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWizard(): WizardValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWizard must be used inside WizardProvider");
  return v;
}
