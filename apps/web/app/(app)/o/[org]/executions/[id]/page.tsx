"use client";

import { useParams } from "next/navigation";
import { useState } from "react";

import { A, Badge, Button, Card, ErrorBanner, Json, PageHeader } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { ExecStep, ExecutionDetail } from "@/lib/types";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "budget_exceeded"]);

function Trace({ output }: { output: Record<string, unknown> }) {
  const trace = output.trace as { turn: number; action: string; tool?: string; outcome: string; reason?: string }[] | undefined;
  if (!Array.isArray(trace)) return null;
  return (
    <ol className="mt-2 space-y-1 text-xs">
      {trace.map((t) => (
        <li key={t.turn} className="flex gap-2">
          <span className="w-10 text-slate-400">#{t.turn}</span>
          <span className="w-20 font-mono">{t.action}</span>
          <span className="w-48 font-mono">{t.tool ?? ""}</span>
          <Badge value={t.outcome} />
          <span className="text-slate-500">{t.reason}</span>
        </li>
      ))}
    </ol>
  );
}

function Citations({ output }: { output: Record<string, unknown> }) {
  const cites = output.citations as { label: string; title: string; section: string | null; quote: string }[] | undefined;
  if (!Array.isArray(cites) || cites.length === 0) return null;
  return (
    <ul className="mt-2 space-y-1 text-xs">
      {cites.map((c) => (
        <li key={c.label}>
          <span className="font-mono">[{c.label}]</span> <b>{c.title}</b>
          {c.section ? ` / ${c.section}` : ""} — <span className="text-slate-500">“{c.quote.slice(0, 160)}…”</span>
        </li>
      ))}
    </ul>
  );
}

function StepRow({ s }: { s: ExecStep }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="border-l-2 border-slate-200 pb-4 pl-4" data-testid={`step-${s.step_id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-semibold">{s.step_id}</span>
        <span className="text-xs text-slate-400">{s.step_type}</span>
        <Badge value={s.status} />
        <span className="text-xs text-slate-500">
          attempt {s.attempt} · {s.duration_ms} ms
        </span>
        <button className="text-xs text-blue-700" onClick={() => setOpen(!open)}>
          {open ? "hide" : "details"}
        </button>
      </div>
      {s.error && <p className="mt-1 text-xs text-red-700">{String(s.error.code)}: {String(s.error.message)}</p>}
      {s.output && <Trace output={s.output} />}
      {s.output && <Citations output={s.output} />}
      {open && <Json value={{ output: s.output, error: s.error }} />}
    </li>
  );
}

export default function ExecutionPage() {
  const { org, id } = useParams<{ org: string; id: string }>();
  const ex = useApi<ExecutionDetail>(`/orgs/${org}/executions/${id}`, 3000);
  const [error, setError] = useState<ApiError>();
  const e = ex.data;

  async function cancel() {
    try {
      await api(`/orgs/${org}/executions/${id}/cancel`, { method: "POST" });
      ex.reload();
    } catch (err) {
      setError(err as ApiError);
    }
  }

  return (
    <>
      <PageHeader
        title={`Execution ${id.slice(0, 8)}`}
        subtitle={e ? `${e.steps_used} step attempts · ${(e.active_ms / 1000).toFixed(2)} s active` : undefined}
        actions={
          e && !TERMINAL.has(e.status) ? (
            <Button variant="danger" onClick={cancel}>
              Cancel
            </Button>
          ) : undefined
        }
      />
      <ErrorBanner error={error ?? ex.error} />
      {e && (
        <>
          <div className="mb-6 flex flex-wrap items-center gap-3">
            <span data-testid="execution-status">
              <Badge value={e.status} />
            </span>
            {e.waiting_on && (
              <span className="text-sm text-amber-800">
                Waiting for {String(e.waiting_on.reason)}
                {e.waiting_on.approval_id ? (
                  <>
                    {" "}— <A href={`/o/${org}/approvals`}>open approvals inbox</A>
                  </>
                ) : null}
              </span>
            )}
            <span className="text-sm text-slate-500">
              LLM: {e.llm_usage.calls} calls · {e.llm_usage.tokens} tokens · ${e.llm_usage.cost_usd}
            </span>
          </div>
          <div className="grid gap-6 xl:grid-cols-2">
            <Card title="Timeline">
              <ol>
                {e.steps.map((s) => (
                  <StepRow key={s.seq} s={s} />
                ))}
              </ol>
              {e.steps.length === 0 && <p className="text-sm text-slate-400">No steps yet.</p>}
            </Card>
            <div>
              {e.error && (
                <Card title="Error">
                  <Json value={e.error} />
                </Card>
              )}
              <Card title="Output">
                <Json value={e.output} />
              </Card>
              <Card title="Input">
                <Json value={e.input} />
              </Card>
              <Card title="State">
                <Json value={e.state} />
              </Card>
            </div>
          </div>
        </>
      )}
    </>
  );
}
