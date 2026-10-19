"""Shaping what one Coding Agent chunk is sent: compact JSON, deduplicated DOM snapshots, only the context that chunk can use.

Everything here is pure (no I/O, no model) so it can be tested directly. The
Coding Agent used to send every chunk the same indent=2 payloads — each plan's
own DOM snapshot AND a fresh per-case snapshot of the same screen, one copy of
the snapshot per case, every generated page object's member list — which is
input tokens paid for data the chunk cannot act on.
"""

from __future__ import annotations

import json

from agents.automation_planner import _strip_planning_irrelevant_fields
from utils.dom_snapshot import compact as _compact_snapshot

#: Plan keys carrying the Planner's own DOM snapshot. Dropped from a chunk's
#: plan slice when a fresh, usable snapshot for that case travels with the
#: prompt instead (the prompt tells the model the fresh one wins anyway).
_PLAN_DOM_KEYS = ("dom_inspection_result", "dom_inspection_context")

#: Ceiling on the generated-page-object member listing sent to one chunk.
_MAX_POM_DESCRIPTION_CHARS = 12000


def compact_json(obj) -> str:
    """Model-bound JSON: no indentation, no spaces after separators."""
    return json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)


def prompt_cases(chunk_cases: list) -> list:
    """The sheet rows as the model needs them: execution-outcome columns
    (Result, Status) and empty values removed."""
    rows = []
    for row in _strip_planning_irrelevant_fields(chunk_cases or []):
        rows.append({k: v for k, v in row.items()
                     if v not in (None, "", [], {}) or k == "Test Case ID"})
    return rows


def usable_snapshots(chunk_cases: list, dom_snapshots: dict) -> dict:
    """{case id: snapshot} for this chunk, skipping failed inspections.

    A failed re-inspection is stored as {"error": ...}; it carries no element
    and must not be presented to the model as a snapshot that outranks the
    Planner's own."""
    out = {}
    for case in chunk_cases or []:
        cid = case.get("Test Case ID")
        snap = (dom_snapshots or {}).get(cid)
        if isinstance(snap, dict) and snap and not snap.get("error"):
            out[cid] = snap
    return out


def dedupe_snapshots(snapshots: dict) -> list:
    """Collapses byte-identical snapshots (cases on one screen) into one entry
    listing every case id it applies to."""
    groups: dict = {}
    for cid, snap in snapshots.items():
        # Token-lean copy for the prompt only (verifiers keep the full shape):
        # role rows carry role+name instead of a JSON-in-JSON locator string.
        snap = _compact_snapshot(snap)
        key = json.dumps(snap, sort_keys=True, default=str)
        groups.setdefault(key, {"test_case_ids": [], "snapshot": snap})["test_case_ids"].append(cid)
    return list(groups.values())


def slim_plans(plans: list, fresh_ids) -> list:
    """The plans with their own DOM snapshot removed for cases that have a
    fresh one elsewhere in the prompt."""
    fresh = set(fresh_ids or ())
    return [{k: v for k, v in plan.items() if k not in _PLAN_DOM_KEYS}
            if plan.get("test_case_id") in fresh else plan
            for plan in plans]


def pages_for_plans(pages: list, plans: list) -> list:
    """Generated page objects for the screens this chunk's plans touch.

    Falls back to every page when none can be tied to the chunk (a case that
    only reuses an existing page object has no elements of its own), so a spec
    is never left without the classes it must import."""
    screens = {str(el.get("screen") or "").strip()
               for plan in plans or [] for el in (plan.get("elements") or [])
               if isinstance(el, dict)}
    picked = [p for p in pages or [] if str(p.get("screen") or "").strip() in screens]
    return picked or list(pages or [])


def cap_text(text: str, limit: int = _MAX_POM_DESCRIPTION_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n  - ... (member list truncated)"
