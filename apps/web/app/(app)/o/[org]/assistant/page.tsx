"use client";

import { useParams } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { Badge, Button, Card, ErrorBanner, Field, inputCls, PageHeader } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { AssistantEvent, AssistantInfo, AssistantRun, KnowledgeBase, TicketDraft } from "@/lib/types";

const DEMO_KB = "Harbor & Pine help centre (demo)";
const EXAMPLES = [
  "How many days do I have to ask for a refund?",
  "What's the status of ticket T-1A2B3C4D?",
  "Please open a ticket: my kitchen tap has been dripping since Monday.",
  "Do you clean swimming pools?",
];

function summarizeDetail(e: AssistantEvent): string {
  const d = e.detail;
  const parts: string[] = [];
  const args = d.args as Record<string, unknown> | undefined;
  if (args) parts.push(Object.entries(args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", "));
  if (Array.isArray(d.results)) parts.push(`→ ${(d.results as string[]).join(", ") || "no passages"}`);
  if (typeof d.status === "string") parts.push(`→ ${d.status}`);
  if (typeof d.draft_id === "string") parts.push(`→ draft ${d.draft_id.slice(0, 8)} (needs approval)`);
  if (Array.isArray(d.tool_calls) && d.tool_calls.length) parts.push(`asked for ${(d.tool_calls as string[]).join(", ")}`);
  if (Array.isArray(d.invalid)) parts.push(`removed unverified citations: ${(d.invalid as string[]).join(", ")}`);
  if (typeof d.ticket_id === "string") parts.push(`ticket ${d.ticket_id}`);
  return parts.join(" ");
}

