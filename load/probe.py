"""Sequential latency probe: one request at a time per endpoint (no contention)."""

import json
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18000"
API = f"{BASE}/api/v1"
CORPUS = json.loads((Path(__file__).parents[1] / "apps/api/tests/fixtures/support_kb.json").read_text())
WF = {"start": "lookup", "inputs": {"customer": {"type": "string"}},
      "steps": [{"id": "lookup", "type": "tool",
                 "config": {"tool": "crm.get_customer", "args": {"customer_ref": "$.input.customer"}}}]}

c = httpx.Client(timeout=120)
email, pw = f"probe-{uuid.uuid4().hex[:8]}@example.com", uuid.uuid4().hex + "Aa1!"
c.post(f"{API}/auth/register", json={"email": email, "password": pw, "display_name": "P"}).raise_for_status()
h = {"Authorization": f"Bearer {c.post(f'{API}/auth/login', json={'email': email, 'password': pw}).json()['access_token']}"}
o = f"{API}/orgs/{c.post(f'{API}/orgs', json={'name': 'Probe ' + email[6:14]}, headers=h).json()['id']}"
c.post(f"{o}/demo-data", headers=h).raise_for_status()
wf = c.post(f"{o}/workflows", json={"name": "probe"}, headers=h).json()["id"]
c.post(f"{o}/workflows/{wf}/versions", json={"definition": WF}, headers=h).raise_for_status()
c.post(f"{o}/workflows/{wf}/deployments", json={"version": 1}, headers=h).raise_for_status()
kb = c.post(f"{o}/knowledge-bases", json={"name": "kb"}, headers=h).json()["id"]
for d in CORPUS["documents"]:
    c.post(f"{o}/knowledge-bases/{kb}/documents", json={"title": d["title"], "content": d["content"]}, headers=h)
for _ in range(60):
    if all(d["status"] == "ready" for d in c.get(f"{o}/knowledge-bases/{kb}/documents", headers=h).json()):
        break
    time.sleep(1)
ex = c.post(f"{o}/workflows/{wf}/executions", json={"input": {"customer": "C-1001"}}, headers=h).json()["id"]

probes = {
    "healthz": lambda: c.get(f"{BASE}/healthz"),
    "list_workflows": lambda: c.get(f"{o}/workflows", headers=h),
    "list_executions": lambda: c.get(f"{o}/executions?limit=20", headers=h),
    "get_execution": lambda: c.get(f"{o}/executions/{ex}", headers=h),
    "start_execution": lambda: c.post(f"{o}/workflows/{wf}/executions", json={"input": {"customer": "C-1002"}}, headers=h),
    "search_keyword": lambda: c.post(f"{o}/knowledge-bases/{kb}/search", json={"query": "refund delay", "strategy": "keyword"}, headers=h),
    "search_dense": lambda: c.post(f"{o}/knowledge-bases/{kb}/search", json={"query": "refund delay", "strategy": "dense"}, headers=h),
    "search_hybrid": lambda: c.post(f"{o}/knowledge-bases/{kb}/search", json={"query": "refund delay", "strategy": "hybrid"}, headers=h),
}
print("| endpoint | median ms | max ms (20 sequential calls) |\n|---|---|---|")
for name, call in probes.items():
    times = []
    for _ in range(20):
        t = time.perf_counter()
        r = call()
        times.append((time.perf_counter() - t) * 1000)
        assert r.status_code < 300, (name, r.status_code, r.text[:200])
    print(f"| {name} | {statistics.median(times):.1f} | {max(times):.1f} |")
