import { NextResponse } from "next/server";

import { API_BASE_URL, forwardedHeaders, upstreamJson } from "@/lib/session";

export const maxDuration = 60; // a sleeping free-tier API can take ~30-60 s to wake

export async function POST(req: Request): Promise<Response> {
  const body = await req.text();
  const { status, data } = await upstreamJson(() =>
    fetch(`${API_BASE_URL}/api/v1/auth/register`, {
      method: "POST",
      headers: { "content-type": "application/json", ...forwardedHeaders(req) },
      body,
      cache: "no-store",
    }),
  );
  return NextResponse.json(data, { status });
}
