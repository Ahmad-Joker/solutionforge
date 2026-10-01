import { NextResponse } from "next/server";

import { API_BASE_URL, forwardedHeaders, setTokens, type TokenPair } from "@/lib/session";

export async function POST(req: Request): Promise<Response> {
  const body = await req.text();
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...forwardedHeaders(req),
    },
    body,
    cache: "no-store",
  });
  const data: unknown = await res.json();
  if (!res.ok) return NextResponse.json(data, { status: res.status });
  await setTokens(data as TokenPair);
  return NextResponse.json({ ok: true });
}
