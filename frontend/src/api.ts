import { config } from "./config";
import { getToken } from "./auth";

async function request(path: string, options: RequestInit = {}): Promise<any> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string> | undefined ?? {}),
  };
  if (token) headers["Authorization"] = token;

  const resp = await fetch(`${config.apiBase}${path}`, { ...options, headers });
  const text = await resp.text();
  let body: any = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!resp.ok) {
    const err = new Error(body?.message || `HTTP ${resp.status}`);
    (err as any).status = resp.status;
    (err as any).body = body;
    throw err;
  }
  return body;
}

export const api = {
  health: () => request("/healthz"),

  search: (q: string, limit = 20) =>
    request(`/search?q=${encodeURIComponent(q)}&limit=${limit}`),

  suggest: (prefix: string) =>
    request(`/suggest?prefix=${encodeURIComponent(prefix)}`),

  createAsset: (payload: any) =>
    request("/assets", { method: "POST", body: JSON.stringify(payload) }),

  // PUT /assets/{id} — update fields; the backend writes an immutable
  // revision (with the field diff) and bumps the asset's version. Pass
  // changeSource='agent' to tag the revision as agent-assisted (sends the
  // X-Agent-Source header the backend reads); defaults to 'manual'.
  updateAsset: (id: string, payload: any, changeSource: "manual" | "agent" = "manual") =>
    request(`/assets/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
      headers: changeSource === "agent" ? { "X-Agent-Source": "true" } : {},
    }),

  getAsset: (id: string) => request(`/assets/${encodeURIComponent(id)}`),

  // Public (no-auth) browse + read of published assets.
  publicAssets: (q = "", limit = 24) =>
    request(`/public/assets?limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ""}`),

  publicAsset: (id: string) => request(`/public/assets/${encodeURIComponent(id)}`),

  validate: (entity_type: string, payload: any) =>
    request("/validate", {
      method: "POST",
      body: JSON.stringify({ entity_type, payload }),
    }),

  lifecycleAction: (assetId: string, action: "register" | "publish" | "archive" | "unpublish") =>
    request(`/assets/${assetId}/${action}`, { method: "POST", body: JSON.stringify({}) }),

  duplicates: () => request("/duplicates"),

  revisions: (entity_type: string, entity_id: string) =>
    request(`/revisions?entity_type=${encodeURIComponent(entity_type)}&entity_id=${encodeURIComponent(entity_id)}`),

  metricsAssetCounts: () => request("/metrics/asset-counts"),

  metricsValidationDistribution: () => request("/metrics/validation-distribution"),

  agentChat: (message: string, history: any[] = []) =>
    request("/agent/chat", {
      method: "POST",
      body: JSON.stringify({ message, history }),
    }),

  // Public, no-auth MetaMate — published-data-only. Used on the landing page.
  publicAgentChat: (message: string, history: any[] = []) =>
    request("/public/agent/chat", {
      method: "POST",
      body: JSON.stringify({ message, history }),
    }),

  createOrg: (name: string, display_name?: string) =>
    request("/orgs", { method: "POST", body: JSON.stringify({ name, display_name }) }),

  createCollection: (name: string, space_id: string, description?: string) =>
    request("/collections", {
      method: "POST",
      body: JSON.stringify({ name, space_id, description }),
    }),
};
