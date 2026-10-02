"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Badge, Button, Card, ErrorBanner, Field, inputCls, PageHeader, Table, Td, when } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type {
  DeploymentDecision,
  EvalCase,
  EvalComparison,
  EvalDataset,
  EvalMetrics,
  EvalRun,
  Workflow,
} from "@/lib/types";

const METRICS: [string, string][] = [
  ["pass_rate", "Pass rate"],
  ["tool_selection_accuracy", "Tool selection"],
  ["structured_output_validity", "Schema valid"],
  ["citation_accuracy", "Citations"],
  ["groundedness_lexical", "Grounded (lexical)"],
  ["latency_p95_ms", "p95 ms"],
  ["cost_per_case_usd", "$/case"],
  ["security_cases_passed", "Security"],
];

function fmt(m: EvalMetrics | null, key: string): string {
  if (!m) return "…";
  const v = m[key];
  if (v === null || v === undefined) return "n/a";
  if (key === "security_cases_passed") return `${v}/${m.security_cases ?? 0}`;
  if (typeof v === "number" && v <= 1 && key !== "latency_p95_ms") return `${(v * 100).toFixed(0)}%`;
  return String(v);
}

const EXAMPLE_CASE = {
  name: "looks up the customer",
  input: { customer: "C-1001" },
  expectations: { status: "succeeded", expected_tools: ["crm.get_customer"] },
  tags: [],
};

function useAction() {
  const [error, setError] = useState<ApiError | string>();
  const [busy, setBusy] = useState(false);
  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(undefined);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof ApiError ? e : String(e));
    } finally {
      setBusy(false);
    }
  }
  return { error, busy, run, setError };
}

