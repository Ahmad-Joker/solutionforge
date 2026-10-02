/**
 * Server-only token handling for the backend-for-frontend.
 *
 * Access and refresh tokens live in httpOnly, SameSite=Lax cookies set by Next.js route
 * handlers: browser JavaScript never sees them (an XSS bug cannot exfiltrate tokens).
 * Mutating requests through the proxy must carry `x-sf-csrf: 1`; a cross-site form cannot
 * set custom headers, and cross-site fetch would trigger CORS preflight that we never allow.
 */
import "server-only";

import { cookies } from "next/headers";

export const API_BASE_URL = process.env.SF_API_URL ?? "http://localhost:8000";
const ACCESS = "sf_access";
const REFRESH = "sf_refresh";
// Secure cookies by default in production; SF_COOKIE_SECURE=false only for plain-http test
// servers (the E2E suite). Never disable it behind a real domain.
const secure = (process.env.SF_COOKIE_SECURE ?? (process.env.NODE_ENV === "production" ? "true" : "false")) === "true";

export interface TokenPair {
  access_token: string;
  access_expires_at: string;
  refresh_token: string;
  refresh_expires_at: string;
}

export async function setTokens(t: TokenPair): Promise<void> {
  const jar = await cookies();
  const base = { httpOnly: true, secure, sameSite: "lax" as const, path: "/" };
  jar.set(ACCESS, t.access_token, { ...base, expires: new Date(t.access_expires_at) });
  // Refresh token is only ever needed by the BFF routes.
  jar.set(REFRESH, t.refresh_token, { ...base, path: "/api", expires: new Date(t.refresh_expires_at) });
}

export async function clearTokens(): Promise<void> {
  const jar = await cookies();
  jar.delete(ACCESS);
  jar.delete({ name: REFRESH, path: "/api" });
}

export async function accessToken(): Promise<string | undefined> {
  return (await cookies()).get(ACCESS)?.value;
}

export async function refreshToken(): Promise<string | undefined> {
  return (await cookies()).get(REFRESH)?.value;
}

/** Rotate tokens once using the refresh cookie. Returns the new access token or undefined. */
export async function tryRefresh(): Promise<string | undefined> {
  const rt = await refreshToken();
  if (!rt) return undefined;
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/refresh`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ refresh_token: rt }),
    cache: "no-store",
  });
  if (!res.ok) {
    await clearTokens();
    return undefined;
  }
  const pair = (await res.json()) as TokenPair;
  await setTokens(pair);
  return pair.access_token;
}

/**
 * Headers propagated to the API. X-Forwarded-For carries the end user's IP so per-IP rate
 * limits work behind the BFF; the API trusts it only from the BFF's address
 * (uvicorn --forwarded-allow-ips). The edge load balancer must overwrite client-supplied
 * X-Forwarded-For, otherwise per-IP limits can be evaded (per-account limits still apply).
 */
export function forwardedHeaders(req: Request): Record<string, string> {
  const out: Record<string, string> = {};
  for (const name of ["x-request-id", "x-forwarded-for"]) {
    const value = req.headers.get(name);
    if (value) out[name] = value;
  }
  return out;
}

/**
 * Free hosting sleeps when idle; the first request may hit a cold API (slow, or a non-JSON
 * error page from the platform). Callers get a clean 503 envelope instead of a crash.
 */
export const API_UNAVAILABLE = {
  error: {
    code: "api_unavailable",
    message: "The API is starting up (free hosting sleeps when idle). Please retry in about 30 seconds.",
  },
};

export async function upstreamJson(
  call: () => Promise<Response>,
): Promise<{ ok: boolean; status: number; data: unknown }> {
  let res: Response;
  try {
    res = await call();
  } catch {
    return { ok: false, status: 503, data: API_UNAVAILABLE };
  }
  const type = res.headers.get("content-type") ?? "";
  if (!type.includes("json")) {
    return { ok: false, status: res.status >= 500 ? 503 : res.status, data: API_UNAVAILABLE };
  }
  return { ok: res.ok, status: res.status, data: await res.json() };
}
