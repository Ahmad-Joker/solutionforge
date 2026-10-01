import { NextResponse } from "next/server";

import { API_BASE_URL, forwardedHeaders } from "@/lib/session";

export async function POST(req: Request): Promise<Response> {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/register`, {
    method: "POST",
    headers: { "content-type": "application/json", ...forwardedHeaders(req) },
    body: await req.text(),
    cache: "no-store",
  });
  return NextResponse.json(await res.json(), { status: res.status });
}
