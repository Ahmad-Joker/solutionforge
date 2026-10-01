"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Badge, Button, Card, ErrorBanner, inputCls, PageHeader, Table, Td } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { Me, Org } from "@/lib/types";

export default function OrgPicker() {
  const router = useRouter();
  const me = useApi<Me>("/auth/me");
  const orgs = useApi<Org[]>("/orgs");
  const [name, setName] = useState("");
  const [error, setError] = useState<ApiError>();

  async function create() {
    try {
      const org = await api<Org>("/orgs", { method: "POST", body: { name } });
      router.push(`/o/${org.id}`);
    } catch (e) {
      setError(e as ApiError);
    }
  }

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST", headers: { "x-sf-csrf": "1" } });
    router.push("/login");
  }

  return (
    <main className="mx-auto max-w-3xl p-8">
      <PageHeader
        title="Organizations"
        subtitle={me.data ? `Signed in as ${me.data.email}` : undefined}
        actions={
          <Button variant="secondary" onClick={logout}>
            Sign out
          </Button>
        }
      />
      <ErrorBanner error={error ?? orgs.error} />
      <Card title="Your organizations">
        <Table headers={["Name", "Slug", "Role"]} empty={orgs.data?.length === 0}>
          {orgs.data?.map((o) => (
            <tr key={o.id} className="cursor-pointer hover:bg-slate-50" onClick={() => router.push(`/o/${o.id}`)}>
              <Td>{o.name}</Td>
              <Td mono>{o.slug}</Td>
              <Td>
                <Badge value={o.role} />
              </Td>
            </tr>
          ))}
        </Table>
      </Card>
      <Card title="Create organization">
        <div className="flex gap-2">
          <input
            name="org-name"
            className={inputCls}
            placeholder="Acme Support"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <Button onClick={create} disabled={name.trim().length < 2} testId="create-org">
            Create
          </Button>
        </div>
      </Card>
    </main>
  );
}
