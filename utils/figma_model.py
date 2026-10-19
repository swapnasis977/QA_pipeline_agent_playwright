"""Figma node tree -> a logical screen object model.

Reads the JSON that `GET /v1/files/<key>` returns and lists, per screen (a top-level frame of a page),
the UI components a person would test: buttons, text fields, dropdowns, checkboxes, radios, toggles,
sliders, tables, navigation, links and dialogs.

DESIGN INPUT ONLY. Everything here is inferred from layer NAMES and node types, which designers choose
freely, so it is ledger-grade `figma` knowledge (confidence 0.45): it can tell the test case writer
which controls a screen is meant to have, and it must never supply a selector to generated code. Only
an observed source (crawl, recording, confirmed) may do that.

No network and no model: the caller fetches the JSON, this only reads it.
"""

from __future__ import annotations

import re
from typing import Any

#: component type -> a regex over the layer name. First match wins, so order is specificity.
_KINDS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (kind, re.compile(pattern, re.I))
    for kind, pattern in (
        ("checkbox", r"check ?box|tick ?box"),
        ("radio", r"\bradio"),
        ("toggle", r"\btoggle\b|\bswitch\b"),
        ("slider", r"\bslider\b|\brange\b"),
        ("dropdown", r"drop ?-?down|\bselect\b|combo ?box|\bpicker\b"),
        ("text_field", r"text ?-?field|text ?-?box|text ?area|\binput\b|search ?(bar|box|field)?$|\bfield\b"),
        ("table", r"\btable\b|data ?-?grid"),
        ("dialog", r"\bmodal\b|\bdialog\b|\bpopup\b|\bpopover\b"),
        ("navigation", r"\bnav|\bmenu\b|\bsidebar\b|\bbreadcrumb|\btabs?\b|\bheader bar\b"),
        ("link", r"\bhyper ?link\b|\blink\b"),
        ("button", r"\bbutton\b|\bbtn\b|\bcta\b"),
    )
)

#: Node types a designer builds a control from. Plain shapes and vectors are decoration.
_CONTAINERS = {"FRAME", "COMPONENT", "COMPONENT_SET", "INSTANCE", "GROUP", "SECTION"}

#: A control's insides are the control: do not list its label text or its icon as separate components.
_LEAF_KINDS = {"button", "checkbox", "radio", "toggle", "slider", "dropdown", "text_field", "link"}

MAX_COMPONENTS_PER_SCREEN = 80
MAX_TEXTS_PER_SCREEN = 40


def classify(node: dict) -> str | None:
    """The component type a node looks like, from its name; None for anything else."""
    if node.get("type") not in _CONTAINERS:
        return None
    name = str(node.get("name", ""))
    for kind, pattern in _KINDS:
        if pattern.search(name):
            return kind
    return None


def _first_text(node: dict, limit: int = 80) -> str:
    """The first visible text inside a node (a button's caption, a field's placeholder)."""
    stack = [node]
    while stack:
        current = stack.pop(0)
        if current.get("type") == "TEXT" and str(current.get("characters", "")).strip():
            return " ".join(str(current["characters"]).split())[:limit]
        stack.extend(current.get("children") or [])
    return ""


def _walk(node: dict, components: list[dict], texts: list[str]) -> None:
    for child in node.get("children") or []:
        if child.get("visible") is False:
            continue
        kind = classify(child)
        if kind and len(components) < MAX_COMPONENTS_PER_SCREEN:
            components.append({
                "type": kind,
                "name": str(child.get("name", "")),
                "label": _first_text(child),
                "node_id": str(child.get("id", "")),
            })
            if kind in _LEAF_KINDS:
                continue
        elif child.get("type") == "TEXT":
            text = " ".join(str(child.get("characters", "")).split())
            if text and len(texts) < MAX_TEXTS_PER_SCREEN and text not in texts:
                texts.append(text[:120])
        _walk(child, components, texts)


def build_screen_model(file_json: dict[str, Any]) -> dict[str, Any]:
    """{"screens": [{"name", "page", "node_id", "components": [...], "texts": [...]}], ...}."""
    screens = []
    for page in (file_json.get("document") or {}).get("children") or []:
        for frame in page.get("children") or []:
            if frame.get("type") not in {"FRAME", "SECTION", "COMPONENT"} or frame.get("visible") is False:
                continue
            components: list[dict] = []
            texts: list[str] = []
            _walk(frame, components, texts)
            screens.append({
                "name": str(frame.get("name", "")),
                "page": str(page.get("name", "")),
                "node_id": str(frame.get("id", "")),
                "components": components,
                "texts": texts,
            })
    return {
        "source": "figma",
        "confidence": 0.45,
        "note": "Inferred from layer names. Describes what the design intends; never a locator source.",
        "screens": screens,
    }


def render_markdown(model: dict[str, Any]) -> str:
    """The model as a page a reviewer can read."""
    lines = ["# Screen object model (from Figma)", "",
             f"_{model.get('note', '')}_", ""]
    for screen in model.get("screens", []):
        lines += [f"## {screen['name']}  ({screen['page']})", ""]
        if screen["components"]:
            lines += ["| Type | Layer name | Label |", "|---|---|---|"]
            lines += [f"| {c['type']} | {c['name']} | {c['label']} |" for c in screen["components"]]
        else:
            lines.append("_No components recognised from layer names._")
        lines.append("")
    return "\n".join(lines)
