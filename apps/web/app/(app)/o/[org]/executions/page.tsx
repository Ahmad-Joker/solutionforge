"use client";

import { useParams } from "next/navigation";
import { useState } from "react";

import { A, Badge, Card, ErrorBanner, PageHeader, Table, Td, when } from "@/components/ui";
import { useApi } from "@/lib/hooks";
import type { Execution } from "@/lib/types";

const STATUSES = ["", "queued", "running", "waiting", "succeeded", "failed", "cancelled", "budget_exceeded"];

export default function Executions() {
  const { org } = useParams<{ org: string }>();
  const [status, setStatus] = useState("");
  const list = useApi<Execution[]>(`/orgs/${org}/executions?limit=100${status ? `&status=${status}` : ""}`, 5000);
  return (
    <>
      <PageHeader title="Executions" subtitle="Refreshes every 5 seconds." />
      <ErrorBanner error={list.error} />
      <Card
        title="Recent"
        actions={
          <select className="rounded border border-slate-300 px-2 py-1 text-sm" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s || "all statuses"}
              </option>
            ))}
          </select>
        }
      >
        <Table headers={["Execution", "Status", "Current step", "Steps", "Active", "Created"]} empty={list.data?.length === 0}>
          {list.data?.map((e) => (
            <tr key={e.id}>
              <Td mono>
                <A href={`/o/${org}/executions/${e.id}`}>{e.id.slice(0, 8)}</A>
              </Td>
              <Td>
                <Badge value={e.status} />
              </Td>
              <Td mono>{e.current_step ?? "—"}</Td>
              <Td>{e.steps_used}</Td>
              <Td>{(e.active_ms / 1000).toFixed(2)} s</Td>
              <Td>{when(e.created_at)}</Td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
