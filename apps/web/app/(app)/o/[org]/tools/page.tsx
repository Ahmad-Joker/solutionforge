"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Badge, Button, Card, ErrorBanner, Field, inputCls, PageHeader, Table, Td } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { Tool } from "@/lib/types";

interface ToolPolicy {
  policy: { auto_allow_up_to: string; blocked_tools: string[]; blocked_risk_levels: string[] };
}

export default function Tools() {
  const { org } = useParams<{ org: string }>();
  const tools = useApi<Tool[]>(`/orgs/${org}/tools`);
  const policy = useApi<ToolPolicy>(`/orgs/${org}/tool-policy`);
  const [blocked, setBlocked] = useState("");
  const [ceiling, setCeiling] = useState("low_risk_write");
  const [error, setError] = useState<ApiError>();
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (policy.data) {
      setBlocked(policy.data.policy.blocked_tools.join(", "));
      setCeiling(policy.data.policy.auto_allow_up_to);
    }
  }, [policy.data]);

  async function toggle(t: Tool) {
    try {
      await api(`/orgs/${org}/tools/${t.name}`, {
        method: "PUT",
        body: { enabled: !(t.installation?.enabled ?? false), auto_approve_low_risk: t.installation?.auto_approve_low_risk ?? true },
      });
      tools.reload();
    } catch (e) {
      setError(e as ApiError);
    }
  }

  async function savePolicy() {
    setSaved(false);
    try {
      await api(`/orgs/${org}/tool-policy`, {
        method: "PUT",
        body: {
          auto_allow_up_to: ceiling,
          blocked_tools: blocked.split(",").map((s) => s.trim()).filter(Boolean),
          blocked_risk_levels: policy.data?.policy.blocked_risk_levels ?? [],
        },
      });
      setSaved(true);
      policy.reload();
    } catch (e) {
      setError(e as ApiError);
    }
  }

  return (
    <>
      <PageHeader title="Tools & policy" subtitle="Risk is declared by each tool; policy decides deterministically." />
      <ErrorBanner error={error ?? tools.error} />
      <Card title="Catalog">
        <Table headers={["Tool", "Risk", "Description", "Credentials", "Enabled"]}>
          {tools.data?.map((t) => (
            <tr key={t.name}>
              <Td mono>{t.name}</Td>
              <Td>
                <Badge value={t.risk_level} />
              </Td>
              <Td>{t.description}</Td>
              <Td>{t.requires_credentials ? (t.installation?.has_credentials ? "configured" : "missing") : "—"}</Td>
              <Td>
                <Button variant="secondary" onClick={() => toggle(t)}>
                  {t.installation?.enabled ? "Disable" : "Enable"}
                </Button>
              </Td>
            </tr>
          ))}
        </Table>
      </Card>
      <Card title="Organization tool policy">
        <Field label="Auto-allow up to">
          <select className={inputCls} value={ceiling} onChange={(e) => setCeiling(e.target.value)}>
            <option value="read_only">read_only (all writes need approval)</option>
            <option value="low_risk_write">low_risk_write</option>
          </select>
        </Field>
        <Field label="Blocked tools (comma-separated)">
          <input className={inputCls} value={blocked} onChange={(e) => setBlocked(e.target.value)} />
        </Field>
        <div className="flex items-center gap-3">
          <Button onClick={savePolicy}>Save policy</Button>
          {saved && <span className="text-sm text-emerald-700">Saved (audited).</span>}
        </div>
      </Card>
    </>
  );
}
