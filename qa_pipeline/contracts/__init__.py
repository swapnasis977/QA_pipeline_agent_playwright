"""Validated artifact I/O — the layer every stage boundary goes through.

`read_validated` / `write_validated` wrap the existing `agents.artifact_io`
(atomic writes, UTF-8 in both directions, corrupt-JSON tolerance) and add the
contract check. Validating on BOTH sides is deliberate and is not redundant:

  - on WRITE, it fails the producing stage, so the error names the agent that
    actually made the mistake;
  - on READ, it catches an artifact hand-edited between runs, or one written
    by an older CONTRACT_VERSION during a resume.

A read failure is reported and treated as absent rather than raised by
default (`strict=False`), because the pipeline's correct response to a
corrupt artifact is usually "regenerate that stage", not "abort the run" —
the same judgment `artifact_io.read_json` already makes about corrupt JSON.
"""

from __future__ import annotations

from pathlib import Path

from agents.artifact_io import read_json, write_json

from .artifacts import (  # noqa: F401  (re-exported for callers)
    AUTOMATION_PLAN,
    CARD_TYPES,
    CASE_RESULT,
    CLASSIFICATION,
    ELEMENT_VALUE,
    EXECUTION_TYPES,
    GENERATED_FILE,
    HEAL_OUTCOME,
    KNOWLEDGE_CARD,
    PLAN_ELEMENT,
    PROVENANCE,
    REGISTRY,
    RUN_RESULT,
    SCREEN_VALUE,
    SEVERITIES,
    TEST_CASE,
    TEST_KINDS,
    VERDICTS,
)
from .schema import Field, Schema, ValidationError  # noqa: F401

__all__ = [
    "Schema", "Field", "ValidationError", "REGISTRY", "PROVENANCE", "CARD_TYPES",
    "EXECUTION_TYPES", "VERDICTS", "SEVERITIES", "TEST_KINDS", "KNOWLEDGE_CARD", "ELEMENT_VALUE",
    "SCREEN_VALUE", "TEST_CASE", "PLAN_ELEMENT", "AUTOMATION_PLAN", "GENERATED_FILE",
    "CASE_RESULT", "RUN_RESULT", "CLASSIFICATION", "HEAL_OUTCOME",
    "resolve", "validate_many", "check_many", "read_validated", "write_validated",
]


def resolve(schema) -> Schema:
    """Accepts a Schema or its name, so callers can say `"test_case"`."""
    if isinstance(schema, Schema):
        return schema
    try:
        return REGISTRY[schema]
    except KeyError:
        raise ValueError(
            f"Unknown contract {schema!r}. Known: {sorted(REGISTRY)}") from None


def validate_many(schema, items: list) -> list:
    """Validates a list of artifacts, returning ONE flat error list with each
    entry's index in its path. Collected rather than fail-fast so a repair
    prompt can fix every bad row of a 40-case sheet in a single round trip."""
    s = resolve(schema)
    errors = []
    for i, item in enumerate(items or []):
        errors.extend(s.validate(item, f"{s.name}[{i}]"))
    return errors


def check_many(schema, items: list) -> list:
    """`validate_many`, but raises. Returns the coerced items on success."""
    s = resolve(schema)
    errors = validate_many(s, items)
    if errors:
        raise ValidationError(s.name, errors)
    return [s.coerce(item) for item in (items or [])]


def write_validated(path, schema, data, many: bool = False):
    """Validates, then writes atomically. Raises before touching disk.

    Writing only AFTER validation matters for resume: a stage that half-wrote
    a malformed artifact and then died would be skipped on the next run by
    the "output already present" check, silently baking the corruption in.
    """
    s = resolve(schema)
    if many:
        payload = check_many(s, data)
    else:
        s.check(data)
        payload = s.coerce(data)
    write_json(path, payload)
    return payload


def read_validated(path, schema, many: bool = False, strict: bool = False,
                   default=None):
    """Reads and validates. On a contract violation: raises when `strict`,
    otherwise reports on stderr and returns `default` (treat as absent)."""
    s = resolve(schema)
    raw = read_json(path, default=None)
    if raw is None:
        return default if default is not None else ([] if many else None)

    errors = validate_many(s, raw) if many else s.validate(raw, s.name)
    if errors:
        exc = ValidationError(s.name, errors)
        if strict:
            raise exc
        print(f"⚠️  '{Path(path).name}' violates the {s.name} contract "
              f"({len(errors)} problem(s)) — treating it as absent so the stage "
              f"regenerates it. First: {errors[0]}")
        return default if default is not None else ([] if many else None)

    return [s.coerce(i) for i in raw] if many else s.coerce(raw)
