"""A tiny, dependency-free schema validator.

Why not pydantic: this package has to be importable from the two agents that
must work with NO third-party stack configured at all (bug_reporter and
jira_uploader run with no API key — see their module docstrings), and the
existing codebase deliberately kept `agents/artifact_io.py` dependency-free
for the same reason. Adding a hard dependency here would make the contract
layer the one thing that could stop those from importing.

Why validate at all — the failure this exists to prevent:

`run_pipeline._resolve_case_file()` tries FOUR independent strategies to work
out which generated file a failing test case belongs to (the Coding Agent's
own `test_case_ids`, the Planner's `target_file`, the Reviewer's `file`, then
a unique-basename match) and can still return None. That function is not a
bug. It is the shape a missing contract takes: three agents each emit their
own guess at the same join, nothing checks any of them, and the orchestrator
is left refereeing at the end — by which point the run is already ruined.

With `generated_file.test_case_ids` declared required and validated on write,
the Coding Agent fails immediately and loudly if it omits it, and the whole
four-tier fallback collapses to one dictionary lookup that cannot miss.

Design notes:
  - Errors are COLLECTED, not raised one at a time. A model that produced
    six malformed fields should be told about all six in one repair prompt,
    not made to round-trip six times.
  - Every error string is written to be pasted straight back to the model
    as a repair instruction, so it names the path, what was expected, and
    what arrived.
  - Unknown keys are allowed by default. Models routinely add a helpful
    extra field, and rejecting that would be pedantry, not safety. Set
    `strict=True` on a Schema where extra keys genuinely indicate confusion.
"""

from __future__ import annotations

import re

#: Maps our schema type names to the Python types that satisfy them. `float`
#: accepts int on purpose: JSON has one number type, so a confidence that
#: round-trips as `1` rather than `1.0` is the same value, not an error.
_TYPES = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
    "any": (object,),
}


class ValidationError(Exception):
    """Raised by `Schema.check()`. Carries the full error list, not just the
    first one, so a caller building a repair prompt has everything."""

    def __init__(self, schema_name: str, errors: list):
        self.schema_name = schema_name
        self.errors = list(errors)
        super().__init__(
            f"{schema_name}: {len(self.errors)} contract violation(s):\n  - "
            + "\n  - ".join(self.errors)
        )

    def as_repair_prompt(self) -> str:
        """The error list phrased as an instruction to the model that
        produced the artifact. Used by `verifiers.repair_loop`."""
        return (
            f"Your previous response did not satisfy the '{self.schema_name}' "
            f"contract. Fix EXACTLY these problems and return the corrected "
            f"JSON only:\n  - " + "\n  - ".join(self.errors)
        )


class Field:
    """One field in a schema.

    `of` carries the element schema for a list of objects, or the value type
    name for a list of scalars ("str"). `choices` restricts to a fixed set —
    used for the enum-ish fields (execution type, classification verdict,
    provenance) where a model inventing a fifth value silently breaks the
    deterministic code downstream that switches on them.
    """

    __slots__ = ("name", "kind", "required", "default", "choices", "of",
                 "min_items", "pattern", "desc", "min_value", "max_value")

    def __init__(self, name, kind="str", required=True, default=None,
                 choices=None, of=None, min_items=None, pattern=None,
                 desc="", min_value=None, max_value=None):
        if kind not in _TYPES:
            raise ValueError(f"Unknown field kind {kind!r} for field {name!r}.")
        self.name = name
        self.kind = kind
        self.required = required
        self.default = default
        self.choices = choices
        self.of = of
        self.min_items = min_items
        self.pattern = re.compile(pattern) if pattern else None
        self.desc = desc
        self.min_value = min_value
        self.max_value = max_value

    def validate(self, value, path: str) -> list:
        errors = []

        if not isinstance(value, _TYPES[self.kind]):
            # bool is a subclass of int in Python, so an `int` field would
            # silently accept True. Caught explicitly because a confidence
            # or a count arriving as a boolean is a real model mistake worth
            # reporting rather than coercing.
            errors.append(
                f"{path}: expected {self.kind}, got "
                f"{type(value).__name__} ({value!r})")
            return errors
        if self.kind in ("int", "float") and isinstance(value, bool):
            errors.append(f"{path}: expected {self.kind}, got bool ({value!r})")
            return errors

        if self.choices is not None and value not in self.choices:
            errors.append(
                f"{path}: {value!r} is not one of the allowed values "
                f"{sorted(self.choices)}")

        if self.pattern is not None and isinstance(value, str) and not self.pattern.match(value):
            errors.append(
                f"{path}: {value!r} does not match the required format "
                f"{self.pattern.pattern!r}")

        if self.min_value is not None and value < self.min_value:
            errors.append(f"{path}: {value!r} is below the minimum {self.min_value}")
        if self.max_value is not None and value > self.max_value:
            errors.append(f"{path}: {value!r} is above the maximum {self.max_value}")

        if self.kind == "list":
            if self.min_items is not None and len(value) < self.min_items:
                errors.append(
                    f"{path}: needs at least {self.min_items} item(s), got {len(value)}")
            for i, item in enumerate(value):
                item_path = f"{path}[{i}]"
                if isinstance(self.of, Schema):
                    errors.extend(self.of.validate(item, item_path))
                elif isinstance(self.of, str) and not isinstance(item, _TYPES[self.of]):
                    errors.append(
                        f"{item_path}: expected {self.of}, got "
                        f"{type(item).__name__} ({item!r})")

        if self.kind == "dict" and isinstance(self.of, Schema):
            errors.extend(self.of.validate(value, path))

        return errors


