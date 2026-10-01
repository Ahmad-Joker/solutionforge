"""Property-based tests (Hypothesis) for the pure, security- and correctness-critical logic."""

from __future__ import annotations

import contextlib
import string
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from solutionforge.domain.tools import ToolInstallation
from solutionforge.retrieval.chunking import chunk_text
from solutionforge.security.rbac import ROLE_RANK, Role
from solutionforge.services.audit_service import REDACTED, sanitize_metadata
from solutionforge.tools import policy
from solutionforge.tools.catalog import default_catalog
from solutionforge.tools.policy import Decision, ToolPolicyConfig
from solutionforge.workflows import expressions

FAST = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])

json_scalars = st.none() | st.booleans() | st.integers(-(10**6), 10**6) | st.text(max_size=20)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)
idents = st.text(alphabet=string.ascii_lowercase + "_", min_size=1, max_size=8)


# ------------------------------------------------------------------ expressions


@FAST
@given(
    scope_input=st.dictionaries(idents, json_values, max_size=5),
    path=st.lists(idents | st.integers(0, 5), max_size=4),
)
def test_lookup_never_raises_unexpected_errors(
    scope_input: dict[str, Any], path: list[Any]
) -> None:
    ref = "$.input" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)
    with contextlib.suppress(expressions.ExpressionError):  # the only allowed failure mode
        expressions.lookup({"input": scope_input, "state": {}, "steps": {}}, ref)


@FAST
@given(text=st.text(max_size=200))
def test_resolve_on_arbitrary_strings_is_contained(text: str) -> None:
    scope = {"input": {"a": 1}, "state": {}, "steps": {}}
    try:
        out = expressions.resolve(text, scope)
    except expressions.ExpressionError:
        return
    assert isinstance(out, str | int)  # strings stay strings unless an exact reference


@FAST
@given(value=json_values)
def test_resolving_values_without_references_is_identity(value: Any) -> None:
    def has_refs(v: Any) -> bool:
        if isinstance(v, str):
            return v.startswith(("$.", "$$")) or "{{" in v
        if isinstance(v, list):
            return any(has_refs(x) for x in v)
        if isinstance(v, dict):
            return any(has_refs(x) for x in v.values())
        return False

    if not has_refs(value):
        assert expressions.resolve(value, {"input": {}, "state": {}, "steps": {}}) == value


# ------------------------------------------------------------------ redaction

SECRET_KEYS = st.sampled_from(
    ["password", "API_KEY", "refresh_token", "client_secret", "Authorization", "private_key"]
)


@FAST
@given(
    value=json_values,
    secret_key=SECRET_KEYS,
    secret=st.text(min_size=12, max_size=30, alphabet="QZXWJ"),
)
def test_secrets_never_survive_sanitization_at_any_depth(
    value: Any, secret_key: str, secret: str
) -> None:
    nested: Any = {secret_key: secret, "data": value}
    for _ in range(3):
        nested = {"wrapper": [nested]}
    out = sanitize_metadata(nested)
    assert secret not in repr(out)
    assert REDACTED in repr(out)


# ------------------------------------------------------------------ policy

SPECS = default_catalog().specs()
ROLES_ASC = sorted(Role, key=lambda r: ROLE_RANK[r])
ORDER = {Decision.DENY: 0, Decision.REQUIRE_APPROVAL: 1, Decision.ALLOW: 2}


@FAST
@given(
    spec=st.sampled_from(SPECS),
    enabled=st.booleans(),
    auto=st.booleans(),
    ceiling=st.sampled_from(["read_only", "low_risk_write"]),
    blocked=st.lists(st.sampled_from([s.name for s in SPECS]), max_size=3),
)
def test_policy_is_monotonic_in_role(
    spec: Any, enabled: bool, auto: bool, ceiling: str, blocked: list[str]
) -> None:
    inst = ToolInstallation(
        tool_name=spec.name, enabled=enabled, auto_approve_low_risk=auto, config={}
    )
    org = ToolPolicyConfig(auto_allow_up_to=ceiling, blocked_tools=blocked)  # type: ignore[arg-type]
    decisions = [
        policy.evaluate(spec, inst, actor_role=r, org_policy=org).decision for r in ROLES_ASC
    ]
    assert [ORDER[d] for d in decisions] == sorted(
        ORDER[d] for d in decisions
    )  # never less permissive upward
    if spec.name in blocked or not enabled:
        assert set(decisions) == {Decision.DENY}  # blocks beat any role
    if spec.risk_level.value in ("external_action", "high_risk"):
        assert Decision.ALLOW not in decisions  # never auto-executed, whatever the config
    assert policy.evaluate(spec, inst, actor_role=None, org_policy=org).decision == Decision.DENY


# ------------------------------------------------------------------ chunking

paragraph = st.text(alphabet=string.ascii_letters + " .,!?", min_size=1, max_size=400)
heading = st.builds(
    lambda level, title: "#" * level + " " + title,
    st.integers(1, 3),
    st.text(alphabet=string.ascii_letters, min_size=1, max_size=20),
)
documents = st.lists(paragraph | heading, min_size=1, max_size=25).map("\n\n".join)


@FAST
@given(doc=documents, size=st.integers(100, 900), overlap_frac=st.floats(0, 0.45))
def test_chunking_invariants(doc: str, size: int, overlap_frac: float) -> None:
    overlap = int(size * overlap_frac)
    chunks = chunk_text(doc, size=size, overlap=overlap)
    covered: set[int] = set()
    for c in chunks:
        assert doc[c.char_start : c.char_end].strip() == c.text
        assert c.char_end - c.char_start <= size
        covered.update(range(c.char_start, c.char_end))
        # A chunk never contains a heading line other than at its very start.
        assert all(not line.lstrip().startswith("#") for line in c.text.splitlines()[1:])
    assert all(i in covered for i, ch in enumerate(doc) if not ch.isspace())
