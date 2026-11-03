/** "free_shipping" -> "Free shipping" */
export function humanize(id: string): string {
  const s = id.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Indian digit grouping, e.g. 412000 -> "4,12,000" */
export function formatCount(n: number): string {
  return n.toLocaleString("en-IN");
}

export const CHANNEL_LABELS: Record<string, string> = {
  push: "Push", email: "Email", sms: "SMS",
  in_app: "In-app", web: "Web on-site", retargeting: "Retargeting",
};

export const OUTCOME_LABELS: Record<string, string> = {
  order_placed: "Order placed",
  checkout_restarted: "Checkout restarted",
  cart_reopened: "Cart reopened",
  app_opened: "App opened from nudge",
  cta_clicked: "Nudge CTA clicked",
  message_opened: "Message opened",
  refunded: "Returned / refunded",
  unsubscribed: "Unsubscribed",
  bought_elsewhere: "Bought elsewhere",
  ignored: "Ignored",
};

// Backend uses Python weekday numbers: Monday = 0 ... Sunday = 6.
export const WEEKDAYS = ["M", "T", "W", "T", "F", "S", "S"] as const;
