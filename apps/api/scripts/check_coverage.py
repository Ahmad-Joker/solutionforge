"""Fail CI when security-critical modules drop below their coverage floor.

Overall coverage is gated by pytest-cov (--cov-fail-under). This adds per-module floors
for the code where an untested branch is most likely to be a vulnerability.
Usage: python scripts/check_coverage.py coverage.json
"""

from __future__ import annotations

import json
import sys

FLOOR = 90.0
CRITICAL = (
    "security/",
    "db/tenancy.py",
    "services/authz.py",
    "services/auth_service.py",
    "services/org_service.py",
    "services/approval_service.py",
    "services/eval_service.py",
    "evaluation/",
    "tools/policy.py",
    "tools/executor.py",
    "workflows/engine.py",
    "workflows/transitions.py",
)


def main(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        files = json.load(fh)["files"]
    failures = []
    for name, data in sorted(files.items()):
        rel = name.replace("\\", "/").split("solutionforge/")[-1]
        pct = data["summary"]["percent_covered"]
        if rel.startswith(CRITICAL) or rel in CRITICAL:
            status = "ok " if pct >= FLOOR else "LOW"
            print(f"{status} {pct:6.1f}%  {rel}")
            if pct < FLOOR:
                failures.append(rel)
    if failures:
        print(f"\nBelow the {FLOOR}% floor: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "coverage.json"))
