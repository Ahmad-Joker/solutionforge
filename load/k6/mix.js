// SolutionForge load test (k6). Run with load/run.sh, which prepends the KB corpus as
// `const CORPUS = {...}` and pipes the script in (no bind mounts needed).
//
//   PROFILE=steady  realistic mix at a fixed arrival rate (does it hold its latency SLO?)
//   PROFILE=stress  read traffic ramped up until latency or errors break (where is the knee?)
//
// Every request is tagged by endpoint so the summary reports per-endpoint percentiles.
import http from "k6/http";
import { check, fail, sleep } from "k6";
import { Counter } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://api:8000";
const API = `${BASE}/api/v1`;
const PROFILE = __ENV.PROFILE || "steady";
const SCALE = Number(__ENV.RATE_SCALE || "1"); // multiplies the steady arrival rates
const executionsStarted = new Counter("executions_started");

const STEADY = {
  reads: { executor: "constant-arrival-rate", rate: Math.max(1, Math.round(40 * SCALE)), timeUnit: "1s", duration: __ENV.DURATION || "3m",
           preAllocatedVUs: 40, maxVUs: 200, exec: "reads" },
  runs: { executor: "constant-arrival-rate", rate: Math.max(1, Math.round(5 * SCALE)), timeUnit: "1s", duration: __ENV.DURATION || "3m",
          preAllocatedVUs: 10, maxVUs: 100, exec: "runs" },
  search: { executor: "constant-arrival-rate", rate: Math.max(1, Math.round(10 * SCALE)), timeUnit: "1s", duration: __ENV.DURATION || "3m",
            preAllocatedVUs: 20, maxVUs: 100, exec: "search" },
};
const STRESS = {
  reads: { executor: "ramping-arrival-rate", startRate: 20, timeUnit: "1s",
           preAllocatedVUs: 100, maxVUs: 600, exec: "reads",
           stages: [ { target: 100, duration: "1m" }, { target: 250, duration: "1m" },
                     { target: 400, duration: "1m" }, { target: 400, duration: "30s" } ] },
};

export const options = {
  scenarios: PROFILE === "stress" ? STRESS : STEADY,
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
  // Declared so the summary contains per-endpoint sub-metrics. The steady profile's
  // targets double as the SLO we check: p95 < 300 ms reads, < 500 ms writes/search.
  thresholds: {
    "http_req_failed": ["rate<0.01"],
    "http_req_duration{name:list_workflows}": ["p(95)<300"],
    "http_req_duration{name:list_executions}": ["p(95)<300"],
    "http_req_duration{name:get_execution}": ["p(95)<300"],
    "http_req_duration{name:start_execution}": ["p(95)<500"],
    "http_req_duration{name:search_hybrid}": ["p(95)<500"],
  },
  setupTimeout: "120s",
};

const WORKFLOW = {
  start: "lookup",
  inputs: { customer: { type: "string" } },
  steps: [
    { id: "lookup", type: "tool",
      config: { tool: "crm.get_customer", args: { customer_ref: "$.input.customer" } }, next: "classify" },
    { id: "classify", type: "llm",
      config: { model: "mock:mock-1", prompt: "Tier: {{ $.steps.lookup.result.tier }}",
                output_schema: { type: "object" } } },
  ],
  output: { tier: "$.steps.lookup.result.tier" },
};

function ok(res, what, status) {
  if (res.status !== status) fail(`${what}: ${res.status} ${res.body}`);
  return res.json();
}

export function setup() {
  const email = `load-${Date.now()}@example.com`;
  const password = `load-test-${Date.now()}-password`;
  const json = { headers: { "content-type": "application/json" } };
  ok(http.post(`${API}/auth/register`, JSON.stringify({ email, password, display_name: "Load" }), json),
     "register", 201);
  const token = ok(http.post(`${API}/auth/login`, JSON.stringify({ email, password }), json),
                   "login", 200).access_token;
  const h = { headers: { "content-type": "application/json", authorization: `Bearer ${token}` } };
  const org = ok(http.post(`${API}/orgs`, JSON.stringify({ name: `Load ${Date.now()}` }), h), "org", 201).id;
  const o = `${API}/orgs/${org}`;
  ok(http.post(`${o}/demo-data`, null, h), "seed", 200);
  const wf = ok(http.post(`${o}/workflows`, JSON.stringify({ name: "load" }), h), "workflow", 201).id;
  ok(http.post(`${o}/workflows/${wf}/versions`, JSON.stringify({ definition: WORKFLOW }), h), "version", 201);
  ok(http.post(`${o}/workflows/${wf}/deployments`, JSON.stringify({ version: 1 }), h), "deploy", 201);
  const kb = ok(http.post(`${o}/knowledge-bases`, JSON.stringify({ name: "support", chunk_size: 400, chunk_overlap: 60 }), h),
                "kb", 201).id;
  for (const d of CORPUS.documents) {
    ok(http.post(`${o}/knowledge-bases/${kb}/documents`,
                 JSON.stringify({ title: d.title, content: d.content, metadata: d.metadata }), h), "doc", 202);
  }
  // Seed a few executions so reads have something to return; wait for ingestion.
  const seeded = [];
  for (let i = 0; i < 5; i++) {
    seeded.push(ok(http.post(`${o}/workflows/${wf}/executions`,
                             JSON.stringify({ input: { customer: "C-1001" } }), h), "seed run", 202).id);
  }
  for (let i = 0; i < 60; i++) {
    const docs = http.get(`${o}/knowledge-bases/${kb}/documents`, h).json();
    if (docs.every((d) => d.status === "ready")) break;
    sleep(1);
  }
  return { o, wf, kb, h, seeded, queries: CORPUS.queries.map((q) => q.q) };
}

export function reads(s) {
  const r1 = http.get(`${s.o}/workflows`, { ...s.h, tags: { name: "list_workflows" } });
  const r2 = http.get(`${s.o}/executions?limit=20`, { ...s.h, tags: { name: "list_executions" } });
  const id = s.seeded[Math.floor(Math.random() * s.seeded.length)];
  const r3 = http.get(`${s.o}/executions/${id}`, { ...s.h, tags: { name: "get_execution" } });
  check(r1, { "200": (r) => r.status === 200 });
  check(r2, { "200": (r) => r.status === 200 });
  check(r3, { "200": (r) => r.status === 200 });
}

export function runs(s) {
  const customers = ["C-1001", "C-1002", "C-1003", "C-1004", "C-1005"];
  const res = http.post(`${s.o}/workflows/${s.wf}/executions`,
    JSON.stringify({ input: { customer: customers[Math.floor(Math.random() * customers.length)] } }),
    { ...s.h, tags: { name: "start_execution" } });
  if (check(res, { "202": (r) => r.status === 202 })) executionsStarted.add(1);
}

export function search(s) {
  const q = s.queries[Math.floor(Math.random() * s.queries.length)];
  const res = http.post(`${s.o}/knowledge-bases/${s.kb}/search`,
    JSON.stringify({ query: q, top_k: 5, strategy: "hybrid" }), { ...s.h, tags: { name: "search_hybrid" } });
  check(res, { "200": (r) => r.status === 200 });
}

export function handleSummary(data) {
  // setup_data holds the load-test user's bearer token: never write it to results.
  const { setup_data: _omit, ...rest } = data;
  return { stdout: JSON.stringify(rest) };
}