function DatasetPanel({ org, ds, wf }: { org: string; ds: EvalDataset; wf: Workflow }) {
  const cases = useApi<EvalCase[]>(`/orgs/${org}/evaluation/datasets/${ds.id}/cases`);
  const runs = useApi<EvalRun[]>(`/orgs/${org}/evaluation/datasets/${ds.id}/runs`, 3000);
  const [caseJson, setCaseJson] = useState(JSON.stringify(EXAMPLE_CASE, null, 2));
  const [version, setVersion] = useState(String(wf.latest_version ?? 1));
  const [selected, setSelected] = useState<string[]>([]);
  const [cmp, setCmp] = useState<EvalComparison>();
  const act = useAction();

  async function addCase() {
    let body: unknown;
    try {
      body = JSON.parse(caseJson);
    } catch {
      act.setError("Case is not valid JSON");
      return;
    }
    await act.run(async () => {
      await api(`/orgs/${org}/evaluation/datasets/${ds.id}/cases`, { method: "POST", body });
      cases.reload();
    });
  }

  useEffect(() => {
    if (selected.length === 0) return setCmp(undefined);
    const qs = selected.map((id) => `run_id=${id}`).join("&");
    api<EvalComparison>(`/orgs/${org}/evaluation/compare?${qs}`).then(setCmp, () => setCmp(undefined));
  }, [org, selected, runs.data]);

  return (
    <Card title={`Dataset: ${ds.name}`} actions={<span className="text-xs text-slate-500">{cases.data?.length ?? 0} cases</span>}>
      <ErrorBanner error={act.error} />
      <div className="mb-4 grid gap-4 md:grid-cols-2">
        <div>
          <Table headers={["Case", "Tags", "Expects"]} empty={cases.data?.length === 0}>
            {cases.data?.map((c) => (
              <tr key={c.id}>
                <Td>{c.name}</Td>
                <Td>{c.tags.join(", ")}</Td>
                <Td mono>{Object.keys(c.expectations).join(", ")}</Td>
              </tr>
            ))}
          </Table>
        </div>
        <div>
          <Field label="Add a case (JSON: name, input, expectations, tags)">
            <textarea
              name="case"
              className={`${inputCls} h-40 font-mono text-xs`}
              value={caseJson}
              onChange={(e) => setCaseJson(e.target.value)}
            />
          </Field>
          <Button onClick={addCase} disabled={act.busy} testId="add-case">
            Add case
          </Button>
        </div>
      </div>

      <div className="mb-3 flex items-end gap-2">
        <Field label="Version">
          <input name="eval-version" className={`${inputCls} w-24`} value={version} onChange={(e) => setVersion(e.target.value)} />
        </Field>
        <div className="mb-3">
          <Button
            testId="run-eval"
            disabled={act.busy}
            onClick={() =>
              act.run(async () => {
                await api(`/orgs/${org}/evaluation/datasets/${ds.id}/runs`, {
                  method: "POST",
                  body: { version: Number(version) },
                });
                runs.reload();
              })
            }
          >
            Run evaluation
          </Button>
        </div>
      </div>

      <Table headers={["", "Version", "Status", ...METRICS.map(([, l]) => l), "Started"]} empty={runs.data?.length === 0}>
        {runs.data?.map((r) => (
          <tr key={r.id} data-testid={`run-v${r.version}`}>
            <Td>
              <input
                type="checkbox"
                aria-label={`compare v${r.version}`}
                disabled={r.status !== "completed"}
                checked={selected.includes(r.id)}
                onChange={(e) =>
                  setSelected((s) => (e.target.checked ? [...s, r.id].slice(-5) : s.filter((x) => x !== r.id)))
                }
              />
            </Td>
            <Td>v{r.version}</Td>
            <Td>
              <Badge value={r.status === "completed" ? "succeeded" : "running"} />
            </Td>
            {METRICS.map(([k]) => (
              <Td key={k} mono>
                <span data-testid={`v${r.version}-${k}`}>{fmt(r.metrics, k)}</span>
              </Td>
            ))}
            <Td>{when(r.created_at)}</Td>
          </tr>
        ))}
      </Table>

      {cmp && (
        <div className="mt-4">
          <h3 className="mb-2 text-sm font-semibold">Per-case comparison</h3>
          <Table headers={["Case", ...cmp.runs.map((r) => `v${r.version}`)]}>
            {Object.entries(cmp.cases).map(([name, byRun]) => (
              <tr key={name}>
                <Td>{name}</Td>
                {cmp.runs.map((r) => (
                  <Td key={r.id}>
                    {byRun[r.id] === undefined ? "—" : <Badge value={byRun[r.id] ? "ok" : "failed"} />}
                  </Td>
                ))}
              </tr>
            ))}
          </Table>
        </div>
      )}
    </Card>
  );
}

