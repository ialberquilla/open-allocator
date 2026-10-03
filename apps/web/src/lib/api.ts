// The typed client for oa_server. Same origin: the browser sends the token
// cookie itself, and no token is ever visible to this code.

import type { components } from "@/lib/api-types";

type Schemas = components["schemas"];

export type PlanStatus = Schemas["PlanSummary"]["status"];
export type PlanSummary = Schemas["PlanSummary"];
export type PlanResponse = Schemas["PlanResponse"];
export type ExecuteReview = Schemas["ExecuteReview"];
export type ReviewBundle = Schemas["ReviewBundle"];
export type TokenAmount = Schemas["TokenAmount"];
export type ApproveResponse = Schemas["ApproveResponse"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code: string | null,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}) },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = (body as { detail?: unknown; error?: unknown } | null) ?? {};
    const reason =
      typeof detail.detail === "object" && detail.detail !== null
        ? (detail.detail as { error?: string; code?: string })
        : { error: typeof detail.error === "string" ? detail.error : undefined };
    throw new ApiError(
      response.status,
      reason.error ?? `${response.status} ${response.statusText}`,
      "code" in reason ? (reason.code ?? null) : null,
    );
  }
  return body as T;
}

export const api = {
  plans: () => request<PlanSummary[]>("/api/plans"),
  plan: (hash: string) => request<PlanResponse>(`/api/plans/${hash}`),
  approve: (hash: string) =>
    request<ApproveResponse>("/api/approve", {
      method: "POST",
      body: JSON.stringify({ plan_hash: hash }),
    }),
  reject: (hash: string) =>
    request<Schemas["RejectResponse"]>("/api/reject", {
      method: "POST",
      body: JSON.stringify({ plan_hash: hash }),
    }),
};
