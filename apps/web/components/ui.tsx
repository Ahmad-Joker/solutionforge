"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import type { ApiError } from "@/lib/client";

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex gap-2">{actions}</div>}
    </div>
  );
}

export function Card({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="mb-6 rounded-lg border border-slate-200 bg-white">
      {(title || actions) && (
        <header className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-700">{title}</h2>
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Button({
  children,
  onClick,
  variant = "primary",
  type = "button",
  disabled,
  testId,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "danger";
  type?: "button" | "submit";
  disabled?: boolean;
  testId?: string;
}) {
  const styles = {
    primary: "bg-slate-900 text-white hover:bg-slate-700",
    secondary: "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50",
    danger: "bg-red-600 text-white hover:bg-red-500",
  }[variant];
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      data-testid={testId}
      className={`rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${styles}`}
    >
      {children}
    </button>
  );
}

const STATUS_COLORS: Record<string, string> = {
  succeeded: "bg-emerald-100 text-emerald-800",
  ready: "bg-emerald-100 text-emerald-800",
  approved: "bg-emerald-100 text-emerald-800",
  ok: "bg-emerald-100 text-emerald-800",
  armed: "bg-emerald-100 text-emerald-800",
  off: "bg-slate-100 text-slate-600",
  running: "bg-blue-100 text-blue-800",
  queued: "bg-slate-100 text-slate-700",
  pending: "bg-amber-100 text-amber-800",
  waiting: "bg-amber-100 text-amber-800",
  ingesting: "bg-blue-100 text-blue-800",
  failed: "bg-red-100 text-red-800",
  rejected: "bg-red-100 text-red-800",
  denied: "bg-red-100 text-red-800",
  budget_exceeded: "bg-orange-100 text-orange-800",
  cancelled: "bg-slate-200 text-slate-600",
  expired: "bg-slate-200 text-slate-600",
  read_only: "bg-slate-100 text-slate-700",
  low_risk_write: "bg-sky-100 text-sky-800",
  external_action: "bg-amber-100 text-amber-800",
  high_risk: "bg-red-100 text-red-800",
};

export function Badge({ value }: { value: string }) {
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${STATUS_COLORS[value] ?? "bg-slate-100 text-slate-700"}`}>
      {value}
    </span>
  );
}

export function ErrorBanner({ error }: { error?: ApiError | string }) {
  if (!error) return null;
  const message = typeof error === "string" ? error : `${error.message}${error.code ? ` (${error.code})` : ""}`;
  return (
    <div role="alert" className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {message}
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return (
    <pre className="max-h-96 overflow-auto rounded bg-slate-50 p-3 text-xs text-slate-800">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Table({ headers, children, empty }: { headers: string[]; children: ReactNode; empty?: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
          <tr>
            {headers.map((h) => (
              <th key={h} className="px-2 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
      {empty && <p className="px-2 py-6 text-center text-sm text-slate-400">Nothing here yet.</p>}
    </div>
  );
}

export function Td({ children, mono }: { children: ReactNode; mono?: boolean }) {
  return <td className={`px-2 py-2 align-top ${mono ? "font-mono text-xs" : ""}`}>{children}</td>;
}

export function A({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className="text-blue-700 hover:underline">
      {children}
    </Link>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="mb-3 block text-sm">
      <span className="mb-1 block font-medium text-slate-700">{label}</span>
      {children}
    </label>
  );
}

export const inputCls =
  "w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-slate-500 focus:outline-none";

export function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}