function GatePanel({ org, wf, datasets }: { org: string; wf: Workflow; datasets: EvalDataset[] }) {
  const gate = useApi<{ policy: Record<string, unknown> | null }>(`/orgs/${org}/workflows/${wf.id}/gate`);
  const decisions = useApi<DeploymentDecision[]>(`/orgs/${org}/workflows/${wf.id}/deployment-decisions`, 5000);
  const [policy, setPolicy] = useState("");
  const act = useAction();

  useEffect(() => {
    if (!gate.data) return;
    const p = gate.data.policy ?? { dataset_id: datasets[0]?.id ?? "", min_pass_rate: 0.9 };
    setPolicy(JSON.stringify(p, null, 2));
  }, [gate.data, datasets]);

  return (
    <Card
      title="Deployment quality gate"
      actions={<Badge value={gate.data?.policy ? "armed" : "off"} />}
    >
      <ErrorBanner error={act.error} />
      <p className="mb-2 text-sm text-slate-600">
        When armed, a version can only become production if its latest evaluation on the gate dataset passes every
        check below (and does not regress against production). Owners can override with a written, audited reason.
      </p>
      <textarea
        name="gate-policy"
        className={`${inputCls} mb-2 h-36 font-mono text-xs`}
        value={policy}
        onChange={(e) => setPolicy(e.target.value)}
      />
      <div className="mb-4 flex gap-2">
        <Button
          testId="save-gate"
          disabled={act.busy}
          onClick={() =>
            act.run(async () => {
              const body = JSON.parse(policy) as unknown;
              await api(`/orgs/${org}/workflows/${wf.id}/gate`, { method: "PUT", body });
              gate.reload();
            })
          }
        >
          Save gate
        </Button>
        {gate.data?.policy && (
          <Button
            variant="secondary"
            disabled={act.busy}
            onClick={() =>
              act.run(async () => {
                await api(`/orgs/${org}/workflows/${wf.id}/gate`, { method: "DELETE" });
                gate.reload();
              })
            }
          >
            Remove gate
          </Button>
        )}
      </div>
      <h3 className="mb-2 text-sm font-semibold">Gate decisions</h3>
      <Table headers={["When", "Version", "Result", "Checks"]} empty={decisions.data?.length === 0}>
        {decisions.data?.map((d) => (
          <tr key={d.id} data-testid={`decision-v${d.version}`}>
            <Td>{when(d.created_at)}</Td>
            <Td>v{d.version}</Td>
            <Td>
              <Badge value={d.passed ? "approved" : d.overridden ? "external_action" : "denied"} />
              {d.overridden && <div className="mt-1 text-xs text-slate-500">override: {d.override_reason}</div>}
            </Td>
            <Td>
              <ul className="text-xs">
                {d.checks.map((c) => (
                  <li key={c.name} className={c.passed ? "text-emerald-700" : "text-red-700"}>
                    {c.passed ? "✓" : "✗"} {c.name} — {c.detail}
                  </li>
                ))}
              </ul>
            </Td>
          </tr>
        ))}
      </Table>
    </Card>
  );
}

export default function Evaluation() {
  const { org } = useParams<{ org: string }>();
  const workflows = useApi<Workflow[]>(`/orgs/${org}/workflows`);
  const [wfId, setWfId] = useState<string>();
  const wf = workflows.data?.find((w) => w.id === wfId) ?? workflows.data?.[0];
  const datasets = useApi<EvalDataset[]>(wf ? `/orgs/${org}/evaluation/datasets?workflow_id=${wf.id}` : null);
  const [name, setName] = useState("");
  const act = useAction();

  return (
    <>
      <PageHeader
        title="Evaluation"
        subtitle="Run datasets against pinned workflow versions, compare them case by case, and gate deployments on the results. All scores are computed by code from what each run actually did."
      />
      <ErrorBanner error={workflows.error ?? datasets.error ?? act.error} />
      {workflows.data?.length === 0 && <p className="text-sm text-slate-500">Create a workflow first.</p>}
      {wf && (
        <>
          <div className="mb-6 flex items-end gap-3">
            <Field label="Workflow">
              <select name="workflow" className={inputCls} value={wf.id} onChange={(e) => setWfId(e.target.value)}>
                {workflows.data?.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name} (prod v{w.deployed_version ?? "—"}, latest v{w.latest_version ?? "—"})
                  </option>
                ))}
              </select>
            </Field>
            <Field label="New dataset">
              <input name="dataset-name" className={inputCls} value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <div className="mb-3">
              <Button
                testId="create-dataset"
                disabled={act.busy || !name}
                onClick={() =>
                  act.run(async () => {
                    await api(`/orgs/${org}/evaluation/datasets`, { method: "POST", body: { workflow_id: wf.id, name } });
                    setName("");
                    datasets.reload();
                  })
                }
              >
                Create dataset
              </Button>
            </div>
          </div>
          {datasets.data?.map((ds) => (
            <DatasetPanel key={ds.id} org={org} ds={ds} wf={wf} />
          ))}
          {datasets.data && <GatePanel org={org} wf={wf} datasets={datasets.data} />}
        </>
      )}
    </>
  );
}
