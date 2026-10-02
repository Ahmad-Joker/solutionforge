"""Post-deploy smoke test: is a deployed environment actually working end to end?

    python scripts/smoke.py https://staging.example.com [--metrics-token TOKEN] [--dev]

Creates a throwaway user and organization, seeds the simulated systems, then deploys and
runs a workflow that exercises the API, the database, the worker (the execution must be
picked up and finish), a tool call, and an LLM call through the mock provider (no spend).
Also checks the security posture that must hold in every environment: no token is
readable without auth, /metrics is not public, and security headers are present.

Exit code 0 = healthy. Anything else = do not promote / roll back.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from typing import Any

import httpx

WORKFLOW = {
    "start": "lookup",
    "inputs": {"customer": {"type": "string"}},
    "steps": [
        {
            "id": "lookup",
            "type": "tool",
            "config": {"tool": "crm.get_customer", "args": {"customer_ref": "$.input.customer"}},
            "next": "classify",
        },
        {
            "id": "classify",
            "type": "llm",
            "config": {
                "model": "mock:mock-1",
                "prompt": "Tier: {{ $.steps.lookup.result.tier }}",
                "output_schema": {"type": "object"},
            },
        },
    ],
    "output": {"tier": "$.steps.lookup.result.tier"},
}


class SmokeFailure(Exception):
    pass


def check(cond: bool, what: str) -> None:
    if not cond:
        raise SmokeFailure(what)
    print(f"  ok  {what}")


def run(base: str, metrics_token: str | None, timeout_s: float, dev: bool = False) -> None:
    api = f"{base.rstrip('/')}/api/v1"
    with httpx.Client(timeout=15, follow_redirects=False) as c:
        r = c.get(f"{base}/healthz")
        check(r.status_code == 200, "liveness /healthz")
        r = c.get(f"{base}/readyz")
        check(r.status_code == 200, f"readiness /readyz (database) -> {r.status_code}")
        check(r.headers.get("x-content-type-options") == "nosniff", "security headers present")
        check(c.get(f"{api}/orgs").status_code == 401, "API requires authentication")

        if not dev:  # dev/test environments serve /metrics openly by design
            check(c.get(f"{base}/metrics").status_code == 404, "/metrics is not public")
        if metrics_token:
            r = c.get(f"{base}/metrics", headers={"Authorization": f"Bearer {metrics_token}"})
            check(
                r.status_code == 200 and "sf_http_requests_total" in r.text,
                "/metrics reachable with the scrape token",
            )

        email = f"smoke-{uuid.uuid4().hex[:12]}@example.com"
        password = uuid.uuid4().hex + "Aa1!"
        r = c.post(
            f"{api}/auth/register",
            json={"email": email, "password": password, "display_name": "Smoke Test"},
        )
        check(r.status_code == 201, f"register -> {r.status_code}")
        r = c.post(f"{api}/auth/login", json={"email": email, "password": password})
        check(r.status_code == 200, "login")
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        org = _ok(
            c.post(f"{api}/orgs", json={"name": f"Smoke {email[6:18]}"}, headers=h),
            "create organization",
        )["id"]
        o = f"{api}/orgs/{org}"
        _ok(c.post(f"{o}/demo-data", headers=h), "seed simulated systems")
        wf = _ok(c.post(f"{o}/workflows", json={"name": "smoke"}, headers=h), "create workflow")[
            "id"
        ]
        _ok(
            c.post(f"{o}/workflows/{wf}/versions", json={"definition": WORKFLOW}, headers=h),
            "create version",
        )
        _ok(
            c.post(f"{o}/workflows/{wf}/deployments", json={"version": 1}, headers=h),
            "deploy version",
        )
        ex = _ok(
            c.post(
                f"{o}/workflows/{wf}/executions", json={"input": {"customer": "C-1001"}}, headers=h
            ),
            "start execution",
        )["id"]

        deadline = time.monotonic() + timeout_s
        status = "queued"
        while time.monotonic() < deadline:
            body = _ok(c.get(f"{o}/executions/{ex}", headers=h), None)
            status = body["status"]
            if status not in ("queued", "running"):
                break
            time.sleep(1)
        check(status == "succeeded", f"worker ran the execution (status={status})")
        check(body["output"]["tier"] in ("standard", "gold", "platinum"), "tool output flowed")
        check(body["llm_usage"]["calls"] >= 1, "LLM call metered")

        other = c.get(f"{api}/orgs/{uuid.uuid4()}/workflows", headers=h)
        check(other.status_code in (403, 404), "foreign organization is not accessible")


def _ok(r: httpx.Response, what: str | None) -> Any:
    if r.status_code >= 300:
        raise SmokeFailure(f"{what or r.request.url.path} -> {r.status_code}: {r.text[:300]}")
    if what:
        print(f"  ok  {what}")
    return r.json()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("base_url")
    p.add_argument("--metrics-token")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--dev", action="store_true", help="target is a dev/test environment")
    args = p.parse_args()
    print(f"smoke test: {args.base_url}")
    try:
        run(args.base_url, args.metrics_token, args.timeout, dev=args.dev)
    except (SmokeFailure, httpx.HTTPError) as exc:
        print(f"  FAIL {exc}")
        return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
