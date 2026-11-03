// The ONLY file that talks to the backend. When Zip 5B lands (campaign
// create/edit), add its calls here and nothing else needs to change.
// (5B is connected now: the placeholder calls are gone.)

import type {
  Campaign, CampaignDetail, CampaignSummary, CustomerState, Decision, DecisionDetail,
  GenerationResult, Health, MessageTemplate, MessageVariant, RespondResult, TemplateLibrary,
  UpliftDimension, UpliftSummary, UseCaseConfig, ValidationOut, VariantReview, VariantStatus,
} from "./types";

const BASE = "/api"; // Vite proxy -> http://127.0.0.1:8000

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError(0, "Backend not reachable. Is uvicorn running on port 8000?");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* keep statusText */ }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

const put = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });

const enc = encodeURIComponent;

export const api = {
  health: () => request<Health>("/health"),
  listCampaigns: () => request<CampaignSummary[]>("/campaigns"),
  getCampaign: (id: string) => request<UseCaseConfig>(`/campaigns/${encodeURIComponent(id)}`),
  seedDemo: (count = 100, seed = 0) =>
    post<{ created: number; first_id: string }>("/demo/seed", { count, seed }),
  listCustomers: (limit = 500) => request<CustomerState[]>(`/customers?limit=${limit}`),
  // Demo only (backend/app/api/demo.py): the fake customer reacts.
  respond: (decisionId: string) => post<RespondResult>("/demo/respond", { decision_id: decisionId }),
  decide: (customerId: string, asOf?: string) =>
    post<Decision>("/decide", { customer_id: customerId, as_of: asOf ?? null }),
  listDecisions: (limit = 50, customerId?: string) =>
    request<Decision[]>(`/decisions?limit=${limit}${customerId ? `&customer_id=${encodeURIComponent(customerId)}` : ""}`),
  getDecision: (id: string) => request<DecisionDetail>(`/decisions/${encodeURIComponent(id)}`),
  getUplift: () => request<UpliftSummary>("/insights/uplift"),
  // Raw on purpose: the tab normalises it, so a small shape difference does not break the page.
  getUpliftBy: (dimension: UpliftDimension) => request<unknown>(`/insights/uplift/by/${dimension}`),
  getMessageFunnel: () => request<unknown>("/insights/messages"),
  getGuardrails: () => request<unknown>("/insights/guardrails"),

  // ---- campaigns (5B). Templates, variants and review only work on a DRAFT campaign. ----
  cloneCampaign: (sourceId: string, newId: string, name?: string) =>
    post<CampaignDetail>(`/campaigns/${enc(sourceId)}/clone`, { new_id: newId, name }),
  updateCampaign: (id: string, config: UseCaseConfig) =>
    put<CampaignDetail>(`/campaigns/${enc(id)}`, config),
  validateCampaign: (id: string) => request<ValidationOut>(`/campaigns/${enc(id)}/validation`),
  activateCampaign: (id: string, replaceActive = false) =>
    post<Campaign>(`/campaigns/${enc(id)}/activate`, { replace_active: replaceActive }),
  archiveCampaign: (id: string) => post<Campaign>(`/campaigns/${enc(id)}/archive`, {}),
  listTemplates: (id: string) => request<MessageTemplate[]>(`/campaigns/${enc(id)}/templates`),
  listVariants: (id: string, templateId?: string, status?: VariantStatus) => {
    const q = [templateId && `template_id=${enc(templateId)}`, status && `status=${status}`]
      .filter(Boolean).join("&");
    return request<MessageVariant[]>(`/campaigns/${enc(id)}/variants${q ? `?${q}` : ""}`);
  },
  getTemplateLibrary: async (id: string): Promise<TemplateLibrary> => {
    const [templates, variants] = await Promise.all([api.listTemplates(id), api.listVariants(id)]);
    return { templates, variants };
  },
  generateVariants: (id: string, templateId: string, count = 5) =>
    post<GenerationResult>(`/campaigns/${enc(id)}/variants/generate`, { template_id: templateId, count }),
  approveVariant: (id: string, variantId: string) =>
    post<VariantReview>(`/campaigns/${enc(id)}/variants/${enc(variantId)}/approve`, {}),
  rejectVariant: (id: string, variantId: string) =>
    post<VariantReview>(`/campaigns/${enc(id)}/variants/${enc(variantId)}/reject`, {}),
};
