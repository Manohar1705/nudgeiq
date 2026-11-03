// Turns the raw JSON from the analytics endpoints into the shapes the Action tab draws.
// The endpoints were written before this tab, so this reader is deliberately forgiving:
// it accepts a bare array or { rows | items | groups }, flat or nested treated/holdout
// fields, and ci_low/ci_high, lower/upper or ci: [low, high]. Rows it cannot read are dropped.

import type { Channel, GuardrailGroup, MessageFunnelRow, UpliftSlice } from "../api/types";

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v.length > 0 ? v : null);
const firstNum = (o: Obj, keys: string[]): number | null => {
  for (const k of keys) { const n = num(o[k]); if (n !== null) return n; }
  return null;
};

function rowsOf(raw: unknown): Obj[] {
  const list = Array.isArray(raw)
    ? raw
    : isObj(raw)
      ? (["rows", "items", "groups", "slices", "data"].map((k) => raw[k]).find(Array.isArray) as unknown[] | undefined) ?? []
      : [];
  return list.filter(isObj);
}

export function normalizeSlices(raw: unknown): UpliftSlice[] {
  const out: UpliftSlice[] = [];
  for (const r of rowsOf(raw)) {
    const key = str(r.key) ?? str(r.name) ?? str(r.label) ?? str(r.value) ?? str(r.offer) ?? str(r.timing) ?? str(r.channel);
    if (!key) continue;
    const t = isObj(r.treated) ? r.treated : r;
    const h = isObj(r.holdout) ? r.holdout : null;
    let low = firstNum(r, ["ci_low", "lower", "uplift_low"]);
    let high = firstNum(r, ["ci_high", "upper", "uplift_high"]);
    if (Array.isArray(r.ci) && r.ci.length === 2) { low = num(r.ci[0]); high = num(r.ci[1]); }
    out.push({
      key,
      decisions: firstNum(t, ["decisions", "n"]) ?? 0,
      resolved: firstNum(t, ["resolved"]) ?? 0,
      orders: firstNum(t, ["orders"]) ?? 0,
      conversion: firstNum(t, ["conversion"]) ?? 0,
      holdout_conversion: h ? firstNum(h, ["conversion"]) : firstNum(r, ["holdout_conversion"]),
      uplift: firstNum(r, ["uplift", "relative_uplift"]),
      ci_low: low,
      ci_high: high,
    });
  }
  return out;
}

export function normalizeFunnel(raw: unknown): MessageFunnelRow[] {
  const out: MessageFunnelRow[] = [];
  for (const r of rowsOf(raw)) {
    const id = str(r.variant_id);
    if (!id) continue;
    out.push({
      variant_id: id,
      text: str(r.text) ?? id,
      channel: (str(r.channel) as Channel | null) ?? null,
      sent: firstNum(r, ["sent"]) ?? 0,
      opened: firstNum(r, ["opened"]) ?? 0,
      clicked: firstNum(r, ["clicked"]) ?? 0,
      orders: firstNum(r, ["orders", "goal"]) ?? 0,
      avg_reward: firstNum(r, ["avg_reward", "shaping"]) ?? 0,
    });
  }
  return out;
}

const group = (v: unknown): GuardrailGroup | null => {
  if (!isObj(v)) return null;
  const sent = num(v.sent);
  if (sent === null) return null;
  return { sent, unsubscribed: num(v.unsubscribed) ?? 0, refunded: num(v.refunded) ?? 0, app_opened: num(v.app_opened) ?? 0 };
};

/** { treated: {...}, holdout: {...} } -> typed groups. Holdout may be missing; treated may not. */
export function normalizeGuardrails(raw: unknown): { treated: GuardrailGroup; holdout: GuardrailGroup | null } | null {
  if (!isObj(raw)) return null;
  const treated = group(raw.treated);
  return treated ? { treated, holdout: group(raw.holdout) } : null;
}
