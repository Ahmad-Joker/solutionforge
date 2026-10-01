"use client";

import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { A, Button, Card, ErrorBanner, Field, inputCls, PageHeader, Table, Td } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { Workflow } from "@/lib/types";

export default function Workflows() {
  const { org } = useParams<{ org: string }>();
  const router = useRouter();
  const list = useApi<Workflow[]>(`/orgs/${org}/workflows`);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<ApiError>();

  async function create() {
    try {
      const wf = await api<Workflow>(`/orgs/${org}/workflows`, { method: "POST", body: { name, description } });
      router.push(`/o/${org}/workflows/${wf.id}`);
    } catch (e) {
      setError(e as ApiError);
    }
  }

  return (
    <>
      <PageHeader title="Workflows" subtitle="Versioned definitions; production = the deployed version." />
      <ErrorBanner error={error ?? list.error} />
      <Card title="All workflows">
        <Table headers={["Name", "Latest", "Deployed", "Description"]} empty={list.data?.length === 0}>
          {list.data?.map((w) => (
            <tr key={w.id}>
              <Td>
                <A href={`/o/${org}/workflows/${w.id}`}>{w.name}</A>
              </Td>
              <Td>{w.latest_version ? `v${w.latest_version}` : "—"}</Td>
              <Td>{w.deployed_version ? `v${w.deployed_version}` : "not deployed"}</Td>
              <Td>{w.description}</Td>
            </tr>
          ))}
        </Table>
      </Card>
      <Card title="New workflow">
        <Field label="Name">
          <input name="workflow-name" className={inputCls} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Description">
          <input className={inputCls} value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Button onClick={create} disabled={name.trim().length < 2} testId="create-workflow">
          Create
        </Button>
      </Card>
    </>
  );
}
