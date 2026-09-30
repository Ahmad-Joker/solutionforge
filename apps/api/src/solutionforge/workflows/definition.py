"""Workflow definition schema and static validation ("compilation").

Everything that can be checked before running is checked here, at version-creation time:
graph integrity, reachability, step configs, expression syntax, references to unknown steps
or undeclared inputs. A version that compiles can still fail at run time (bad data, a
failing tool), but it can never be structurally broken.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from solutionforge.core.errors import ValidationFailed
from solutionforge.workflows import expressions
from solutionforge.workflows.registry import StepHandler, StepRegistry, UnknownStepType

STEP_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
MAX_STEPS = 200


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryPolicy(_Strict):
    max_attempts: int = Field(default=1, ge=1, le=10)
    initial_backoff_seconds: float = Field(default=1.0, ge=0, le=3600)
    multiplier: float = Field(default=2.0, ge=1, le=10)
    max_backoff_seconds: float = Field(default=60.0, ge=0, le=86400)

    def backoff_seconds(self, failed_attempt: int) -> float:
        """Delay before the attempt after ``failed_attempt`` (1-based)."""
        raw = self.initial_backoff_seconds * self.multiplier ** (failed_attempt - 1)
        return float(min(self.max_backoff_seconds, raw))


class Limits(_Strict):
    max_steps: int = Field(default=100, ge=1, le=1000, description="Step attempts, incl. retries")
    max_active_seconds: float = Field(
        default=300, ge=1, le=3600, description="Time spent running steps (excludes waiting)"
    )


class InputField(_Strict):
    type: Literal["string", "number", "integer", "boolean", "object", "array"]
    required: bool = True
    description: str = Field(default="", max_length=500)


class StepSpec(_Strict):
    id: str = Field(pattern=STEP_ID_PATTERN)
    type: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    next: str | None = None
    on_error: str | None = Field(default=None, description="Fallback step if all attempts fail")
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: float = Field(default=30, gt=0, le=600)


class WorkflowDefinition(_Strict):
    schema_version: Literal[1] = 1
    start: str
    steps: list[StepSpec] = Field(min_length=1, max_length=MAX_STEPS)
    inputs: dict[str, InputField] = Field(default_factory=dict)
    output: dict[str, Any] = Field(
        default_factory=dict, description="Mapping resolved against the final scope"
    )
    limits: Limits = Field(default_factory=Limits)


@dataclass(frozen=True, slots=True)
class CompiledStep:
    spec: StepSpec
    handler: StepHandler[Any]
    config: BaseModel


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    definition: WorkflowDefinition
    steps: dict[str, CompiledStep]

    def canonical_json(self) -> dict[str, Any]:
        return self.definition.model_dump(mode="json")

    def hash(self) -> str:
        blob = json.dumps(self.canonical_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


class DefinitionInvalid(ValidationFailed):
    code = "invalid_workflow_definition"

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("Workflow definition is invalid", details={"errors": errors})
        self.errors = errors


def compile_definition(raw: dict[str, Any], registry: StepRegistry) -> CompiledWorkflow:
    try:
        definition = WorkflowDefinition.model_validate(raw)
    except PydanticValidationError as exc:
        raise DefinitionInvalid(
            [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        ) from None

    errors: list[dict[str, Any]] = []

    def err(loc: list[str | int], msg: str) -> None:
        errors.append({"loc": loc, "msg": msg})

    ids = [s.id for s in definition.steps]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        err(["steps"], f"duplicate step ids: {dupes}")
    known = set(ids)
    if definition.start not in known:
        err(["start"], f"start step '{definition.start}' does not exist")

    compiled: dict[str, CompiledStep] = {}
    edges: dict[str, set[str]] = {}
    refs: list[tuple[list[str | int], str]] = []

    for i, spec in enumerate(definition.steps):
        loc: list[str | int] = ["steps", i]
        try:
            handler = registry.get(spec.type)
        except UnknownStepType:
            err([*loc, "type"], f"unknown step type '{spec.type}'; known: {registry.types()}")
            continue
        try:
            config = handler.parse_config(spec.config)
        except PydanticValidationError as exc:
            for e in exc.errors():
                err([*loc, "config", *e["loc"]], e["msg"])
            continue
        compiled[spec.id] = CompiledStep(spec=spec, handler=handler, config=config)

        targets = [spec.next, spec.on_error, *handler.targets(config)]
        for t in targets:
            if t is not None and t not in known:
                err(loc, f"step '{spec.id}' points to unknown step '{t}'")
        if spec.on_error == spec.id:
            err([*loc, "on_error"], "a step cannot be its own error handler")
        edges[spec.id] = {t for t in targets if t is not None}
        refs.extend(([*loc, "config"], r) for r in handler.references(config))

    refs.extend((["output"], r) for r in expressions.references(definition.output))
    for loc, ref in refs:
        _check_reference(ref, loc, known, definition.inputs, err)

    if not errors and definition.start in known:
        reachable = _reachable(definition.start, edges)
        unreachable = [i for i in ids if i not in reachable]
        if unreachable:
            err(["steps"], f"unreachable steps: {unreachable}")

    if errors:
        raise DefinitionInvalid(errors)
    return CompiledWorkflow(definition=definition, steps=compiled)


def _check_reference(
    ref: str,
    loc: list[str | int],
    step_ids: set[str],
    inputs: dict[str, InputField],
    err: Any,
) -> None:
    try:
        path = expressions.parse_path(ref)
    except expressions.ExpressionError as exc:
        err(loc, str(exc))
        return
    if path[0] == "steps" and (len(path) < 2 or path[1] not in step_ids):
        err(loc, f"reference to unknown step: {ref}")
    if path[0] == "input" and inputs and (len(path) < 2 or path[1] not in inputs):
        err(loc, f"reference to undeclared input: {ref}")


def _reachable(start: str, edges: dict[str, set[str]]) -> set[str]:
    seen, queue = {start}, deque([start])
    while queue:
        for nxt in edges.get(queue.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def validate_input(definition: WorkflowDefinition, data: dict[str, Any]) -> None:
    """Check execution input against declared ``inputs`` (if any are declared)."""
    if not definition.inputs:
        return
    errors: list[dict[str, Any]] = []
    for name, spec in definition.inputs.items():
        if name not in data:
            if spec.required:
                errors.append({"loc": ["input", name], "msg": "required input missing"})
            continue
        value = data[name]
        is_bool = isinstance(value, bool)
        ok = isinstance(value, _JSON_TYPES[spec.type]) and (spec.type == "boolean" or not is_bool)
        if not ok:
            errors.append({"loc": ["input", name], "msg": f"expected {spec.type}"})
    for name in data.keys() - definition.inputs.keys():
        errors.append({"loc": ["input", name], "msg": "undeclared input"})
    if errors:
        raise ValidationFailed("Execution input is invalid", details={"errors": errors})
