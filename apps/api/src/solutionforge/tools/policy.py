"""Deterministic tool policy: (tool risk, tenant installation) → allow / approval / deny.

Pure function, no I/O, no model involvement. The LLM (or a workflow author) can *request* a
tool; this decides. Phase 7 extends the inputs with the acting principal's role and an
org-level policy; Phase 8 turns REQUIRE_APPROVAL into a durable human approval instead of
a refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from solutionforge.domain.tools import ToolInstallation
from solutionforge.security.rbac import Permission
from solutionforge.tools.spec import RiskLevel, ToolSpec


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: Decision
    reason: str
    approver_permission: Permission | None = None


def evaluate(spec: ToolSpec, installation: ToolInstallation | None) -> PolicyResult:
    if installation is None or not installation.enabled:
        return PolicyResult(Decision.DENY, "tool is not enabled for this organization")
    match spec.risk_level:
        case RiskLevel.READ_ONLY:
            return PolicyResult(Decision.ALLOW, "read-only")
        case RiskLevel.LOW_RISK_WRITE:
            if installation.auto_approve_low_risk:
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
                Decision.REQUIRE_APPROVAL,
                "high-risk action",
                Permission.APPROVAL_DECIDE_HIGH_RISK,
            )
