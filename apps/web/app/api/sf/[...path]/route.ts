/**
 * Authenticated proxy: /api/sf/<path> → API /api/v1/<path>.
 * Attaches the access token from the httpOnly cookie, refreshes once on 401 (rotation),
 * and enforces the CSRF header on mutating methods.
 */
import { NextResponse } from "next/server";

import { accessToken, API_BASE_URL, forwardedHeaders, tryRefresh } from "@/lib/session";

type Ctx = { params: Promise<{ path: string[] }> };
const SAFE = new Set(["GET", "HEAD"]);
const SEGMENT = /^[A-Za-z0-9._-]+$/;

async function forward(req: Request, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  if (!path.every((p) => SEGMENT.test(p))) {
    return NextResponse.json({ error: { code: "bad_path", message: "Invalid path" } }, { status: 400 });
  }
  if (!SAFE.has(req.method) && req.headers.get("x-sf-csrf") !== "1") {
    return NextResponse.json({ error: { code: "csrf", message: "Missing CSRF header" } }, { status: 403 });
  }
  const search = new URL(req.url).search;
  const url = `${API_BASE_URL}/api/v1/${path.join("/")}${search}`;
  const body = SAFE.has(req.method) ? undefined : await req.text();

  const call = (token: string | undefined) =>
    fetch(url, {
      method: req.method,
      headers: {
        ...(body !== undefined ? { "content-type": "application/json" } : {}),
        ...(token ? { authorization: `Bearer ${token}` } : {}),
        ...forwardedHeaders(req),
      },
      body,
      cache: "no-store",
    });

  let res = await call(await accessToken());
  if (res.status === 401) {
    const fresh = await tryRefresh();
    if (fresh) res = await call(fresh);
  }
  const text = await res.text();
  return new Response(res.status === 204 ? null : text, {
    status: res.status,
    headers: { "content-type": res.headers.get("content-type") ?? "application/json" },
  });
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
