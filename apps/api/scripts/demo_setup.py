"""Populate a running SolutionForge with a realistic demo (used for README screenshots).

    python scripts/demo_setup.py http://127.0.0.1:8197 out.json

Creates a user + organization, runs the three case studies (workflows, knowledge base,
evaluation runs), then starts a few live support executions so the approvals inbox has
real pending requests. Writes the credentials and IDs to ``out.json`` for the screenshot
script. Throwaway data only: the user/password are random per run.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from case_study import run_case_study


async def main(base: str) -> dict[str, object]:
    api = f"{base.rstrip('/')}/api/v1"
    email = f"demo-{uuid.uuid4().hex[:8]}@example.com"
    password = uuid.uuid4().hex + "Aa1!"
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            f"{api}/auth/register",
            json={"email": email, "password": password, "display_name": "Dana Ops"},
        )
        r.raise_for_status()
        token = (
            await c.post(f"{api}/auth/login", json={"email": email, "password": password})
        ).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        org = (await c.post(f"{api}/orgs", headers=h, json={"name": "Acme Retail"})).json()["id"]
        o = f"{api}/orgs/{org}"
        results = {}
        for name in ("support", "research", "ops"):
            detail = await run_case_study(c, h, o, name)
            results[name] = {
                "workflow_id": detail["workflow_id"],
                "run_id": detail["id"],
                "pass_rate": detail["metrics"]["pass_rate"],
            }
        wf = results["support"]["workflow_id"]
        live = []
        for order, msg in (
            ("O-50011", "Sixteen days late. I'd like some compensation please."),
            ("O-50021", "Still waiting, call me on +1 415 555 2671."),
            ("O-50001", "Just checking where my order is."),
        ):
            r = await c.post(
                f"{o}/workflows/{wf}/executions",
                headers=h,
                json={"input": {"order": order, "message": msg}},
            )
            live.append(r.json()["id"])
        for _ in range(60):  # wait for the embedded worker
            states = [
                (await c.get(f"{o}/executions/{e}", headers=h)).json()["status"] for e in live
            ]
            if all(s not in ("queued", "running") for s in states):
                break
            await asyncio.sleep(1)
    print("demo ready:", {k: v["pass_rate"] for k, v in results.items()}, "live:", states)
    return {"email": email, "password": password, "org": org, "cases": results, "executions": live}


if __name__ == "__main__":
    demo = asyncio.run(main(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(demo, indent=1), encoding="utf-8")