class Schema:
    """A named set of fields.

    `validate()` returns an error list (possibly empty). `check()` raises.
    `coerce()` fills defaults for absent optional fields so downstream code
    can index without `.get()` chains — but it never invents a value for a
    REQUIRED field, because silently defaulting a required field is exactly
    how a missing join goes undetected until it breaks something far away.
    """

    def __init__(self, name: str, fields: list, strict: bool = False, desc: str = ""):
        self.name = name
        self.fields = list(fields)
        self.strict = strict
        self.desc = desc
        self._by_name = {f.name: f for f in self.fields}

    def validate(self, data, path: str = "") -> list:
        prefix = f"{path}." if path else ""
        if not isinstance(data, dict):
            return [f"{path or self.name}: expected an object, got {type(data).__name__}"]

        errors = []
        for field in self.fields:
            present = field.name in data and data[field.name] is not None
            if not present:
                if field.required:
                    errors.append(
                        f"{prefix}{field.name}: required field is missing"
                        + (f" ({field.desc})" if field.desc else ""))
                continue
            errors.extend(field.validate(data[field.name], f"{prefix}{field.name}"))

        if self.strict:
            for key in data:
                if key not in self._by_name:
                    errors.append(f"{prefix}{key}: unexpected field")

        return errors

    def check(self, data, path: str = ""):
        """Validates and raises `ValidationError` on any problem. Returns the
        data unchanged so it can be used inline: `plan = SCHEMA.check(plan)`."""
        errors = self.validate(data, path)
        if errors:
            raise ValidationError(self.name, errors)
        return data

    def coerce(self, data: dict) -> dict:
        """Returns a copy with defaults filled in for absent OPTIONAL fields.
        Required fields are never defaulted — see the class docstring."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for field in self.fields:
            missing = field.name not in out or out[field.name] is None
            if missing and not field.required and field.default is not None:
                out[field.name] = (list(field.default) if isinstance(field.default, list)
                                   else dict(field.default) if isinstance(field.default, dict)
                                   else field.default)
        return out

    def describe(self) -> str:
        """A compact, human- and model-readable rendering of the contract.

        Sent to the model in the system prompt so the shape it is asked for
        and the shape that is enforced come from the SAME declaration — the
        classic drift being a skill file that documents one field name and a
        validator that checks another.
        """
        lines = [f"{self.name} — {self.desc}" if self.desc else self.name]
        for f in self.fields:
            bits = [f.kind]
            if not f.required:
                bits.append("optional")
            if f.choices:
                bits.append("one of " + "|".join(str(c) for c in sorted(f.choices)))
            if isinstance(f.of, Schema):
                bits.append(f"of {f.of.name}")
            elif isinstance(f.of, str):
                bits.append(f"of {f.of}")
            lines.append(f"  {f.name} ({', '.join(bits)})"
                         + (f" — {f.desc}" if f.desc else ""))
        return "\n".join(lines)
