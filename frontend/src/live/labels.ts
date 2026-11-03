import type { Action } from "../api/types";
import { CHANNEL_LABELS, humanize } from "../wizard/format";

export function actionTitle(a: Action): string {
  if (!a.channel) return "Wait (no nudge)";
  return `${CHANNEL_LABELS[a.channel] ?? a.channel} · ${a.timing ?? "now"}`;
}

export function actionSub(a: Action): string {
  if (!a.channel) return "No legal or useful action";
  return [a.offer, a.lever].filter((x): x is string => !!x).map(humanize).join(" + ") || "No offer";
}

export function sameAction(a: Action, b: Action): boolean {
  return a.channel === b.channel && a.timing === b.timing && a.offer === b.offer && a.lever === b.lever;
}

export const signed = (n: number) => (n >= 0 ? `+${n}` : `${n}`);
