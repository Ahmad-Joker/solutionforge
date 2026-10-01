"use client";

import { useParams } from "next/navigation";

import { Card, ErrorBanner, PageHeader, Table, Td, when } from "@/components/ui";
import { useApi } from "@/lib/hooks";
import type { AuditEvent } from "@/lib/types";

export default function Audit() {
  const { org } = useParams<{ org: string }>();
  const events = useApi<AuditEvent[]>(`/orgs/${org}/audit-events?limit=200`);
  return (
    <>
      <PageHeader title="Audit log" subtitle="Append-only. Secrets are redacted before storage." />
      <ErrorBanner error={events.error} />
      <Card>
        <Table headers={["When", "Event", "Resource", "Metadata"]} empty={events.data?.length === 0}>
          {events.data?.map((e) => (
            <tr key={e.id}>
              <Td>{when(e.created_at)}</Td>
              <Td mono>{e.event_type}</Td>
              <Td mono>
                {e.resource_type}
                {e.resource_id ? `:${e.resource_id.slice(0, 8)}` : ""}
              </Td>
              <Td mono>{JSON.stringify(e.metadata).slice(0, 160)}</Td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
