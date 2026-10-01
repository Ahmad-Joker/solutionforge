"""Deterministic tool policy: who may cause which tool call, and what needs approval.

Inputs: the tool's declared risk and required permission, the tenant's installation, the
organization's tool policy, and the *current* role of the execution's initiator (resolved
from the database at call time, so demotions and removals apply immediately). Pure function:
no I/O, no model involvement. Evaluation order, first match wins:

1. tool not installed / disabled                         → DENY
2. tool or its risk level blocked by org policy           → DENY
3. initiator no longer a member                           → DENY
4. initiator's role lacks the tool's required permission  → DENY
5. read_only                                              → ALLOW
6. low_risk_write within org ceiling and auto-approved    → ALLOW, else REQUIRE_APPROVAL
7. external_action                                        → REQUIRE_APPROVAL (approval:decide)
8. high_risk                                       → REQUIRE_APPROVAL (approval:decide_high_risk)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from solutionforge.domain.tools import ToolInstallation
from solutionforge.security.rbac import Permission, Role, has_permission
from solutionforge.tools.spec import RiskLevel, ToolSpec


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ToolPolicyConfig(BaseModel):
    """Organization-wide tool policy. Defaults reproduce the platform baseline."""

    model_config = ConfigDict(extra="forbid")

    auto_allow_up_to: Literal["read_only", "low_risk_write"] = "low_risk_write"
    blocked_tools: list[str] = Field(default_factory=list, max_length=100)
    blocked_risk_levels: list[RiskLevel] = Field(default_factory=list, max_length=4)

    def blocks(self, spec: ToolSpec) -> bool:
        return spec.name in self.blocked_tools or spec.risk_level in self.blocked_risk_levels


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: Decision
    reason: str
    approver_permission: Permission | None = None


def evaluate(
    spec: ToolSpec,
    installation: ToolInstallation | None,
    *,
    actor_role: Role | None,
    org_policy: ToolPolicyConfig | None = None,
) -> PolicyResult:
    org_policy = org_policy or ToolPolicyConfig()
    if installation is None or not installation.enabled:
        return PolicyResult(Decision.DENY, "tool is not enabled for this organization")
    if org_policy.blocks(spec):
        return PolicyResult(Decision.DENY, "blocked by organization tool policy")
    if actor_role is None:
        return PolicyResult(Decision.DENY, "execution initiator is no longer a member")
    if not has_permission(actor_role, spec.required_permission):
        return PolicyResult(
            Decision.DENY,
            f"initiator role '{actor_role.value}' lacks '{spec.required_permission.value}'",
        )
    match spec.risk_level:
        case RiskLevel.READ_ONLY:
            return PolicyResult(Decision.ALLOW, "read-only")
        case RiskLevel.LOW_RISK_WRITE:
            if (
                org_policy.auto_allow_up_to == "low_risk_write"
                and installation.auto_approve_low_risk
            ):
                return PolicyResult(Decision.ALLOW, "low-risk write auto-approved by tenant policy")
            return PolicyResult(
                Decision.REQUIRE_APPROVAL,
                "tenant policy requires approval for writes",
                Permission.APPROVAL_DECIDE,
            )
        case RiskLevel.EXTERNAL_ACTION:
            return PolicyResult(
                Decision.REQUIRE_APPROVAL, "external action", Permission.APPROVAL_DECIDE
            )
        case RiskLevel.HIGH_RISK:
            return PolicyResult(
                Decision.REQUIRE_APPROVAL, "high-risk action", Permission.APPROVAL_DECIDE_HIGH_RISK
            )
