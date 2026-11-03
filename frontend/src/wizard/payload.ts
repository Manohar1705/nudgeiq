import type { OutcomeType, UseCaseConfig } from "../api/types";
import type { Draft } from "./WizardContext";

/**
 * The wizard's choices merged into the draft campaign's own config.
 * `base` must be the DRAFT's config (its use_case_id is the draft's id), not the live one.
 *
 * Not saved: the cohort picked in step 1. The campaign config has no "target cohort" field,
 * so that choice is only used for the summary on the last screen.
 */
export function buildDraftConfig(base: UseCaseConfig, d: Draft): UseCaseConfig {
  const points = { ...base.rewards.points };
  for (const [k, v] of Object.entries(d.rewards)) {
    if (typeof v === "number") points[k as OutcomeType] = v;
  }
  const options = new Set([...d.channels, ...d.offers, ...d.levers]);
  const eligibility = Object.fromEntries(
    Object.entries(base.action_space.eligibility ?? {}).filter(([option]) => options.has(option)),
  );
  return {
    ...base,
    context_signals: base.context_signals.map((s) => ({ ...s, enabled: d.signals[s.signal_id] ?? s.enabled })),
    action_space: {
      ...base.action_space,
      levers: d.levers, channels: d.channels, offers: d.offers, timings: d.timings, eligibility,
    },
    guardrails: {
      ...base.guardrails,
      max_contacts_per_window: d.maxContacts,
      window_days: d.windowDays,
      cooldown_hours: d.cooldownHours,
      allowed_start_hour: d.startHour,
      allowed_end_hour: d.endHour,
      allowed_weekdays: d.weekdays,
    },
    rewards: { points },
  };
}
