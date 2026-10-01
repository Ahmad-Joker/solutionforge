"use client";

import { useParams } from "next/navigation";
import { useState } from "react";

import { A, Badge, Button, Card, ErrorBanner, PageHeader, Table, Td, when } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { Approval, Execution, UsageRow, Workflow } from "@/lib/types";

function Stat({ label, value, testId }: { label: string; value: string | number; testId?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="text-xs uppercase text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold" data-testid={testId}>
        {value}
      </div>
    </div>
  );
}

export default function Overview() {
  const { org } = useParams<{ org: string }>();
  const workflows = useApi<Workflow[]>(`/orgs/${org}/workflows`);
  const executions = useApi<Execution[]>(`/orgs/${org}/executions?limit=10`, 5000);
  const approvals = useApi<Approval[]>(`/orgs/${org}/approvals?status=pending`, 5000);
  const usage = useApi<UsageRow[]>(`/orgs/${org}/usage/summary`);
  const [seedMsg, setSeedMsg] = useState<string>();
  const [error, setError] = useState<ApiError>();

  const cost = (usage.data ?? []).reduce((sum, r) => sum + Number(r.cost_usd), 0);

  async function seed() {
    try {
      const r = await api<{ customers: number; orders: number; skipped: boolean }>(`/orgs/${org}/demo-data`, {
        method: "POST",
      });
      setSeedMsg(r.skipped ? "Demo data already present." : `Seeded ${r.customers} customers, ${r.orders} orders.`);
    } catch (e) {
      setError(e as ApiError);
    }
  }

  return (
    <>
      <PageHeader
        title="Overview"
        actions={
          <Button variant="secondary" onClick={seed} testId="seed-demo">
            Seed demo data
          </Button>
        }
      />
      <ErrorBanner error={error} />
      {seedMsg && <p className="mb-4 text-sm text-emerald-700">{seedMsg}</p>}
      <div className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Workflows" value={workflows.data?.length ?? "…"} />
        <Stat label="Pending approvals" value={approvals.data?.length ?? "…"} testId="stat-pending" />
        <Stat label="Recent executions" value={executions.data?.length ?? "…"} />
        <Stat label="LLM spend (30 d)" value={`$${cost.toFixed(4)}`} />
      </div>
      <Card title="Recent executions">
        <Table headers={["Execution", "Status", "Step", "Created"]} empty={executions.data?.length === 0}>
          {executions.data?.map((e) => (
            <tr key={e.id}>
              <Td mono>
                <A href={`/o/${org}/executions/${e.id}`}>{e.id.slice(0, 8)}</A>
              </Td>
              <Td>
                <Badge value={e.status} />
              </Td>
              <Td mono>{e.current_step ?? "—"}</Td>
              <Td>{when(e.created_at)}</Td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
