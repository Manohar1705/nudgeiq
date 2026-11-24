import type { OutcomeType } from "../api/types";
import type { Draft } from "./WizardContext";

// Must match RewardConfig in the backend: these outcomes are zero or negative.
export const NEGATIVE_OUTCOMES: OutcomeType[] = ["refunded", "unsubscribed", "bought_elsewhere", "ignored"];
export const POSITIVE_OUTCOMES: OutcomeType[] = [
  "order_placed", "checkout_restarted", "cart_reopened", "app_opened", "cta_clicked", "message_opened",
];

export function rewardTotals(rewards: Draft["rewards"]) {
  const sum = (keys: OutcomeType[]) => keys.reduce((a, k) => a + (rewards[k] ?? 0), 0);
  return { positive: sum(POSITIVE_OUTCOMES), penalty: sum(NEGATIVE_OUTCOMES) };
}

/** Returns a list of problems; empty list = step is valid. step is 0-based. */
export function validateStep(step: number, d: Draft): string[] {
  const errs: string[] = [];
  if (step === 0) {
    if (!d.cohortId) errs.push("Pick a cohort to continue.");
  }
  if (step === 1) {
    if (!Object.values(d.signals).some(Boolean)) errs.push("Select at least one context signal.");
  }
  if (step === 2) {
    if (!d.levers.length) errs.push("Select at least one recovery lever.");
    if (!d.channels.length) errs.push("Select at least one channel.");
    if (!d.offers.length) errs.push("Select at least one offer.");
    if (!d.timings.length) errs.push("Select at least one timing slot.");
    if (!d.weekdays.length) errs.push("Select at least one day of the week.");
    if (d.startHour >= d.endHour) errs.push("Allowed hours: start must be before end.");
    if (!Number.isInteger(d.maxContacts) || d.maxContacts < 0) errs.push("Max contacts must be 0 or more.");
    if (!Number.isInteger(d.windowDays) || d.windowDays < 1) errs.push("Window days must be 1 or more.");
    if (!Number.isInteger(d.cooldownHours) || d.cooldownHours < 0) errs.push("Cool-down must be 0 or more.");
  }
  if (step === 3) {
    for (const k of POSITIVE_OUTCOMES) {
      if ((d.rewards[k] ?? 0) < 0) errs.push(`${k.replace(/_/g, " ")} must be zero or positive.`);
    }
    for (const k of NEGATIVE_OUTCOMES) {
      if ((d.rewards[k] ?? 0) > 0) errs.push(`${k.replace(/_/g, " ")} must be zero or negative.`);
    }
    if ((d.rewards.order_placed ?? 0) <= 0) errs.push("Order placed needs a positive reward.");
  }
  if (step === 4) {
    if (!d.templateId || d.variants.length === 0) errs.push("Pick a template and generate its variations.");
  }
  if (step === 5) {
    const approved = d.variants.filter((v) => v.status === "approved");
    if (approved.length === 0) errs.push("Approve at least one variation to launch.");
    if (d.templateChannel === "sms" && approved.some((v) => !/\bSTOP\b/i.test(v.text))) {
      errs.push("Approved SMS variations must include an opt-out (Reply STOP).");
    }
  }
  return errs;
}
