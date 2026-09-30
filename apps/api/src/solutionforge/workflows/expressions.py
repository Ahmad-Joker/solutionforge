"""A deliberately tiny, side-effect-free expression language for workflow definitions.

No ``eval``, no attribute access, no function calls, no regex (ReDoS). Just:

* **References:** ``"$.input.customer.id"``, ``"$.steps.lookup.items[0]"``, ``"$.state.count"``.
  Roots: ``input`` (execution input), ``state`` (workflow variables), ``steps`` (outputs of
  completed steps, by step id).
* **Templates:** ``"Hello {{ $.input.name }}"``. Non-string values are JSON-encoded.
* **Literals:** anything else. A leading ``$$`` escapes a literal ``$``.
* **Predicates:** ``{"path": ..., "op": ..., "value": ...}`` combined with ``all``/``any``/``not``.

Evaluation is strict: referencing a missing path is an error (use ``op: exists`` to test for
presence), and ordering comparisons between incompatible types are errors, not ``False``.
"""

from __future__ import annotations

import json
import operator
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ROOTS = frozenset({"input", "state", "steps"})
_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(\d{1,6})\]")
_TEMPLATE = re.compile(r"\{\{\s*(\$\.[^}\s]+)\s*\}\}")
_MAX_DEPTH = 32
_ORDER = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}

Scope = dict[str, Any]


class ExpressionError(ValueError):
    """Invalid expression or failed evaluation. Never retryable: the definition or data is wrong."""


def parse_path(ref: str) -> tuple[str | int, ...]:
    if not ref.startswith("$."):
        raise ExpressionError(f"reference must start with '$.': {ref!r}")
    body, pos, parts = ref[1:], 0, []
    while pos < len(body):
        m = _TOKEN.match(body, pos)
        if m is None:
            raise ExpressionError(f"invalid reference syntax: {ref!r}")
        parts.append(m.group(1) if m.group(1) is not None else int(m.group(2)))
        pos = m.end()
    if not parts or parts[0] not in ROOTS:
        raise ExpressionError(f"reference root must be one of {sorted(ROOTS)}: {ref!r}")
    if len(parts) > _MAX_DEPTH:
        raise ExpressionError(f"reference too deep: {ref!r}")
    return tuple(parts)


_MISSING = object()


def _lookup(scope: Scope, ref: str) -> Any:
    cur: Any = scope
    for part in parse_path(ref):
        if isinstance(part, int):
            cur = cur[part] if isinstance(cur, list) and part < len(cur) else _MISSING
        else:
            cur = cur.get(part, _MISSING) if isinstance(cur, dict) else _MISSING
        if cur is _MISSING:
            return _MISSING
    return cur


def lookup(scope: Scope, ref: str) -> Any:
    value = _lookup(scope, ref)
    if value is _MISSING:
        raise ExpressionError(f"path not found: {ref}")
    return value


def exists(scope: Scope, ref: str) -> bool:
    return _lookup(scope, ref) is not _MISSING


def resolve(value: Any, scope: Scope, _depth: int = 0) -> Any:
    """Resolve references and templates anywhere inside a JSON-like value."""
    if _depth > _MAX_DEPTH:
        raise ExpressionError("value nested too deeply")
    if isinstance(value, str):
        if value.startswith("$$"):
            return value[1:]
        if value.startswith("$."):
            return lookup(scope, value)
        if "{{" in value:
            return _TEMPLATE.sub(lambda m: _stringify(lookup(scope, m.group(1))), value)
        return value
    if isinstance(value, dict):
        return {k: resolve(v, scope, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, scope, _depth + 1) for v in value]
    return value


def references(value: Any) -> list[str]:
    """All references in a value; used to validate definitions statically."""
    if isinstance(value, str):
        if value.startswith("$$"):
            return []
        if value.startswith("$."):
            return [value]
        return _TEMPLATE.findall(value)
    if isinstance(value, dict):
        return [r for v in value.values() for r in references(v)]
    if isinstance(value, list):
        return [r for v in value for r in references(v)]
    return []


def _stringify(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- predicates

CompareOp = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "exists", "truthy"
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Compare(_Strict):
    path: str
    op: CompareOp
    value: Any = None


class AllOf(_Strict):
    all: list[Predicate] = Field(min_length=1, max_length=50)


class AnyOf(_Strict):
    any: list[Predicate] = Field(min_length=1, max_length=50)


class Not(_Strict):
    not_: Predicate = Field(alias="not")


Predicate = Annotated[Compare | AllOf | AnyOf | Not, Field(union_mode="left_to_right")]
for _m in (AllOf, AnyOf, Not):
    _m.model_rebuild()


def predicate_references(p: Compare | AllOf | AnyOf | Not) -> list[str]:
    if isinstance(p, Compare):
        return [p.path, *references(p.value)]
    if isinstance(p, AllOf):
        return [r for c in p.all for r in predicate_references(c)]
    if isinstance(p, AnyOf):
        return [r for c in p.any for r in predicate_references(c)]
    return predicate_references(p.not_)


def evaluate(p: Compare | AllOf | AnyOf | Not, scope: Scope) -> bool:
    if isinstance(p, AllOf):
        return all(evaluate(c, scope) for c in p.all)
    if isinstance(p, AnyOf):
        return any(evaluate(c, scope) for c in p.any)
    if isinstance(p, Not):
        return not evaluate(p.not_, scope)

    if p.op == "exists":
        return exists(scope, p.path)
    left = lookup(scope, p.path)
    if p.op == "truthy":
        return bool(left)
    right = resolve(p.value, scope)
    match p.op:
        case "eq":
            return bool(left == right)
        case "ne":
            return bool(left != right)
        case "in" | "not_in":
            if not isinstance(right, list):
                raise ExpressionError(f"'{p.op}' requires a list value")
            return (left in right) == (p.op == "in")
        case "contains":
            if not isinstance(left, list | str | dict):
                raise ExpressionError("'contains' requires a list, string or object on the left")
            if isinstance(left, str) and not isinstance(right, str):
                raise ExpressionError("'contains' on a string requires a string value")
            return bool(right in left)
        case _:
            return _order(p.op, left, right)


def _order(op: str, left: Any, right: Any) -> bool:
    numeric = (int, float)
    comparable = (
        isinstance(left, numeric)
        and isinstance(right, numeric)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    ) or (isinstance(left, str) and isinstance(right, str))
    if not comparable:
        raise ExpressionError(f"cannot compare {type(left).__name__} {op} {type(right).__name__}")
    return bool(_ORDER[op](left, right))
