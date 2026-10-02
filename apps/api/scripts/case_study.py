"""Run a customer case study (cases/<name>/) end to end through the public API.

    python scripts/case_study.py support  http://localhost:8000
    python scripts/case_study.py research http://localhost:8000 --json results.json

Creates a throwaway user and organization, seeds the simulated systems (and, for
`research`, a knowledge base from the benchmark corpus), creates and deploys the workflow,
loads the evaluation dataset, runs it, and prints the measured metrics and every case.
The same function drives tests/integration/test_case_studies.py in-process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[3]
CASES = ROOT / "cases"
Drain = Callable[[], Awaitable[None]]


async def _ok(r: httpx.Response, status: int, what: str) -> Any:
    if r.status_code != status:
        raise RuntimeError(f"{what}: {r.status_code} {r.text[:300]}")
    return r.json()


async def _wait(
    drain: Drain | None, check: Callable[[], Awaitable[bool]], wait_seconds: float
) -> None:
    deadline = time.monotonic() + wait_seconds
    while True:
        if drain is not None:
            await drain()
        if await check():
            return
        if time.monotonic() > deadline:
            raise TimeoutError("timed out waiting")
        await asyncio.sleep(0 if drain is not None else 1)


async def run_case_study(
    c: httpx.AsyncClient,
    headers: dict[str, str],
    org_url: str,
    name: str,
    *,
    drain: Drain | None = None,
    wait_seconds: float = 180,
) -> dict[str, Any]:
    """Set up and evaluate one case study inside an existing organization."""
    folder = CASES / name
    definition = json.loads((folder / "workflow.json").read_text(encoding="utf-8"))
    dataset = json.loads((folder / "dataset.json").read_text(encoding="utf-8"))
    o, h = org_url, headers

    await _ok(await c.post(f"{o}/demo-data", headers=h), 200, "seed demo data")
    # Credentials for the simulated email provider (sending still requires human approval).
    await _ok(
        await c.put(
            f"{o}/tools/email.send_message",
            headers=h,
            json={
                "config": {"from_address": "support@acme.example"},
                "credentials": {"api_key": "simulated-provider-key"},
            },
        ),
        200,
        "configure email tool",
    )

    if "corpus" in dataset:
        corpus = json.loads((ROOT / dataset["corpus"]).read_text(encoding="utf-8"))
        kb = (
            await _ok(
                await c.post(
                    f"{o}/knowledge-bases",
                    headers=h,
                    json={"name": "policies", "chunk_size": 400, "chunk_overlap": 60},
                ),
                201,
                "kb",
            )
        )["id"]
        for d in corpus["documents"]:
            await _ok(
                await c.post(
                    f"{o}/knowledge-bases/{kb}/documents",
                    headers=h,
                    json={"title": d["title"], "content": d["content"], "metadata": d["metadata"]},
                ),
                202,
                "document",
            )

        async def ingested() -> bool:
            docs = (await c.get(f"{o}/knowledge-bases/{kb}/documents", headers=h)).json()
            return all(d["status"] == "ready" for d in docs)

        await _wait(drain, ingested, wait_seconds)

    wf = (
        await _ok(
            await c.post(f"{o}/workflows", headers=h, json={"name": f"case-{name}"}),
            201,
            "workflow",
        )
    )["id"]
    await _ok(
        await c.post(f"{o}/workflows/{wf}/versions", headers=h, json={"definition": definition}),
        201,
        "version",
    )
    await _ok(
        await c.post(f"{o}/workflows/{wf}/deployments", headers=h, json={"version": 1}),
        201,
        "deploy",
    )
    ds = (
        await _ok(
            await c.post(
                f"{o}/evaluation/datasets",
                headers=h,
                json={"workflow_id": wf, "name": dataset["name"]},
            ),
            201,
            "dataset",
        )
    )["id"]
    for case in dataset["cases"]:
        await _ok(
            await c.post(
                f"{o}/evaluation/datasets/{ds}/cases",
                headers=h,
                json={
                    "name": case["name"][:120],
                    "input": case["input"],
                    "expectations": case["expectations"],
                    "tags": case.get("tags", []),
                },
            ),
            201,
            "case",
        )
    run = (
        await _ok(
            await c.post(f"{o}/evaluation/datasets/{ds}/runs", headers=h, json={"version": 1}),
            202,
            "run",
        )
    )["id"]

    async def finished() -> bool:
        status: str = (await c.get(f"{o}/evaluation/runs/{run}", headers=h)).json()["status"]
        return status == "completed"

    await _wait(drain, finished, wait_seconds)
    detail: dict[str, Any] = await _ok(
        await c.get(f"{o}/evaluation/runs/{run}", headers=h), 200, "run detail"
    )
    cases = {
        x["id"]: x["name"]
        for x in await _ok(
            await c.get(f"{o}/evaluation/datasets/{ds}/cases", headers=h), 200, "cases"
        )
    }
    detail["case_names"] = {r["case_id"]: cases.get(r["case_id"], "?") for r in detail["results"]}
    detail["workflow_id"] = wf
    return detail


def summarize(name: str, detail: dict[str, Any]) -> str:
    m = detail["metrics"]
    keys = (
        "cases",
        "passed",
        "pass_rate",
        "tool_selection_accuracy",
        "security_cases",
        "security_cases_passed",
        "latency_p50_ms",
        "latency_p95_ms",
        "cost_per_case_usd",
        "error_rate",
    )
    lines = [f"## {name}", "", "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {m.get(k)} |" for k in keys]
    lines += ["", "| case | passed | failures |", "|---|---|---|"]
    for r in sorted(detail["results"], key=lambda r: detail["case_names"][r["case_id"]]):
        lines.append(
            f"| {detail['case_names'][r['case_id']]} | {'yes' if r['passed'] else 'NO'} "
            f"| {'; '.join(r['failures'])} |"
        )
    return "\n".join(lines)


async def _main(name: str, base: str) -> dict[str, Any]:
    api = f"{base.rstrip('/')}/api/v1"
    async with httpx.AsyncClient(timeout=60) as c:
        email, pw = f"case-{uuid.uuid4().hex[:10]}@example.com", uuid.uuid4().hex + "Aa1!"
        await _ok(
            await c.post(
                f"{api}/auth/register",
                json={"email": email, "password": pw, "display_name": "Case study"},
            ),
            201,
            "register",
        )
        token = (
            await _ok(
                await c.post(f"{api}/auth/login", json={"email": email, "password": pw}),
                200,
                "login",
            )
        )["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        org = (
            await _ok(
                await c.post(f"{api}/orgs", headers=h, json={"name": f"Case {name} {email[5:13]}"}),
                201,
                "org",
            )
        )["id"]
        detail = await run_case_study(c, h, f"{api}/orgs/{org}", name)
    return detail


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("name", choices=sorted(d.name for d in CASES.iterdir() if d.is_dir()))
    p.add_argument("base_url")
    p.add_argument("--json")
    a = p.parse_args()
    result = asyncio.run(_main(a.name, a.base_url))
    print(summarize(a.name, result))
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    sys.exit(0)
