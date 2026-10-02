# Observability

There are three signals, joined by a shared trace ID:

- **traces**: OpenTelemetry, exported over OTLP/HTTP;
- **metrics**: Prometheus;
- **logs**: structlog JSON lines.

## Run it locally

```bash
SF_OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318 docker compose --profile observability up --build
```

| UI | URL | Notes |
|---|---|---|
| Grafana | http://localhost:3001 | Anonymous viewers allowed; admin password from `GRAFANA_ADMIN_PASSWORD` (default `admin`, local only). Dashboard: *SolutionForge — overview* |
| Prometheus | http://localhost:9090 | Alert rules from `infra/obs/alerts.yml` |
| Jaeger | http://localhost:16686 | Services `solutionforge-api`, `solutionforge-worker` |

All three UIs are bound to `127.0.0.1` only.

> **Status:** the compose profile and Grafana provisioning are written but **not yet run**,
> because Docker isn't available on the development machine. The pieces that are verified
> are:
> - the emitted metrics and the `/metrics` endpoint (integration tests);
> - the trace structure and span contents (in-memory exporter tests);
> - every dashboard and alert expression referencing a metric that really exists (a unit
>   test).

## Traces

```
POST /api/v1/orgs/{org_id}/workflows/{workflow_id}/executions   (FastAPI server span)
└─ fastapi.endpoint
   └─ workflow.execution            ← worker process, possibly much later
      ├─ workflow.step tool
      │  └─ tool.invoke             (sf.tool, sf.tool.risk, sf.tool.outcome)
      └─ workflow.step llm
         └─ llm.generate            (models, chosen model, cost; one llm.attempt event per try)
```

- **HTTP spans come from FastAPI's built-in OpenTelemetry support.** It handles route
  templates, W3C `traceparent` extraction and query redaction. The app adds no second server
  span.
- **The trace survives the queue.** When an execution is created, the request's
  `traceparent` is stored on the row (`executions.traceparent`, migration 0009). Each time
  a worker runs the execution (first run, retries, resume after an approval), it starts
  `workflow.execution` as a child of that context.
- **Content policy:** spans contain IDs, names, statuses, durations, token counts and costs.
  They **never** contain workflow inputs, tool arguments, prompts or model outputs. A test
  runs a workflow and fails if the customer reference, the prompt text or the model's
  answer appears in any span attribute or event.
- **Tracing is off unless `SF_OTEL_EXPORTER_OTLP_ENDPOINT` is set.** With no exporter, the
  OpenTelemetry API is a no-op.

**Logs.** Every log line written inside a span carries `trace_id` and `span_id`. Search
logs by the trace ID Jaeger shows. The access-log line is written outside FastAPI's span,
so it has `request_id`, `route` and `path` but no trace ID.

## Metrics

| Metric | Type | Labels |
|---|---|---|
| `sf_http_requests_total` | counter | method, route (template), status |
| `sf_http_request_duration_seconds` | histogram | method, route |
| `sf_workflow_executions_finished_total` | counter | status (terminal transitions made by the engine) |
| `sf_workflow_step_duration_seconds` | histogram | step_type, status |
| `sf_workflow_executions` | gauge | status (queued / running / waiting) |
| `sf_workflow_oldest_queued_seconds` | gauge | age of the oldest *runnable* queued execution |
| `sf_llm_calls_total` | counter | provider, model, outcome |
| `sf_llm_tokens_total` | counter | provider, model, direction |
| `sf_llm_cost_usd_total` | counter | provider, model (from the same price table as billing) |
| `sf_llm_call_duration_seconds` | histogram | provider, model |
| `sf_tool_calls_total` | counter | tool, risk, outcome (`succeeded`, `replayed`, or the tool error code, e.g. `tool_denied`, `tool_approval_required`) |
| `sf_tool_call_duration_seconds` | histogram | tool |
| `sf_approvals_pending` | gauge | |
| `sf_documents` | gauge | status (pending / ingesting / failed) |
| `sf_evaluation_runs_running` | gauge | |
| `sf_deployment_gate_decisions_total` | counter | result (passed / blocked / overridden) |

**Label rules:**
- Labels come only from bounded sets: route templates (never raw paths), the tool catalog,
  the price table, and status or error codes defined in code. An unmatched path is recorded
  as `route="unmatched"`.
- **No tenant, user or org identifiers appear in metrics.** A test scrapes `/metrics` after
  real traffic and fails if it finds a UUID, an email or an org name. Another test fails if
  any metric declares a tenant-like label. Per-organization cost and usage live in the
  database and the usage API, behind RBAC.

**Gauges** are sampled every 15 s by a background loop, using aggregate queries across all
tenants. With several workers, every replica reports the same value, so dashboards use
`max()`.

**Scraping:**
- **API:** `GET /metrics` needs `Authorization: Bearer $SF_METRICS_TOKEN`. Without a token
  it's served only in dev/test; in staging or production it returns 404.
- **Workers:** each worker serves metrics on `SF_WORKER_METRICS_PORT`. Keep that port on the
  private network.
- **Multiple API replicas:** each is scraped separately. Prometheus sums them.

## Dashboards and alerts as code

- **Dashboard:** `apps/api/scripts/build_dashboard.py` generates
  `infra/obs/dashboards/solutionforge.json`. A test fails if the committed file is stale or
  if any expression names a metric that doesn't exist. Grafana provisioning sets
  `allowUiUpdates: false`.
- **Alerts:** `infra/obs/alerts.yml`, covering:
  - API 5xx ratio;
  - queue backlog;
  - workflow failure spike;
  - LLM provider errors;
  - LLM spend spike;
  - approvals piling up;
  - dead-lettered documents.

  **The thresholds are starting points, not measured SLOs.** There is no production
  traffic yet to set them from.

## Not done yet

- **Logs** aren't shipped anywhere (they go to stdout). Loki or CloudWatch belongs with the
  cloud deployment phase.
- **Metrics exemplars**, which link a histogram bucket to a trace, aren't wired up yet.
- **No measured latency numbers yet.** Phase 17 (load testing) will produce them.
