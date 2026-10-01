from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from solutionforge.connectors.simulated import (
    SIMULATED_TOOLS,
    CreateTicketIn,
    DraftMessageIn,
    GetCustomerIn,
    IssueRefundIn,
)
from solutionforge.domain.tools import ToolInstallation
from solutionforge.security.crypto import CredentialCipher, CredentialError
from solutionforge.security.rbac import Role
from solutionforge.tools import policy
from solutionforge.tools.catalog import ToolCatalog, default_catalog
from solutionforge.tools.policy import Decision
from solutionforge.tools.spec import RiskLevel

# ------------------------------------------------------------------ credentials


def test_credentials_round_trip_and_ciphertext_hides_values() -> None:
    c = CredentialCipher([CredentialCipher.generate_key()])
    token = c.encrypt({"api_key": "sk-live-123"})
    assert b"sk-live-123" not in token
    assert c.decrypt(token) == {"api_key": "sk-live-123"}


def test_wrong_key_cannot_decrypt() -> None:
    token = CredentialCipher([CredentialCipher.generate_key()]).encrypt({"a": "b"})
    with pytest.raises(CredentialError):
        CredentialCipher([CredentialCipher.generate_key()]).decrypt(token)


def test_key_rotation() -> None:
    old, new = CredentialCipher.generate_key(), CredentialCipher.generate_key()
    token = CredentialCipher([old]).encrypt({"api_key": "x"})
    ring = CredentialCipher([new, old])  # new primary, old still accepted
    assert ring.decrypt(token) == {"api_key": "x"}
    rotated = ring.rotate(token)
    assert CredentialCipher([new]).decrypt(rotated) == {"api_key": "x"}  # old key retirable


def test_tampered_token_rejected() -> None:
    c = CredentialCipher([CredentialCipher.generate_key()])
    token = bytearray(c.encrypt({"a": "b"}))
    token[20] ^= 1
    with pytest.raises(CredentialError):
        c.decrypt(bytes(token))


def test_invalid_key_material() -> None:
    with pytest.raises(ValueError, match="Fernet"):
        CredentialCipher(["not-a-key"])


# ------------------------------------------------------------------ policy


def _inst(enabled: bool = True, auto: bool = True) -> ToolInstallation:
    return ToolInstallation(tool_name="x", enabled=enabled, auto_approve_low_risk=auto, config={})


SPECS = {s.risk_level: s for s in default_catalog().specs()}


@pytest.mark.parametrize(
    ("risk", "inst", "expected"),
    [
        (RiskLevel.READ_ONLY, _inst(), Decision.ALLOW),
        (RiskLevel.READ_ONLY, None, Decision.DENY),
        (RiskLevel.READ_ONLY, _inst(enabled=False), Decision.DENY),
        (RiskLevel.LOW_RISK_WRITE, _inst(), Decision.ALLOW),
        (RiskLevel.LOW_RISK_WRITE, _inst(auto=False), Decision.REQUIRE_APPROVAL),
        (RiskLevel.EXTERNAL_ACTION, _inst(), Decision.REQUIRE_APPROVAL),
        (RiskLevel.HIGH_RISK, _inst(), Decision.REQUIRE_APPROVAL),
    ],
)
def test_policy_matrix(risk: RiskLevel, inst: ToolInstallation | None, expected: Decision) -> None:
    assert policy.evaluate(SPECS[risk], inst, actor_role=Role.OPERATOR).decision == expected


def test_high_risk_needs_privileged_approver() -> None:
    r = policy.evaluate(SPECS[RiskLevel.HIGH_RISK], _inst(), actor_role=Role.OPERATOR)
    assert r.approver_permission is not None
    assert r.approver_permission.value == "approval:decide_high_risk"


# ------------------------------------------------------------------ catalog + schemas


def test_catalog_rejects_bad_and_duplicate_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ToolCatalog([*SIMULATED_TOOLS, SIMULATED_TOOLS[0]])


def _strings_bounded(schema: dict[str, Any], defs: dict[str, Any]) -> list[str]:
    """Every string field must be bounded (maxLength/pattern/enum/format/const)."""
    if "$ref" in schema:
        return _strings_bounded(defs[schema["$ref"].split("/")[-1]], defs)
    problems: list[str] = []
    if schema.get("type") == "string" and not (
        {"maxLength", "pattern", "enum", "format", "const"} & schema.keys()
    ):
        problems.append(str(schema))
    for key in ("properties",):
        for sub in schema.get(key, {}).values():
            problems += _strings_bounded(sub, defs)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            problems += _strings_bounded(schema[key], defs)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key, []):
            problems += _strings_bounded(sub, defs)
    return problems


@pytest.mark.parametrize("spec", default_catalog().specs(), ids=lambda s: s.name)
def test_every_tool_input_string_is_bounded_and_extra_fields_forbidden(spec: Any) -> None:
    schema = spec.input_schema()
    assert schema.get("additionalProperties") is False
    assert _strings_bounded(schema, schema.get("$defs", {})) == []


@pytest.mark.parametrize("spec", default_catalog().specs(), ids=lambda s: s.name)
def test_mcp_descriptor_shape(spec: Any) -> None:
    d = spec.mcp_descriptor()
    assert set(d) == {"name", "description", "inputSchema", "outputSchema", "annotations"}
    assert d["inputSchema"]["type"] == "object"
    assert d["annotations"]["readOnlyHint"] == (spec.risk_level == RiskLevel.READ_ONLY)


def test_side_effecting_tools_are_retry_safe_or_single_shot() -> None:
    for spec in default_catalog().specs():
        if spec.has_side_effects and spec.max_attempts > 1:
            assert spec.supports_idempotency_key or spec.idempotent, spec.name


# ------------------------------------------------------------------ input hardening


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (DraftMessageIn, {"to": "a@example.com", "subject": "Hi\r\nBcc: evil@x.com", "body": "x"}),
        (DraftMessageIn, {"to": "not-an-email", "subject": "Hi", "body": "x"}),
        (CreateTicketIn, {"subject": "x", "body": "y", "priority": "critical"}),
        (CreateTicketIn, {"subject": "x", "body": "y", "assignee": "root"}),
        (CreateTicketIn, {"subject": "x" * 201, "body": "y"}),
        (IssueRefundIn, {"order_ref": "O-50001", "amount_cents": -100, "reason": "r"}),
        (
            IssueRefundIn,
            {"order_ref": "O-50001; DROP TABLE sim_orders", "amount_cents": 1, "reason": "r"},
        ),
        (GetCustomerIn, {}),
        (GetCustomerIn, {"customer_ref": "C-1001", "email": "a@example.com"}),
    ],
)
def test_injection_and_malformed_inputs_rejected(model: Any, data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)
