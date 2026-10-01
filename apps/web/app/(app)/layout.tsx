import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

/** Server-side gate: no session cookie → login. (The API remains the authority on every call.) */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const jar = await cookies();
  if (!jar.get("sf_access") && !jar.get("sf_refresh")) redirect("/login");
  return <>{children}</>;
}
