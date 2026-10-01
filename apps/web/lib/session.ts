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
const secure = process.env.NODE_ENV === "production";

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
