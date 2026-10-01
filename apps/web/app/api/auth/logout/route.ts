import { NextResponse } from "next/server";

import { API_BASE_URL, clearTokens, refreshToken } from "@/lib/session";

export async function POST(): Promise<Response> {
  const rt = await refreshToken();
  if (rt) {
    // Revoke the whole login session server-side, not just the cookies.
    await fetch(`${API_BASE_URL}/api/v1/auth/logout`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ refresh_token: rt }),
      cache: "no-store",
    }).catch(() => undefined);
  }
  await clearTokens();
  return NextResponse.json({ ok: true });
}
