"use client";

import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { Button, Card, ErrorBanner, Field, inputCls, Json, PageHeader, Table, Td, when } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { Deployment, Execution, Version, Workflow } from "@/lib/types";

const STARTER = JSON.stringify(
  {
    start: "greet",
    inputs: { name: { type: "string" } },
    steps: [{ id: "greet", type: "transform", config: { set: { message: "Hello {{ $.input.name }}" } } }],
    output: { message: "$.state.message" },
  },
  null,
  2,
);

export default function WorkflowDetail() {
  const { org, id } = useParams<{ org: string; id: string }>();
  const router = useRouter();
  const base = `/orgs/${org}/workflows/${id}`;
  const wf = useApi<Workflow>(base);
  const versions = useApi<Version[]>(`${base}/versions`);
  const deployments = useApi<Deployment[]>(`${base}/deployments`);
  const [definition, setDefinition] = useState(STARTER);
  const [changelog, setChangelog] = useState("");
  const [input, setInput] = useState('{\n  "name": "Ada"\n}');
  const [error, setError] = useState<ApiError | string>();
  const [selected, setSelected] = useState<Version>();

  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setError(undefined);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof ApiError ? e : String(e));
      return undefined;
    }
  }

  async function createVersion() {
    let parsed: unknown;
    try {
      parsed = JSON.parse(definition);
    } catch {
      setError("Definition is not valid JSON");
      return;
    }
    await run(() => api(`${base}/versions`, { method: "POST", body: { definition: parsed, changelog } }));
    versions.reload();
    wf.reload();
  }

  async function deploy(version: number) {
    await run(() => api(`${base}/deployments`, { method: "POST", body: { version, reason: "deployed from dashboard" } }));
    deployments.reload();
    wf.reload();
  }

  async function execute() {
    let parsed: unknown;
    try {
      parsed = JSON.parse(input);
    } catch {
      setError("Input is not valid JSON");
      return;
    }
    const ex = await run(() => api<Execution>(`${base}/executions`, { method: "POST", body: { input: parsed } }));
    if (ex) router.push(`/o/${org}/executions/${ex.id}`);
  }

  const details = error instanceof ApiError && error.details ? <Json value={error.details} /> : null;

  return (
    <>
      <PageHeader
        title={wf.data?.name ?? "Workflow"}
        subtitle={wf.data ? `Production: ${wf.data.deployed_version ? `v${wf.data.deployed_version}` : "none"}` : undefined}
      />
      <ErrorBanner error={error} />
      {details}
      <div className="grid gap-6 xl:grid-cols-2">
        <div>
          <Card title="Versions (immutable)">
            <Table headers={["Version", "Created", "Changelog", ""]} empty={versions.data?.length === 0}>
              {versions.data?.map((v) => (
                <tr key={v.id}>
                  <Td>
                    <button className="text-blue-700 hover:underline" onClick={() => setSelected(v)}>
                      v{v.version}
                    </button>
                  </Td>
                  <Td>{when(v.created_at)}</Td>
                  <Td>{v.changelog}</Td>
                  <Td>
                    {wf.data?.deployed_version === v.version ? (
                      <span className="text-xs text-emerald-700">live</span>
                    ) : (
                      <Button variant="secondary" onClick={() => deploy(v.version)} testId={`deploy-v${v.version}`}>
                        Deploy
                      </Button>
                    )}
                  </Td>
                </tr>
              ))}
            </Table>
          </Card>
          <Card title="Run production version">
            <Field label="Input (JSON)">
              <textarea name="input" className={`${inputCls} h-28 font-mono`} value={input} onChange={(e) => setInput(e.target.value)} />
            </Field>
            <Button onClick={execute} disabled={!wf.data?.deployed_version} testId="run">
              Run
            </Button>
          </Card>
          <Card title="Deployment history">
            <Table headers={["Version", "When", "Reason"]} empty={deployments.data?.length === 0}>
              {deployments.data?.map((d) => (
                <tr key={d.id}>
                  <Td>v{d.version}</Td>
                  <Td>{when(d.created_at)}</Td>
                  <Td>{d.reason}</Td>
                </tr>
              ))}
            </Table>
          </Card>
        </div>
        <div>
          <Card title="New version">
            <Field label="Definition (JSON)">
              <textarea
                name="definition"
                className={`${inputCls} h-80 font-mono text-xs`}
                value={definition}
                onChange={(e) => setDefinition(e.target.value)}
              />
            </Field>
            <Field label="Changelog">
              <input className={inputCls} value={changelog} onChange={(e) => setChangelog(e.target.value)} />
            </Field>
            <Button onClick={createVersion} testId="create-version">
              Create version
            </Button>
          </Card>
          {selected && (
            <Card title={`v${selected.version} definition`} actions={<span className="font-mono text-xs text-slate-400">{selected.definition_hash.slice(0, 12)}</span>}>
              <Json value={selected.definition} />
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
