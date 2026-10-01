from __future__ import annotations

from typing import Any

import pytest

from solutionforge.domain.tools import ToolInstallation
from solutionforge.security.ratelimit import (
    FailOpenRateLimiter,
    InMemoryRateLimiter,
    Limit,
    RedisRateLimiter,
)
from solutionforge.security.rbac import Role
from solutionforge.tools import policy
from solutionforge.tools.catalog import default_catalog
from solutionforge.tools.policy import Decision, ToolPolicyConfig
from solutionforge.tools.spec import RiskLevel

CAT = default_catalog()
READ = CAT.get("crm.get_customer").spec
WRITE = CAT.get("ticketing.create_ticket").spec
REFUND = CAT.get("payments.issue_refund").spec
INST = ToolInstallation(tool_name="x", enabled=True, auto_approve_low_risk=True, config={})


@pytest.mark.parametrize(
    ("spec", "role", "org", "expected", "reason"),
    [
        (READ, Role.OPERATOR, None, Decision.ALLOW, "read-only"),
        (READ, None, None, Decision.DENY, "no longer a member"),
        (READ, Role.VIEWER, None, Decision.DENY, "lacks 'workflow:execute'"),
        (
            READ,
            Role.OWNER,
            ToolPolicyConfig(blocked_tools=["crm.get_customer"]),
            Decision.DENY,
            "blocked",
        ),
        (
            REFUND,
            Role.OWNER,
            ToolPolicyConfig(blocked_risk_levels=[RiskLevel.HIGH_RISK]),
            Decision.DENY,
            "blocked",
        ),
        (
            WRITE,
            Role.OPERATOR,
            ToolPolicyConfig(auto_allow_up_to="read_only"),
            Decision.REQUIRE_APPROVAL,
            "requires approval",
        ),
        (WRITE, Role.OPERATOR, None, Decision.ALLOW, "auto-approved"),
        (REFUND, Role.OPERATOR, None, Decision.REQUIRE_APPROVAL, "high-risk"),
    ],
)
def test_policy_order(
    spec: Any, role: Role | None, org: ToolPolicyConfig | None, expected: Decision, reason: str
) -> None:
    r = policy.evaluate(spec, INST, actor_role=role, org_policy=org)
    assert r.decision == expected and reason in r.reason


def test_org_policy_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        ToolPolicyConfig.model_validate({"allow_everything": True})


# ------------------------------------------------------------------ rate limiting

LIMIT = Limit("t", 3, 60)


async def test_in_memory_fixed_window() -> None:
    now = [1000.0]
    rl = InMemoryRateLimiter(clock=lambda: now[0])
    results = [await rl.hit(LIMIT, "alice") for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[-1].retry_after_seconds == 21  # window ends at 1020
    assert (await rl.hit(LIMIT, "bob")).allowed  # keys are independent
    now[0] = 1021  # next window
    assert (await rl.hit(LIMIT, "alice")).allowed


class FakePipeline:
    def __init__(self, store: dict[str, int]) -> None:
        self.store, self.ops = store, []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def incr(self, k: str) -> None:
        self.ops.append(("incr", k))

    def expire(self, k: str, ttl: int) -> None:
        self.ops.append(("expire", k, ttl))

    async def execute(self) -> list[Any]:
        k = self.ops[0][1]
        self.store[k] = self.store.get(k, 0) + 1
        return [self.store[k], True]


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self.store)


async def test_redis_limiter_counts_and_hashes_keys() -> None:
    fake = FakeRedis()
    rl = RedisRateLimiter(fake, clock=lambda: 1000.0)
    decisions = [await rl.hit(LIMIT, "victim@example.com") for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert all("victim" not in k and k.startswith("rl:t:") for k in fake.store)


async def test_fail_open_on_backend_error() -> None:
    class Broken:
        async def hit(self, limit: Limit, key: str) -> Any:
            raise ConnectionError("redis down")

    errors: list[Exception] = []
    rl = FailOpenRateLimiter(Broken(), on_error=errors.append)
    assert (await rl.hit(LIMIT, "k")).allowed and len(errors) == 1
