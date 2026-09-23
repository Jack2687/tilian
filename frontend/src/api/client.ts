import type { AdminAnnouncement, AdminCard, AdminConcurrency, AdminLog, AdminOverview, AdminProxyPools, AdminRouteStat, AdminTask, ClusterStatus, MetaResponse, StatsResponse, SuccessTimeline, Task } from "../types";

const API_BASE = import.meta.env.VITE_API_BASE || "/api/v1";
let sessionToken = sessionStorage.getItem("tilian:user-token") || "";

export function setAuthToken(value: string) {
  sessionToken = value;
  if (value) sessionStorage.setItem("tilian:user-token", value);
  else sessionStorage.removeItem("tilian:user-token");
}

export function getAuthToken() {
  return sessionToken;
}

export class ApiError extends Error {
  constructor(message: string, public status: number, public code = "request_failed") {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers || {});
  if (sessionToken && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${sessionToken}`);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item: { msg?: string }) => item.msg || "参数错误").join("；")
      : body.detail;
    throw new ApiError(body.message || body.error || detail || `请求失败 (${response.status})`, response.status, body.code);
  }
  return response.json() as Promise<T>;
}

export const api = {
  register: (value: { username: string; password: string; displayName?: string }) => request<{ token: string; user: import("../types").User }>("/auth/register", { method: "POST", body: JSON.stringify(value) }),
  login: (value: { username: string; password: string }) => request<{ token: string; user: import("../types").User }>("/auth/login", { method: "POST", body: JSON.stringify(value) }),
  me: () => request<{ user: import("../types").User }>("/auth/me"),
  meta: () => request<MetaResponse>("/meta"),
  stats: () => request<StatsResponse>("/stats"),
  statsForMode: (mode: string) => request<StatsResponse>(`/stats?mode=${encodeURIComponent(mode)}`),
  recentSuccess: () => request<SuccessTimeline>("/recent-success"),
  checkCard: (code: string) => request<{ ok: boolean; remain: number; quota: number; status: string; maxConcurrency?: number; cdkConcurrency?: number }>("/cards/check", {
    method: "POST",
    body: JSON.stringify({ code }),
  }),
  fuseCards: (codes: string[], remark = "CDK 融合") => request<{ ok: boolean; card: { code: string; quota: number; remain: number }; fusion: Record<string, unknown> }>("/cards/fuse", {
    method: "POST",
    body: JSON.stringify({ codes, remark }),
  }),
  parseAccounts: (text: string) => request<{ count: number; items: Array<{ at?: string; sessionToken?: string; email?: string; atMask?: string; type?: string; alg?: string; aud?: string; subject?: string; expired?: boolean; exp?: number }> }>("/accounts/parse", {
    method: "POST",
    body: JSON.stringify({ text }),
  }),
  createTask: (payload: Record<string, unknown>, requestId: string) => request<Task>("/tasks", {
    method: "POST",
    headers: { "Idempotency-Key": requestId },
    body: JSON.stringify(payload),
  }),
  listTasks: (cardCode: string) => request<Task[]>("/tasks?limit=30", {
    headers: { "X-Tilian-CDK": cardCode },
  }),
  getTask: (id: string, cardCode: string) => request<Task>(`/tasks/${id}`, {
    headers: { "X-Tilian-CDK": cardCode },
  }),
  cancelTask: (id: string, cardCode: string) => request<Task>(`/tasks/${id}/cancel`, {
    method: "POST",
    headers: { "X-Tilian-CDK": cardCode },
  }),
  submitBlikCode: (id: string, cardCode: string, itemIndex: number, code: string) => request<Task>(`/tasks/${id}/blik/code`, { method: "POST", headers: { "X-Tilian-CDK": cardCode }, body: JSON.stringify({ itemIndex, code }) }),
  refreshQr: (id: string, cardCode: string, itemIndex: number) => request<Task>(`/tasks/${id}/qr/refresh`, { method: "POST", headers: { "X-Tilian-CDK": cardCode }, body: JSON.stringify({ itemIndex }) }),
  adminOverview: (token: string) => request<AdminOverview>("/admin/overview", {
    headers: { Authorization: `Bearer ${token}` },
  }),
  adminCluster: (token: string) => request<ClusterStatus>("/admin/cluster", {
    headers: { Authorization: `Bearer ${token}` },
  }),
  adminCards: (token: string, limit = 100) => request<{ items: AdminCard[]; total: number }>(`/admin/cards?limit=${limit}`, {
    headers: { Authorization: `Bearer ${token}` },
  }),
  adminTasks: (token: string, limit = 100) => request<{ items: AdminTask[]; total: number }>(`/admin/tasks?limit=${limit}`, {
    headers: { Authorization: `Bearer ${token}` },
  }),
  adminUsers: (token: string, params: { q?: string; status?: string; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== "") query.set(key, String(value));
    return request<{ items: import("../types").AdminUser[]; total: number; limit: number; offset: number }>(`/admin/users?${query.toString()}`, { headers: { Authorization: `Bearer ${token}` } });
  },
  adminUsersFiltered: (token: string, params: { q?: string; status?: string; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== "") query.set(key, String(value));
    return request<{ items: import("../types").AdminUser[]; total: number; limit: number; offset: number }>(`/admin/users?${query.toString()}`, { headers: { Authorization: `Bearer ${token}` } });
  },
  updateAdminUser: (token: string, id: number, value: Record<string, unknown>) => request<{ ok: boolean; user: import("../types").AdminUser }>(`/admin/users/${id}`, { method: "PATCH", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(value) }),
  adminUserPolicy: (token: string) => request<import("../types").UserPolicy>("/admin/user-policy", { headers: { Authorization: `Bearer ${token}` } }),
  updateAdminUserPolicy: (token: string, value: import("../types").UserPolicy) => request<import("../types").UserPolicy>("/admin/user-policy", { method: "PUT", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(value) }),
  importAdminCards: (token: string, cards: Array<{ code: string; quota: number }>) => request<{ ok: boolean; created: number; updated: number }>("/admin/cards/import", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: JSON.stringify({ cards }),
  }),
  generateAdminCards: (token: string, value: { count: number; quota: number; days: number; remark: string; fusionDisabled: boolean }) => request<{ created: number; cards: Array<{ code: string; quota: number; days: number; expiresAt: string }> }>("/admin/cards/generate", { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(value) }),
  adminFuseCards: (token: string, codes: string[], remark: string) => request<{ card: { code: string; quota: number; remain: number } }>("/admin/cards/fuse", { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify({ codes, remark }) }),
  adminCardFusions: (token: string) => request<{ total: number; records: Array<{ id: string; newCode: string; sourceCodes: string[]; totalRemaining: number; remark: string; actor: string; createdAt: string }> }>("/admin/card-fusions", { headers: { Authorization: `Bearer ${token}` } }),
  adminConcurrency: (token: string) => request<AdminConcurrency>("/admin/concurrency", { headers: { Authorization: `Bearer ${token}` } }),
  updateAdminConcurrency: (token: string, value: AdminConcurrency) => request<AdminConcurrency>("/admin/concurrency", { method: "PUT", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(value) }),
  adminProxies: (token: string) => request<AdminProxyPools>("/admin/proxies", { headers: { Authorization: `Bearer ${token}` } }),
  updateAdminProxies: (token: string, value: AdminProxyPools) => request<AdminProxyPools>("/admin/proxies", { method: "PUT", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(value) }),
  adminAnnouncement: (token: string) => request<{ announcement: AdminAnnouncement | null }>("/admin/announcement", { headers: { Authorization: `Bearer ${token}` } }),
  publishAdminAnnouncement: (token: string, value: Pick<AdminAnnouncement, "title" | "content">) => request<{ announcement: AdminAnnouncement }>("/admin/announcement", { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(value) }),
  removeAdminAnnouncement: (token: string) => request<{ deleted: number }>("/admin/announcement", { method: "DELETE", headers: { Authorization: `Bearer ${token}` } }),
  adminLogs: (token: string) => request<{ total: number; logs: AdminLog[] }>("/admin/logs?limit=200", { headers: { Authorization: `Bearer ${token}` } }),
  clearAdminLogs: (token: string) => request<{ deleted: number }>("/admin/logs/clear", { method: "POST", headers: { Authorization: `Bearer ${token}` } }),
  adminRouteStats: (token: string) => request<{ items: AdminRouteStat[] }>("/admin/route-stats", { headers: { Authorization: `Bearer ${token}` } }),
  resetAdminStats: (token: string) => request<{ ok: boolean }>("/admin/danger/reset-stats", { method: "POST", headers: { Authorization: `Bearer ${token}` } }),
  updateCardStatus: (token: string, id: number, value: { status?: string; fusionDisabled?: boolean }) => request<{ ok: boolean; status: string; fusionDisabled: boolean }>(`/admin/cards/${id}`, { method: "PATCH", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(value) }),
  announcement: () => request<{ announcement: AdminAnnouncement | null }>("/announcement"),
  clearTasks: (cardCode: string) => request<{ cleared: number }>("/tasks", { method: "DELETE", headers: { "X-Tilian-CDK": cardCode } }),
  checkProxy: (proxy: string) => request<{ ok: boolean; proxy: string; ip?: string; latencyMs?: number; error?: string }>("/proxy/check", { method: "POST", body: JSON.stringify({ proxy }) }),
  plusRecords: (limit = 30) => request<{ records: Array<{ email: string; t: string; taskId: string }> }>(`/plus-records?limit=${limit}`),
  paymentConfig: () => request<{ enabled: boolean; plans: Array<{ id: string; label: string; quota: number; money: number }> }>("/payment/config"),
  taskEvents: async (id: string, cardCode: string) => {
    const headers = new Headers({ "X-Tilian-CDK": cardCode });
    if (sessionToken) headers.set("Authorization", `Bearer ${sessionToken}`);
    const response = await fetch(`${API_BASE}/tasks/${id}/events`, { headers });
    if (!response.ok) throw new ApiError("事件读取失败", response.status);
    const text = await response.text();
    return text.split(/\n/).filter((line) => line.startsWith("data: ")).map((line) => JSON.parse(line.slice(6))) as Array<{ eventId: string; type: string; itemIndex?: number; payload?: Record<string, unknown>; createdAt: string }>;
  },
  createPayment: (planId: string, type: string) => request<{ payUrl: string; order: { outTradeNo: string } }>("/payment/create", { method: "POST", body: JSON.stringify({ planId, type }) }),
  paymentOrder: (orderId: string) => request<{ order: Record<string, unknown> }>(`/payment/order?order_id=${encodeURIComponent(orderId)}`),
};
