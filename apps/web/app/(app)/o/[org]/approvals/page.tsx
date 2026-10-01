"use client";

import { useParams } from "next/navigation";
import { useState } from "react";

import { A, Badge, Button, Card, ErrorBanner, Field, inputCls, Json, PageHeader, when } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { Approval } from "@/lib/types";

function ApprovalCard({ a, org, onDone }: { a: Approval; org: string; onDone: () => void }) {
  const [args, setArgs] = useState(JSON.stringify(a.proposed_args, null, 2));
  const [comment, setComment] = useState("");
  const [error, setError] = useState<ApiError | string>();
  const [busy, setBusy] = useState(false);
  const modified = args.trim() !== JSON.stringify(a.proposed_args, null, 2).trim();

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setError(undefined);
    let parsed: unknown;
    if (decision === "approve" && modified) {
      try {
        parsed = JSON.parse(args);
      } catch {
        setError("Arguments are not valid JSON");
        setBusy(false);
        return;
      }
    }
    try {
      await api(`/orgs/${org}/approvals/${a.id}/decision`, {
        method: "POST",
        body: { decision, comment: comment || undefined, ...(parsed !== undefined ? { args: parsed } : {}) },
      });
      onDone();
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title={`${a.tool_name}`}
      actions={
        <div className="flex items-center gap-2">
          <Badge value={a.risk_level} />
          <span className="text-xs text-slate-500">expires {when(a.expires_at)}</span>
        </div>
      }
    >
      <ErrorBanner error={error} />
      <p className="mb-2 text-sm">
        <b>Why:</b> {a.reason} · <b>Needs:</b> <span className="font-mono">{a.required_permission}</span> · <b>From:</b>{" "}
        <A href={`/o/${org}/executions/${a.execution_id}`}>execution {a.execution_id.slice(0, 8)}</A> / step{" "}
        <span className="font-mono">{a.step_id}</span>
      </p>
      <Field label={modified ? "Arguments (modified — will be re-validated)" : "Proposed arguments (edit to modify)"}>
        <textarea name="args" className={`${inputCls} h-32 font-mono text-xs`} value={args} onChange={(e) => setArgs(e.target.value)} />
      </Field>
      <Field label="Comment">
        <input className={inputCls} value={comment} onChange={(e) => setComment(e.target.value)} />
      </Field>
      <div className="flex gap-2">
        <Button onClick={() => decide("approve")} disabled={busy} testId={`approve-${a.id}`}>
          {modified ? "Approve with changes" : "Approve"}
        </Button>
        <Button variant="danger" onClick={() => decide("reject")} disabled={busy} testId={`reject-${a.id}`}>
          Reject
        </Button>
      </div>
    </Card>
  );
}

export default function Approvals() {
  const { org } = useParams<{ org: string }>();
  const pending = useApi<Approval[]>(`/orgs/${org}/approvals?status=pending`, 5000);
  const decided = useApi<Approval[]>(`/orgs/${org}/approvals?limit=20`);
  const reload = () => {
    pending.reload();
    decided.reload();
  };
  return (
    <>
      <PageHeader title="Approvals" subtitle="Actions waiting for a human decision. Decisions are final and audited." />
      <ErrorBanner error={pending.error} />
      {pending.data?.length === 0 && <p className="mb-6 text-sm text-slate-500">No pending approvals.</p>}
      {pending.data?.map((a) => (
        <ApprovalCard key={a.id} a={a} org={org} onDone={reload} />
      ))}
      <Card title="Recent decisions">
        {decided.data
          ?.filter((a) => a.status !== "pending")
          .map((a) => (
            <div key={a.id} className="flex items-center gap-3 border-b border-slate-100 py-2 text-sm">
              <Badge value={a.status} />
              <span className="font-mono">{a.tool_name}</span>
              <span className="text-slate-500">{a.comment}</span>
              {a.approved_args && <Json value={a.approved_args} />}
            </div>
          ))}
      </Card>
    </>
  );
}
