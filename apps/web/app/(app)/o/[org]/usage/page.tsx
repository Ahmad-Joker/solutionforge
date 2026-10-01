"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Button, Card, ErrorBanner, Field, inputCls, PageHeader, Table, Td } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { Budget, UsageRow } from "@/lib/types";

export default function Usage() {
  const { org } = useParams<{ org: string }>();
  const usage = useApi<UsageRow[]>(`/orgs/${org}/usage/summary`);
  const budget = useApi<Budget>(`/orgs/${org}/budget`);
  const [form, setForm] = useState({ daily: "", monthly: "", perExecution: "" });
  const [error, setError] = useState<ApiError>();
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (budget.data)
      setForm({
        daily: budget.data.daily_limit_usd ?? "",
        monthly: budget.data.monthly_limit_usd ?? "",
        perExecution: budget.data.per_execution_limit_usd ?? "",
      });
  }, [budget.data]);

  async function save() {
    setSaved(false);
    const v = (s: string) => (s.trim() === "" ? null : s.trim());
    try {
      await api(`/orgs/${org}/budget`, {
        method: "PUT",
        body: { daily_limit_usd: v(form.daily), monthly_limit_usd: v(form.monthly), per_execution_limit_usd: v(form.perExecution) },
      });
      setSaved(true);
      budget.reload();
    } catch (e) {
      setError(e as ApiError);
    }
  }

  const total = (usage.data ?? []).reduce((s, r) => s + Number(r.cost_usd), 0);

  return (
    <>
      <PageHeader title="Usage & budget" subtitle={`LLM spend, last 30 days: $${total.toFixed(6)}`} />
      <ErrorBanner error={error ?? usage.error} />
      <Card title="By model (every attempt, including failures)">
        <Table headers={["Model", "Calls", "Failed", "Input tok", "Output tok", "Cost (USD)", "Avg latency"]} empty={usage.data?.length === 0}>
          {usage.data?.map((r) => (
            <tr key={r.model}>
              <Td mono>{r.model}</Td>
              <Td>{r.calls}</Td>
              <Td>{r.failed_calls}</Td>
              <Td>{r.input_tokens}</Td>
              <Td>{r.output_tokens}</Td>
              <Td mono>{r.cost_usd}</Td>
              <Td>{r.avg_latency_ms} ms</Td>
            </tr>
          ))}
        </Table>
      </Card>
      <Card title="Budget (empty = unlimited)">
        <div className="grid gap-3 md:grid-cols-3">
          <Field label="Daily (USD)">
            <input className={inputCls} value={form.daily} onChange={(e) => setForm({ ...form, daily: e.target.value })} />
          </Field>
          <Field label="Monthly (USD)">
            <input className={inputCls} value={form.monthly} onChange={(e) => setForm({ ...form, monthly: e.target.value })} />
          </Field>
          <Field label="Per execution (USD)">
            <input className={inputCls} value={form.perExecution} onChange={(e) => setForm({ ...form, perExecution: e.target.value })} />
          </Field>
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={save}>Save budget</Button>
          {saved && <span className="text-sm text-emerald-700">Saved (audited).</span>}
        </div>
      </Card>
    </>
  );
}
