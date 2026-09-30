"""Structured output: schema checks, parsing, validation and repair prompts."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

MAX_SCHEMA_BYTES = 32 * 1024
_MAX_REPORTED_ERRORS = 5


def check_schema(schema: dict[str, Any]) -> None:
    """Raise ValueError if ``schema`` is not a usable JSON Schema object schema."""
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        raise ValueError("output_schema too large")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid JSON Schema: {exc.message}") from None
    if schema.get("type") != "object":
        raise ValueError("output_schema must describe a JSON object (type: object)")


def parse_and_validate(text: str, schema: dict[str, Any]) -> tuple[Any | None, list[str]]:
    """Return (value, errors). Tolerates a ```json fence around the payload; nothing else."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        candidate = candidate.removeprefix("json").strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, [f"not valid JSON: {exc.msg} at position {exc.pos}"]
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    if errors:
        return None, [
            f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}"
            for e in errors[:_MAX_REPORTED_ERRORS]
        ]
    return value, []


def schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Respond with only a JSON object that validates against this JSON Schema. "
        "No prose, no code fences.\n" + json.dumps(schema, separators=(",", ":"), sort_keys=True)
    )


def repair_message(errors: list[str]) -> str:
    return (
        "Your previous response did not satisfy the required JSON Schema:\n- "
        + "\n- ".join(errors)
        + "\nReturn a corrected JSON object only."
    )
