"""Post-deploy check of the assistant, end to end, against a running environment.

    python scripts/assistant_smoke.py https://solutionforge-api.onrender.com --timeout 120

Creates a clearly labelled throwaway account and organization ("SMOKE TEST ... safe to
delete"), loads the demo corpus, then verifies:

  1. configuration: offered models, limits, the knowledge base's embedder;
  2. a sourced answer (citations that the run actually retrieved);
  3. a ticket lookup through get_ticket_status;
  4. a ticket draft that does NOT create a ticket by itself;
  5. explicit approval creates exactly one ticket;
  6. a repeated approval creates nothing more, and 5 concurrent first approvals of a
     fresh pending draft create exactly one ticket;
  7. an edited draft can't be approved with the stale hash;
  8. an unsupported question abstains.

Model: ``fixture:policy-v1`` by default, so the check costs nothing. Pass ``--model
anthropic:...`` to exercise a live model. That makes paid calls, capped per run by the
server's SF_ASSISTANT_RUN_BUDGET_USD.

Cleanup: deletes the demo knowledge base (documents and chunks). The API has no delete for
users, organizations, tickets, drafts or runs, and audit rows are append-only, so those
remain, labelled. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx

LABEL = "SMOKE TEST (assistant) - safe to delete"


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def __call__(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        print(
            f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True
        )
        return ok


async def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("base_url")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--model", default="fixture:policy-v1")
    p.add_argument("--keep", action="store_true", help="do not delete the knowledge base")
    args = p.parse_args(argv)
    check = Check()
    base = args.base_url.rstrip("/") + "/api/v1"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    email = f"smoke-assistant-{stamp}-{secrets.token_hex(3)}@example.com"
    password = secrets.token_urlsafe(18)  # never printed

    async with httpx.AsyncClient(timeout=args.timeout) as c:

        async def call(
            method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 204)
        ) -> httpx.Response:
            r = await c.request(method, base + path, json=body, headers=headers)
            if r.status_code not in ok:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r

        headers: dict[str, str] = {}
        r = await c.post(
            f"{base}/auth/register",
            json={"email": email, "password": password, "display_name": LABEL},
        )
        if r.status_code != 201:
            print(f"register failed: {r.status_code} {r.text[:200]}")
            return 2
        token = (
            await c.post(f"{base}/auth/login", json={"email": email, "password": password})
        ).json()["access_token"]
        headers["Authorization"] = f"Bearer {token}"
        org = (await call("POST", "/orgs", {"name": f"{LABEL} {stamp}"})).json()["id"]
        print(f"throwaway account {email}, organization {org}")
        kb: str | None = None
        try:
            info = (await call("GET", f"/orgs/{org}/assistant")).json()
            print(
                f"models offered: {info['models']}  prompt: {info['prompt_version']}  "
                f"limits: {info['limits']}"
            )
            check(
                "model offered", args.model in info["models"], f"{args.model} in {info['models']}"
            )

            setup = (await call("POST", f"/orgs/{org}/assistant/demo-setup")).json()
            kb = setup["knowledge_base_id"]
            print(
                f"demo knowledge base {kb}, embedder {setup['embedder']}, docs {setup['documents']}"
            )
            deadline = time.monotonic() + max(args.timeout, 180)
            ready = 0
            while time.monotonic() < deadline:
                docs = (await call("GET", f"/orgs/{org}/knowledge-bases/{kb}/documents")).json()
                ready = sum(d["status"] == "ready" for d in docs)
                if ready == 20 or any(d["status"] == "failed" for d in docs):
                    break
                await asyncio.sleep(3)
            check("20 documents indexed", ready == 20, f"{ready}/20 ready")

            async def ask(q: str) -> dict[str, Any]:
                r = await call(
                    "POST",
                    f"/orgs/{org}/assistant/runs",
                    {"question": q, "knowledge_base_id": kb, "model": args.model},
                )
                run: dict[str, Any] = r.json()
                run["_events"] = (
                    await call("GET", f"/orgs/{org}/assistant/runs/{run['id']}/events")
                ).json()
                print(
                    f"      run {run['id'][:8]} status={run['status']} {run['latency_ms']} ms "
                    f"(retrieval {run['retrieval_ms']} ms) "
                    f"tokens {run['input_tokens']}/{run['output_tokens']} cost {run['cost_usd']}  "
                    f"answer: {(run['answer'] or run['error'] or '')[:140]!r}"
                )
                return run

            def tools(run: dict[str, Any]) -> list[dict[str, Any]]:
                return [e for e in run["_events"] if e["kind"] == "tool"]

            # 2. Sourced answer
            run = await ask("How many days do I have to ask for a refund?")
            retrieved = {
                sid
                for e in tools(run)
                if e["name"] == "search_documents"
                for sid in e["detail"].get("results", [])
            }
            cited = [ci["source_id"] for ci in run["citations"]]
            check(
                "sourced answer",
                run["status"] == "completed" and bool(cited) and set(cited) <= retrieved,
                f"cited {cited}, all retrieved in this run",
            )
            check(
                "cited source is a refund document",
                any(ci["doc_id"] in ("HP-POL-02", "HP-SOP-04") for ci in run["citations"]),
                ", ".join(f"{ci['doc_id']} v{ci['version']}" for ci in run["citations"]),
            )

            # 3. Ticket lookup
            run = await ask("What's the status of ticket T-1A2B3C4D?")
            lookups = [e for e in tools(run) if e["name"] == "get_ticket_status"]
            check(
                "ticket lookup",
                bool(lookups)
                and lookups[0]["outcome"] == "found"
                and "open" in (run["answer"] or "").lower(),
                f"tool outcome {lookups[0]['outcome'] if lookups else 'not called'}",
            )

            async def ticket_refs() -> list[str]:
                return [
                    t["ref"]
                    for t in (await call("GET", f"/orgs/{org}/simulated/activity")).json()[
                        "tickets"
                    ]
                ]

            # 4. Draft does not create a ticket
            before = await ticket_refs()
            run = await ask(
                "Please open a ticket: SMOKE TEST, the kitchen tap is dripping. Safe to delete."
            )
            drafts = run["drafts"]
            check(
                "draft prepared, pending",
                len(drafts) == 1 and drafts[0]["status"] == "pending",
                f"{len(drafts)} draft(s)",
            )
            check(
                "no ticket before approval", await ticket_refs() == before, f"{len(before)} tickets"
            )
            draft = drafts[0]
            url = f"/orgs/{org}/assistant/drafts/{draft['id']}/approve"

            # 5. Explicit approval
            first = (await call("POST", url, {"content_hash": draft["content_hash"]})).json()
            ref = first["draft"]["ticket_ref"]
            check(
                "explicit approval creates the ticket",
                first["ticket_created"] is True and bool(ref and re.match(r"^T-[0-9A-F]{8}$", ref)),
                str(ref),
            )

            # 6. Repeated approval of an already-approved draft
            again = (await call("POST", url, {"content_hash": draft["content_hash"]})).json()
            after = await ticket_refs()
            check(
                "repeated approval creates nothing",
                again["ticket_created"] is False
                and again["draft"]["ticket_ref"] == ref
                and len(after) == len(before) + 1,
                f"tickets {len(before)} -> {len(after)}",
            )

            # 6b. Race: concurrent FIRST approvals of a fresh, still-pending draft
            run = await ask("Please open a ticket: SMOKE TEST, draft for the approval race check.")
            race = run["drafts"][0]
            check("race draft is pending", race["status"] == "pending")
            race_url = f"/orgs/{org}/assistant/drafts/{race['id']}/approve"
            race_before = await ticket_refs()
            responses = await asyncio.gather(
                *[call("POST", race_url, {"content_hash": race["content_hash"]}) for _ in range(5)]
            )
            bodies = [r.json() for r in responses]
            created = sum(b["ticket_created"] for b in bodies)
            race_refs = {b["draft"]["ticket_ref"] for b in bodies}
            race_after = await ticket_refs()
            new_refs = [t for t in race_after if t not in race_before]
            check(
                "concurrent approvals create exactly one ticket",
                created == 1 and len(race_refs) == 1 and new_refs == list(race_refs),
                f"5 concurrent approvals: {created} created, refs {sorted(race_refs)}, "
                f"new tickets {new_refs}",
            )

            # 7. Stale review hash
            run = await ask(
                "Please open a ticket: SMOKE TEST, second draft for the stale-approval check."
            )
            d2 = run["drafts"][0]
            base_d2 = f"/orgs/{org}/assistant/drafts/{d2['id']}"
            await call(
                "PATCH",
                base_d2,
                {"subject": "SMOKE TEST edited draft", "description": d2["description"]},
            )
            stale = await call(
                "POST", f"{base_d2}/approve", {"content_hash": d2["content_hash"]}, ok=(409,)
            )
            check("edited draft refuses the stale approval", stale.status_code == 409)
            await call("POST", f"{base_d2}/reject")

            # 8. Unsupported question
            run = await ask("Do you clean swimming pools?")
            check(
                "unsupported question abstains",
                run["status"] == "insufficient_evidence" and not run["citations"],
                run["status"],
            )
        except Exception as exc:
            check("unexpected error", False, f"{type(exc).__name__}: {exc}"[:300])
        finally:
            if kb and not args.keep:
                r = await c.delete(f"{base}/orgs/{org}/knowledge-bases/{kb}", headers=headers)
                check(
                    "cleanup: demo knowledge base deleted", r.status_code == 204, str(r.status_code)
                )
            print(
                f"left in place (no delete API): user {email}, "
                f"organization {org} '{LABEL} {stamp}', "
                "its runs, drafts, tickets and audit rows"
            )

    failed = [n for n, ok, _ in check.rows if not ok]
    print(
        f"\n{len(check.rows) - len(failed)}/{len(check.rows)} checks passed"
        + (f"; FAILED: {failed}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
