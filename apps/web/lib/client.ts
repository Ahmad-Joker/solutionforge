"use client";

/** Browser-side API client. Talks only to the same-origin BFF proxy (cookies + CSRF header). */

export interface ApiErrorBody {
  error?: { code?: string; message?: string; details?: unknown; request_id?: string };
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = init.method ?? "GET";
  const res = await fetch(`/api/sf${path}`, {
    method,
    headers: {
      ...(init.body !== undefined ? { "content-type": "application/json" } : {}),
      ...(method !== "GET" ? { "x-sf-csrf": "1" } : {}),
    },
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && typeof window !== "undefined") {
    window.location.href = "/login";
    throw new ApiError(401, "unauthenticated", "Session expired");
  }
  if (res.status === 204) return undefined as T;
  const data: unknown = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = (data as ApiErrorBody).error ?? {};
    throw new ApiError(res.status, e.code ?? "error", e.message ?? `HTTP ${res.status}`, e.details);
  }
  return data as T;
}

export async function postJson<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", "x-sf-csrf": "1" },
    body: JSON.stringify(body),
  });
  const data: unknown = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = (data as ApiErrorBody).error ?? {};
    throw new ApiError(res.status, e.code ?? "error", e.message ?? `HTTP ${res.status}`, e.details);
  }
  return data as T;
}
