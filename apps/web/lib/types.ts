/** Response shapes of the SolutionForge API (subset used by the dashboard). */

export type Role = "owner" | "admin" | "operator" | "viewer";

export interface Me {
  id: string;
  email: string;
  display_name: string;
}

export interface Org {
  id: string;
  name: string;
  slug: string;
  role: Role;
}

export interface Workflow {
  id: string;
  name: string;
  description: string;
  latest_version: number | null;
  deployed_version: number | null;
  created_at: string;
}

export interface Version {
  id: string;
  version: number;
  definition: Record<string, unknown>;
  definition_hash: string;
  changelog: string;
  created_at: string;
}

export interface Deployment {
  id: string;
  version: number;
  reason: string;
  created_at: string;
}

export interface ExecStep {
  seq: number;
  step_id: string;
  step_type: string;
  attempt: number;
  status: string;
  output: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  started_at: string;
  duration_ms: number;
}

export interface Execution {
  id: string;
  workflow_id: string;
  status: string;
  input: Record<string, unknown>;
  state: Record<string, unknown>;
  output: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  waiting_on: Record<string, unknown> | null;
  current_step: string | null;
  steps_used: number;
  active_ms: number;
  created_at: string;
  finished_at: string | null;
}

export interface ExecutionDetail extends Execution {
  steps: ExecStep[];
  llm_usage: { calls: number; tokens: number; cost_usd: string };
}

export interface Approval {
  id: string;
  execution_id: string;
  step_id: string;
  tool_name: string;
  risk_level: string;
  reason: string;
  required_permission: string;
  proposed_args: Record<string, unknown>;
  approved_args: Record<string, unknown> | null;
  status: string;
  comment: string | null;
  expires_at: string;
  created_at: string;
}

export interface Tool {
  name: string;
  description: string;
  risk_level: string;
  required_permission: string;
  requires_credentials: boolean;
  config_keys: string[];
  installation: { enabled: boolean; auto_approve_low_risk: boolean; has_credentials: boolean } | null;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  embedder: string;
}

export interface Doc {
  id: string;
  title: string;
  status: string;
  chunk_count: number;
  attempts: number;
  last_error: string | null;
}

export interface SearchHit {
  chunk_id: string;
  title: string;
  section: string | null;
  text: string;
  score: number;
  sources: Record<string, number>;
}

export interface UsageRow {
  model: string;
  calls: number;
  failed_calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: string;
  avg_latency_ms: number;
}

export interface Budget {
  daily_limit_usd: string | null;
  monthly_limit_usd: string | null;
  per_execution_limit_usd: string | null;
}

export interface AuditEvent {
  id: string;
  created_at: string;
  event_type: string;
  actor_user_id: string | null;
  resource_type: string | null;
  resource_id: string | null;
  metadata: Record<string, unknown>;
}

export interface EvalDataset {
  id: string;
  workflow_id: string;
  name: string;
  description: string;
  created_at: string;
}

export interface EvalCase {
  id: string;
  name: string;
  input: Record<string, unknown>;
  expectations: Record<string, unknown>;
  tags: string[];
}

export type EvalMetrics = Record<string, number | string | null>;

export interface EvalRun {
  id: string;
  dataset_id: string;
  workflow_id: string;
  version: number;
  status: "running" | "completed";
  metrics: EvalMetrics | null;
  created_at: string;
  finished_at: string | null;
}

export interface EvalComparison {
  runs: { id: string; version: number; status: string; metrics: EvalMetrics | null }[];
  cases: Record<string, Record<string, boolean>>;
}

export interface GateCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface DeploymentDecision {
  id: string;
  version: number;
  candidate_run_id: string | null;
  baseline_run_id: string | null;
  passed: boolean;
  overridden: boolean;
  override_reason: string | null;
  checks: GateCheck[];
  decided_by_user_id: string | null;
  created_at: string;
}
