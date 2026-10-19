"""Test data templates: values worth trying, derived from what the design and the cases say.

Two sources, both read, neither invented:
  * limits stated in the test cases ("at most 255 characters", "between 1 and 100", "minimum 8"):
    each becomes the boundary trio just below, at and just above the limit;
  * text-field components of the Figma screen model: each gets the standard negative inputs
    (empty, whitespace, special characters, unicode, over-long, markup) beside a valid placeholder.

The file is a TEMPLATE for an engineer to review and fill in, not data the specs load: real values
(valid users, image paths, biometric samples) are the project's to supply. Deterministic: no model.
"""

from __future__ import annotations

import re
from typing import Any

_NUM = r"(-?\d+(?:\.\d+)?)"
_UNIT = r"(?:\s*(characters?|chars?|digits?|items?|files?|MB|KB|px|%|seconds?|s|ms))?"
_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("between", re.compile(rf"between\s+{_NUM}\s*(?:and|to|-)\s*{_NUM}{_UNIT}", re.I)),
    ("max", re.compile(rf"(?:max(?:imum)?(?:\s+of)?|at most|up to|no more than|not exceed(?:ing)?)\s*{_NUM}{_UNIT}", re.I)),
    ("min", re.compile(rf"(?:min(?:imum)?(?:\s+of)?|at least)\s*{_NUM}{_UNIT}", re.I)),
)


def _num(text: str) -> float | int:
    value = float(text)
    return int(value) if value.is_integer() else value


def _trio(value) -> dict[str, Any]:
    return {"below": value - 1, "at": value, "above": value + 1}


def extract_limits(cases: list[dict]) -> list[dict]:
    """Every limit a case states, with its boundary values; one entry per distinct rule."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for case in cases:
        text = case.get("text") or case.get("title") or ""
        for kind, pattern in _RULES:
            for m in pattern.finditer(text):
                if kind == "between":
                    low, high, unit = _num(m.group(1)), _num(m.group(2)), (m.group(3) or "")
                    key = (kind, low, high, unit.lower())
                    boundary = {"min": _trio(low), "max": _trio(high)}
                    rule = f"between {low} and {high}"
                else:
                    value, unit = _num(m.group(1)), (m.group(2) or "")
                    key = (kind, value, unit.lower())
                    boundary = _trio(value)
                    rule = f"{kind} {value}"
                if key in seen:
                    continue
                seen.add(key)
                out.append({"rule": rule + (f" {unit}" if unit else ""), "kind": kind,
                            "boundary": boundary, "case": case.get("id", ""),
                            "quote": " ".join(text[max(0, m.start() - 40): m.end() + 20].split())})
    return out


def _field_values(label: str) -> dict[str, str]:
    return {
        "valid": f"<a valid value for {label or 'this field'}>",
        "empty": "",
        "whitespace_only": "   ",
        "special_characters": "!@#$%^&*()_+{}|:\"<>?~`",
        "unicode": "Ünïcödé 測試 العربية",
        "over_long": "a" * 256,
        "markup": "<script>alert(1)</script>",
    }


def fields_from_screen_model(model: dict | None) -> list[dict]:
    out = []
    for screen in (model or {}).get("screens", []):
        for comp in screen.get("components", []):
            if comp.get("type") == "text_field":
                label = comp.get("label") or comp.get("name", "")
                out.append({"screen": screen.get("name", ""), "field": comp.get("name", ""),
                            "label": label, "values": _field_values(label)})
    return out


def build(cases: list[dict], screen_model: dict | None = None) -> dict | None:
    """The template, or None when neither the cases nor the design give anything to base one on."""
    limits = extract_limits(cases)
    fields = fields_from_screen_model(screen_model)
    if not limits and not fields:
        return None
    return {
        "note": "Review and fill in. Boundary values come from limits written in the test cases; "
                "field values come from the Figma text fields. Real data (users, images, samples) is yours to add.",
        "limits": limits,
        "fields": fields,
    }
