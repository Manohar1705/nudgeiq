// Mirrors the backend models in app/models/*. Keep in sync by hand.

export type Channel = "push" | "email" | "sms" | "in_app" | "web" | "retargeting";

export type OutcomeType =
  | "order_placed" | "checkout_restarted" | "cart_reopened"
  | "app_opened" | "cta_clicked" | "message_opened"
  | "refunded" | "unsubscribed" | "bought_elsewhere" | "ignored";

export type VariantStatus = "pending" | "approved" | "rejected";
export type CampaignStatus = "draft" | "active" | "archived";

export interface Action {
  channel: Channel | null;
  timing: string | null;
  offer: string | null;
  lever: string | null;
}

export interface CustomerState {
  customer_id: string;
  cohort_id: string;
  cart_value: number;
  abandoned_minutes: number;
  timing_anchor_minutes: number | null;
  contactable: boolean;
  dnd: boolean;
  has_app: boolean;
  sms_opt_in: boolean;
  email_opt_in: boolean;
  preferred_channel: Channel | null;
  contacts_in_window: number;
  last_contact_at: string | null;
  is_holdout: boolean;
  signals: Record<string, unknown>;
}

export interface ScoredAction { action: Action; score: number }
export interface DecisionStep { agent: string; note: string }

export interface Decision {
  decision_id: string;
  customer_id: string;
  created_at: string;
  state: CustomerState;
  candidates: ScoredAction[];
  blocked: string[];
  chosen: Action;
  explored: boolean;
  is_holdout: boolean;
  bucket: string;
  scheduled_for: string | null;
  variant_id: string | null;
  message: string | null;
  steps: DecisionStep[];
}

export interface Outcome {
  decision_id: string;
  customer_id: string;
  outcome_type: OutcomeType;
  [extra: string]: unknown; // exact fields confirmed in 7A when Live Engine needs them
}

export interface RewardRecord {
  decision_id: string;
  customer_id: string;
  points: number;
  reason: string;
}

export interface DecisionDetail {
  decision: Decision;
  outcomes: Outcome[];
  reward: RewardRecord | null;
}

export interface Cohort { cohort_id: string; label: string; size: number }
export interface ContextSignal { signal_id: string; label: string; enabled: boolean; fields: string[] }

export interface ActionSpace {
  levers: string[];
  channels: Channel[];
  offers: string[];
  timings: string[];
  eligibility: Record<string, string>;
  [extra: string]: unknown;
}

export interface Guardrails {
  max_contacts_per_window: number;
  window_days: number;
  cooldown_hours: number;
  allowed_start_hour: number;
  allowed_end_hour: number;
  allowed_weekdays: number[];
  min_cart_value: number;
  min_abandoned_minutes: number;
  exit_after_days: number;
  [extra: string]: unknown;
}

export interface UseCaseConfig {
  use_case_id: string;
  trigger_event: string;
  name: string;
  cohorts: Cohort[];
  context_signals: ContextSignal[];
  action_space: ActionSpace;
  rewards: { points: Partial<Record<OutcomeType, number>> };
  guardrails: Guardrails;
  learning: {
    epsilon: number;
    holdout_fraction: number;
    variant_selection?: "uniform" | "epsilon_greedy";
    variant_epsilon?: number;
    variant_min_resolved?: number;
  };
}

export interface CampaignSummary {
  use_case_id: string;
  name: string;
  action_space_size: number;
  active_signals: string[];
  active: boolean;
  status: CampaignStatus;
  version: number;
}

export interface GroupStats {
  decisions: number;
  resolved: number;
  orders: number;
  conversion: number;
  avg_reward: number;
}

export interface UpliftSummary {
  treated: GroupStats;
  holdout: GroupStats;
  waited: number;
  uplift: number | null;
  // Optional: shown only if the backend reports a confidence interval on `uplift`.
  ci?: [number, number] | null;
  ci_low?: number | null;
  ci_high?: number | null;
}

// ---- action analytics (4a) ----
export type UpliftDimension = "offer" | "timing" | "channel";

/** One row of /insights/uplift/by/{dimension}: a single offer, timing or channel. */
export interface UpliftSlice {
  key: string;
  decisions: number;
  resolved: number;
  orders: number;
  conversion: number;
  holdout_conversion: number | null;
  uplift: number | null;   // relative uplift vs holdout
  ci_low: number | null;   // confidence interval on `uplift`
  ci_high: number | null;
}

/** One row of /insights/messages: the per-message funnel. See API-CONTRACT-4A.md. */
export interface MessageFunnelRow {
  variant_id: string;
  text: string;
  channel: Channel | null;
  sent: number;
  opened: number;
  clicked: number;
  orders: number;       // "Goal": order_placed
  avg_reward: number;   // "Shaping": mean reward points per sent message
}

/** Outcome counts for one group, from GET /insights/guardrails. See API-CONTRACT-5A.md. */
export interface GuardrailGroup {
  sent: number;
  unsubscribed: number;
  refunded: number;
  app_opened: number;
}

export interface Health {
  status: string;
  [extra: string]: unknown;
}

// ---- templates and variants (6C). Mirrors app/models/variants.py ----
export interface MessageTemplate {
  template_id: string;
  name: string;
  channel: Channel;
  body: string;
  offers: string[];
}

export interface MessageVariant {
  variant_id: string;
  template_id: string;
  text: string;
  principle: string;
  tone: string;
  status: VariantStatus;
  source?: "human" | "llm";
  generated_by?: string | null;
  review_note?: string | null;
}

export interface TemplateLibrary {
  templates: MessageTemplate[];
  variants: MessageVariant[];
}

// ---- campaigns (5B-6). Mirrors app/api/campaigns.py ----
export interface ValidationIssue {
  severity: "error" | "warning";
  code: string;
  path: string;
  message: string;
}

export interface ValidationOut {
  can_activate: boolean;
  error_count: number;
  warning_count: number;
  issues: ValidationIssue[];
}

export interface Campaign {
  campaign_id: string;
  status: CampaignStatus;
  version: number;
  config: UseCaseConfig;
  created_at: string;
  updated_at: string;
  activated_at: string | null;
  archived_at: string | null;
}

export interface CampaignDetail { campaign: Campaign; validation: ValidationOut }

export interface SkippedDraft { text: string; code: string; message: string }

export interface GenerationResult {
  model: string;
  requested: number;
  created: MessageVariant[];
  skipped: SkippedDraft[];
}

export interface VariantReview { variant: MessageVariant; warnings: ValidationIssue[] }

/** What the Launch button produced. `activated` is false when validation blocked it. */
export interface LaunchResult {
  activated: boolean;
  campaign: Campaign | null;
  validation: ValidationOut;
}

// ---- demo respond endpoint (7A). Mirrors backend/app/api/demo.py ----
export interface RespondResult {
  decision_id: string;
  nudged: boolean; // false when the decision was "wait"
  outcomes: OutcomeType[];
  reward: RewardRecord | null;
  policy_updated: boolean;
  next_step: string | null;
}
