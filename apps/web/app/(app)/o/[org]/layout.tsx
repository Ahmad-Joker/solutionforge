"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { useApi } from "@/lib/hooks";
import type { Approval, Org } from "@/lib/types";

const NAV = [
  ["", "Overview"],
  ["/workflows", "Workflows"],
  ["/executions", "Executions"],
  ["/approvals", "Approvals"],
  ["/knowledge", "Knowledge"],
  ["/tools", "Tools & policy"],
  ["/usage", "Usage & budget"],
  ["/audit", "Audit log"],
] as const;

export default function OrgLayout({ children }: { children: ReactNode }) {
  const { org } = useParams<{ org: string }>();
  const path = usePathname();
  const info = useApi<Org>(`/orgs/${org}`);
  const pending = useApi<Approval[]>(`/orgs/${org}/approvals?status=pending`, 15000);
  const base = `/o/${org}`;
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-slate-200 bg-white p-4">
        <Link href="/" className="mb-1 block text-xs text-slate-500 hover:underline">
          ← All organizations
        </Link>
        <div className="mb-6 truncate font-semibold" data-testid="org-name">
          {info.data?.name ?? "…"}
        </div>
        <nav className="space-y-1 text-sm">
          {NAV.map(([href, label]) => {
            const full = base + href;
            const active = href === "" ? path === base : path.startsWith(full);
            const count = href === "/approvals" ? (pending.data?.length ?? 0) : 0;
            return (
              <Link
                key={href}
                href={full}
                className={`flex justify-between rounded px-2 py-1.5 ${
                  active ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100"
                }`}
              >
                <span>{label}</span>
                {count > 0 && (
                  <span className="rounded bg-amber-400 px-1.5 text-xs font-semibold text-slate-900" data-testid="pending-count">
                    {count}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>
      </aside>
      <main className="min-w-0 flex-1 p-8">{children}</main>
    </div>
  );
}