function DraftCard({ draft, org, onChange }: { draft: TicketDraft; org: string; onChange: () => void }) {
  const [subject, setSubject] = useState(draft.subject);
  const [description, setDescription] = useState(draft.description);
  const [current, setCurrent] = useState(draft);
  const [error, setError] = useState<ApiError | string>();
  const [busy, setBusy] = useState(false);
  const edited = subject !== current.subject || description !== current.description;

  async function act(kind: "save" | "approve" | "reject") {
    setBusy(true);
    setError(undefined);
    try {
      const base = `/orgs/${org}/assistant/drafts/${draft.id}`;
      if (kind === "save") {
        setCurrent(await api<TicketDraft>(base, { method: "PATCH", body: { subject, description } }));
      } else if (kind === "approve") {
        // The hash of the version shown here: if it changed elsewhere, the API refuses (409).
        const r = await api<{ draft: TicketDraft }>(`${base}/approve`, {
          method: "POST",
          body: { content_hash: current.content_hash },
        });
        setCurrent(r.draft);
        onChange();
      } else {
        setCurrent(await api<TicketDraft>(`${base}/reject`, { method: "POST" }));
        onChange();
      }
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 rounded-md border border-amber-300 bg-amber-50 p-3" data-testid="ticket-draft">
      <div className="mb-2 flex items-center gap-2 text-sm">
        <b>Ticket draft</b> <Badge value={current.status} />
        {current.ticket_ref && (
          <span className="font-mono" data-testid="ticket-ref">
            opened {current.ticket_ref}
          </span>
        )}
      </div>
      <ErrorBanner error={error} />
      {current.status === "pending" ? (
        <>
          <p className="mb-2 text-xs text-slate-600">
            The assistant prepared this draft. Nothing is opened until you approve it.
          </p>
          <Field label="Subject">
            <input className={inputCls} value={subject} onChange={(e) => setSubject(e.target.value)} />
          </Field>
          <Field label="Description">
            <textarea className={`${inputCls} h-20`} value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <div className="flex gap-2">
            {edited ? (
              <Button onClick={() => act("save")} disabled={busy} variant="secondary">
                Save changes (then review)
              </Button>
            ) : (
              <Button onClick={() => act("approve")} disabled={busy} testId="approve-draft">
                Approve and open ticket
              </Button>
            )}
            <Button variant="danger" onClick={() => act("reject")} disabled={busy} testId="reject-draft">
              Reject
            </Button>
          </div>
        </>
      ) : (
        <p className="text-sm">
          <b>{current.subject}</b>
        </p>
      )}
    </div>
  );
}

function RunCard({ run, org, onChange }: { run: AssistantRun; org: string; onChange: () => void }) {
  const events = useApi<AssistantEvent[]>(`/orgs/${org}/assistant/runs/${run.id}/events`);
  const tools = (events.data ?? []).filter((e) => e.kind !== "provider");
  const cost = run.cost_usd === null ? "unknown" : `$${Number(run.cost_usd).toFixed(5)}`;
  return (
    <Card>
      <div className="mb-3 flex justify-end">
        <div className="max-w-[80%] rounded-lg bg-slate-900 px-3 py-2 text-sm text-white">{run.question}</div>
      </div>
      <div className="mb-2 flex items-center gap-2">
        <Badge value={run.status} />
        <span className="text-xs text-slate-500">
          {run.provider}:{run.model}
          {run.provider === "fixture" && " — rule-based test policy, not a language model"}
        </span>
      </div>
      <div className="whitespace-pre-wrap text-sm" data-testid="assistant-answer">
        {run.answer ?? run.error ?? "(no answer)"}
      </div>
      {run.status === "insufficient_evidence" && (
        <p className="mt-1 text-xs text-amber-700">No verified source supports this answer.</p>
      )}
      {run.citations.length > 0 && (
        <div className="mt-3 text-sm" data-testid="assistant-sources">
          <div className="mb-1 font-medium">Sources</div>
          <ul className="space-y-0.5">
            {run.citations.map((c) => (
              <li key={c.source_id}>
                <span className="font-mono text-xs">{c.source_id}</span> {c.title}
                {c.section ? ` › ${c.section}` : ""}
                {c.version ? <span className="text-xs text-slate-500"> (v{c.version})</span> : null}
              </li>
            ))}
          </ul>
        </div>
      )}
      {run.drafts.map((d) => (
        <DraftCard key={d.id} draft={d} org={org} onChange={() => (events.reload(), onChange())} />
      ))}
      <details className="mt-3 text-xs" data-testid="tool-activity">
        <summary className="cursor-pointer text-slate-600">
          Tool activity ({run.tool_calls} tool call{run.tool_calls === 1 ? "" : "s"})
        </summary>
        <ul className="mt-1 space-y-0.5 font-mono">
          {tools.map((e) => (
            <li key={e.seq}>
              <span className="text-slate-500">{e.kind}</span> <b>{e.name}</b> <Badge value={e.outcome} />{" "}
              {summarizeDetail(e)} {e.latency_ms ? <span className="text-slate-400">{e.latency_ms} ms</span> : null}
            </li>
          ))}
        </ul>
      </details>
      <div className="mt-2 text-xs text-slate-500" data-testid="run-summary">
        {run.turns} model call{run.turns === 1 ? "" : "s"} · {run.retries} retries · {run.input_tokens} in /{" "}
        {run.output_tokens} out tokens · est. cost {cost} · {run.latency_ms} ms total ({run.retrieval_ms} ms retrieval,{" "}
        {run.tool_ms} ms tools) · prompt {run.prompt_version}
      </div>
    </Card>
  );
}

export default function Assistant() {
  const { org } = useParams<{ org: string }>();
  const info = useApi<AssistantInfo>(`/orgs/${org}/assistant`);
  const kbs = useApi<KnowledgeBase[]>(`/orgs/${org}/knowledge-bases`);
  const history = useApi<AssistantRun[]>(`/orgs/${org}/assistant/runs?limit=10`);
  const [kb, setKb] = useState("");
  const [model, setModel] = useState("");
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | string>();
  const [setupNote, setSetupNote] = useState("");

  useEffect(() => {
    const pick = kbs.data?.find((k) => k.name === DEMO_KB) ?? kbs.data?.[0];
    if (!kb && pick) setKb(pick.id);
  }, [kb, kbs.data]);
  useEffect(() => {
    if (!model && info.data) setModel(info.data.default_model);
  }, [model, info.data]);

  async function setup() {
    setBusy(true);
    setError(undefined);
    try {
      const r = await api<{ knowledge_base_id: string; documents: Record<string, number>; demo_ticket_ids: string[] }>(
        `/orgs/${org}/assistant/demo-setup`,
        { method: "POST" },
      );
      setKb(r.knowledge_base_id);
      setSetupNote(
        `Demo content loaded (${r.documents.added} added, ${r.documents.unchanged} unchanged). ` +
          `Documents are indexed in the background. Demo tickets: ${r.demo_ticket_ids.join(", ")}.`,
      );
      kbs.reload();
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  async function ask(e?: FormEvent, text?: string) {
    e?.preventDefault();
    const q = (text ?? question).trim();
    if (!q || !kb) return;
    setBusy(true);
    setError(undefined);
    try {
      await api<AssistantRun>(`/orgs/${org}/assistant/runs`, {
        method: "POST",
        body: { question: q, knowledge_base_id: kb, model: model || undefined },
      });
      setQuestion("");
      history.reload();
    } catch (err) {
      setError(err as ApiError);
    } finally {
      setBusy(false);
    }
  }

  const runs = [...(history.data ?? [])].reverse();
  const hasDemo = kbs.data?.some((k) => k.name === DEMO_KB);
  return (
    <>
      <PageHeader
        title="Assistant"
        subtitle="Harbor & Pine (fictional demo company): sourced answers, ticket lookups, and ticket drafts that a person must approve."
        actions={
          !hasDemo && kbs.data ? (
            <Button onClick={setup} disabled={busy} testId="demo-setup">
              Load demo content
            </Button>
          ) : undefined
        }
      />
      <ErrorBanner error={error ?? info.error ?? kbs.error} />
      {setupNote && <p className="mb-4 text-sm text-emerald-700">{setupNote}</p>}
      {runs.map((r) => (
        <RunCard key={r.id} run={r} org={org} onChange={history.reload} />
      ))}
      <Card>
        <form onSubmit={ask}>
          <div className="mb-2 grid grid-cols-2 gap-3">
            <Field label="Knowledge base">
              <select className={inputCls} value={kb} onChange={(e) => setKb(e.target.value)} data-testid="assistant-kb">
                {kbs.data?.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.name} ({k.embedder})
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Model">
              <select className={inputCls} value={model} onChange={(e) => setModel(e.target.value)} data-testid="assistant-model">
                {info.data?.models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                    {m.startsWith("fixture:") ? " (rule-based test policy)" : ""}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <textarea
            className={`${inputCls} h-20`}
            placeholder="Ask about services, prices or policies, check a ticket (T-1A2B3C4D), or ask to open a ticket"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            data-testid="assistant-question"
          />
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button type="submit" disabled={busy || !kb || !question.trim()} testId="assistant-ask">
              {busy ? "Working…" : "Ask"}
            </Button>
            {EXAMPLES.map((x) => (
              <button
                key={x}
                type="button"
                className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                disabled={busy || !kb}
                onClick={() => ask(undefined, x)}
              >
                {x}
              </button>
            ))}
          </div>
        </form>
      </Card>
    </>
  );
}
