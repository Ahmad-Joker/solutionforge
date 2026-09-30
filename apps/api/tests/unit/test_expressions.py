from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter

from solutionforge.workflows import expressions as ex

SCOPE: dict[str, Any] = {
    "input": {"customer": {"id": "c-1", "tier": "gold"}, "amount": 120, "tags": ["vip", "eu"]},
    "state": {"count": 3, "flag": False, "name": "Ada"},
    "steps": {"lookup": {"items": [{"sku": "A1"}, {"sku": "B2"}]}},
}
PRED = TypeAdapter(ex.Predicate)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("$.input.customer.id", "c-1"),
        ("$.input.tags[1]", "eu"),
        ("$.steps.lookup.items[0].sku", "A1"),
        ("$.state.flag", False),
        ("$.input", SCOPE["input"]),
    ],
)
def test_lookup(ref: str, expected: Any) -> None:
    assert ex.lookup(SCOPE, ref) == expected


@pytest.mark.parametrize(
    "ref",
    ["$.input.missing", "$.input.tags[9]", "$.state.count.deeper", "$.steps.nope"],
)
def test_missing_paths_raise(ref: str) -> None:
    with pytest.raises(ex.ExpressionError, match="not found"):
        ex.lookup(SCOPE, ref)


@pytest.mark.parametrize(
    "ref",
    ["input.x", "$.secrets.x", "$.input..x", "$.input.x[-1]", "$.input.__class__()", "$."],
)
def test_invalid_reference_syntax(ref: str) -> None:
    with pytest.raises(ex.ExpressionError):
        ex.parse_path(ref)


def test_resolve_nested_templates_and_escapes() -> None:
    value = {
        "id": "$.input.customer.id",
        "greeting": "Hi {{ $.state.name }}, you have {{ $.state.count }} items",
        "tags": ["$.input.tags[0]", "literal"],
        "price": "$$5.00",
        "n": 7,
        "obj": "{{ $.steps.lookup.items[1] }}",
    }
    assert ex.resolve(value, SCOPE) == {
        "id": "c-1",
        "greeting": "Hi Ada, you have 3 items",
        "tags": ["vip", "literal"],
        "price": "$5.00",
        "n": 7,
        "obj": '{"sku":"B2"}',
    }


def test_references_are_collected_statically() -> None:
    refs = ex.references({"a": "$.input.x", "b": ["x {{ $.state.y }}", "$$.not.a.ref"]})
    assert sorted(refs) == ["$.input.x", "$.state.y"]


@pytest.mark.parametrize(
    ("pred", "expected"),
    [
        ({"path": "$.input.amount", "op": "gt", "value": 100}, True),
        ({"path": "$.input.amount", "op": "lte", "value": 100}, False),
        ({"path": "$.input.customer.tier", "op": "in", "value": ["gold", "platinum"]}, True),
        ({"path": "$.input.tags", "op": "contains", "value": "vip"}, True),
        ({"path": "$.input.missing", "op": "exists"}, False),
        ({"path": "$.state.flag", "op": "truthy"}, False),
        ({"path": "$.state.count", "op": "eq", "value": "$.state.count"}, True),  # ref as value
        (
            {
                "all": [
                    {"path": "$.state.count", "op": "gte", "value": 3},
                    {"not": {"path": "$.state.flag", "op": "truthy"}},
                ]
            },
            True,
        ),
        (
            {
                "any": [
                    {"path": "$.state.count", "op": "lt", "value": 0},
                    {"path": "$.input.customer.tier", "op": "eq", "value": "gold"},
                ]
            },
            True,
        ),
    ],
)
def test_predicates(pred: dict[str, Any], expected: bool) -> None:
    assert ex.evaluate(PRED.validate_python(pred), SCOPE) is expected


@pytest.mark.parametrize(
    "pred",
    [
        {"path": "$.input.customer.tier", "op": "gt", "value": 3},  # str vs int
        {"path": "$.state.flag", "op": "lt", "value": 1},  # bool is not a number here
        {"path": "$.input.missing", "op": "eq", "value": 1},  # strict: use exists
        {"path": "$.input.amount", "op": "in", "value": 5},  # 'in' needs a list
    ],
)
def test_predicate_type_errors_are_loud(pred: dict[str, Any]) -> None:
    with pytest.raises(ex.ExpressionError):
        ex.evaluate(PRED.validate_python(pred), SCOPE)


def test_unknown_operator_rejected_at_parse_time() -> None:
    with pytest.raises(ValueError, match="op"):
        PRED.validate_python({"path": "$.input.amount", "op": "regex", "value": ".*"})
