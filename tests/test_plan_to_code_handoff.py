"""The Planner -> Coding Agent handoff: what survives it, and in what shape.

Everything the Coding Agent knows about a case's real screen arrives through
`planner_doc.json`. Two things were quietly lost in transit:

1. THE BROWSER EVIDENCE. `automation_planner.run()` drives a real browser and
   attaches the result (`dom_inspection_result` and its grade) to each plan
   AFTER the model has spoken. A plan the gate then repaired went back through
   `adapters.replan`, which calls `plan_cases` directly and never inspects —
   and the repaired plan REPLACED the first-pass one wholesale. So the case the
   gate had just corrected was the one that reached the Coding Agent with no
   snapshot at all, and if the repaired plan happened to say
   needs_dom_inspection: false, nothing re-inspected it either: no DOM in the
   generation prompt, no snapshot for `_verify_file_locators`, and therefore no
   locator repair pass. The most-scrutinized case got the least grounding.

2. THE PATH SHAPE. `adapters.normalize_plan` normalizes `target_file` to
   repo-relative forward slashes precisely because a backslashed or "./"-
   prefixed path is a regex that matches no test file. But that normalization
   lands in `automation_plan.json`, while the Coding Agent reads
   `planner_doc.json` — written raw by `automation_planner.run()` and (before
   this) only rewritten when a repair happened. On the ordinary no-repair run,
   and on every direct `python agents/automation_planner.py` invocation, the
   Coding Agent grouped cases by an un-normalized path that the rest of the
   pipeline had already agreed was invalid.

Run with:  python -m pytest tests/test_plan_to_code_handoff.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import coding_agent  # noqa: E402
from qa_pipeline.agents import adapters  # noqa: E402

# --- 1. browser evidence must survive a gate repair ------------------------

def _first_pass_plan():
    """A plan as `automation_planner.run()` leaves it: the model's own fields,
    plus the inspection block the browser pass attached afterward."""
    return {
        "test_case_id": "TC-001",
        "target_file": "tests/e2e/register.spec.ts",
        "test_kind": "e2e",
        "navigation_keywords": ["Registration", "Capture"],
        "elements": [{"logical_name": "Start Capture", "screen": "Capture",
                      "card_id": "c1", "locator": "getByRole('button')"}],
        "needs_dom_inspection": True,
        "dom_inspection_confidence": 0.9,
        "dom_inspection_quality": "ok",
        "dom_inspection_context": "Registration Capture",
        "dom_inspection_result": {"logged_in": True, "navigated_by_keyword": True,
                                  "elements": [{"name": "Start Capture"}] * 12},
    }


def test_a_repair_that_keeps_the_click_path_keeps_the_snapshot():
    """The gate's complaint was about naming, not navigation — the browser
    evidence for that screen is still valid and must not be thrown away."""
    original = _first_pass_plan()
    repaired = {
        "test_case_id": "TC-001",
        "target_file": "tests/e2e/register.spec.ts",
        "test_kind": "e2e",
        "navigation_keywords": ["Registration", "Capture"],  # unchanged
        # The gate made it rename the element to the app's real label.
        "elements": [{"logical_name": "Start Capture", "screen": "Capture",
                      "card_id": "c1", "locator": "getByRole('button')"}],
        "needs_dom_inspection": True,
        "dom_inspection_confidence": 0.9,
    }

    merged = adapters.merge_replanned(original, repaired)

    assert merged["dom_inspection_quality"] == "ok"
    assert merged["dom_inspection_result"] == original["dom_inspection_result"]
    # The repair's own corrections still win where they overlap.
    assert merged["elements"] == repaired["elements"]


def test_a_repair_that_changes_the_click_path_drops_the_stale_snapshot():
    """A different navigation path means the old snapshot is of a different
    screen. Carrying it forward would be worse than carrying nothing — it
    would look like verified evidence for a screen nobody visited."""
    original = _first_pass_plan()
    repaired = {
        **_first_pass_plan(),
        "navigation_keywords": ["Reports", "Export"],  # a different screen
        "dom_inspection_result": None,
        "dom_inspection_quality": None,
    }
    del repaired["dom_inspection_result"]
    del repaired["dom_inspection_quality"]

    merged = adapters.merge_replanned(original, repaired)

    assert "dom_inspection_result" not in merged
    assert "dom_inspection_quality" not in merged
    # And it must be re-inspected downstream rather than coded from nothing.
    assert merged["needs_dom_inspection"] is True
    assert merged["dom_inspection_confidence"] == 0.0


def test_a_dropped_snapshot_makes_the_coding_agent_reinspect(tmp_path):
    """The end of that chain: `_cases_needing_reinspection` must actually pick
    the case up, or the fix above is cosmetic."""
    merged = adapters.merge_replanned(
        _first_pass_plan(),
        {**_first_pass_plan(), "navigation_keywords": ["Reports"]})
    assert "dom_inspection_result" not in merged  # the merge dropped it

    doc = tmp_path / "planner_doc.json"
    doc.write_text(json.dumps([merged]), encoding="utf-8")

    assert coding_agent._cases_needing_reinspection(doc) == {"TC-001"}


# --- 2. the path shape the Coding Agent actually reads ---------------------

def test_coding_agent_normalizes_target_file_it_reads_from_the_planner_doc(tmp_path):
    """`planner_doc.json` can be written by `automation_planner.run()` with no
    orchestrator in the loop at all (its own `_cli`), so the normalization has
    to hold at the READER, not only at the contract boundary."""
    doc = tmp_path / "planner_doc.json"
    doc.write_text(json.dumps([
        {"test_case_id": "TC-001", "target_file": ".\\tests\\ui\\register.spec.ts"},
        {"test_case_id": "TC-002", "target_file": "./tests/ui/register.spec.ts"},
        {"test_case_id": "TC-003", "target_file": "tests/ui/register.spec.ts"},
    ]), encoding="utf-8")

    plans = coding_agent._load_planner_plans(doc)
    assert [p["target_file"] for p in plans] == ["tests/ui/register.spec.ts"] * 3


def test_three_spellings_of_one_path_group_into_one_generation_call(tmp_path):
    """Why it matters: `_group_cases_by_target_file` keys on the raw string, so
    three spellings of one file became three independent LLM calls, each
    generating its own version of the same spec and the last one silently
    overwriting the rest."""
    doc = tmp_path / "planner_doc.json"
    doc.write_text(json.dumps([
        {"test_case_id": "TC-001", "target_file": ".\\tests\\ui\\register.spec.ts"},
        {"test_case_id": "TC-002", "target_file": "./tests/ui/register.spec.ts"},
        {"test_case_id": "TC-003", "target_file": "tests/ui/register.spec.ts"},
    ]), encoding="utf-8")
    cases = [{"Test Case ID": f"TC-00{n}"} for n in (1, 2, 3)]

    groups = coding_agent._group_cases_by_target_file(cases, doc)

    assert list(groups) == ["tests/ui/register.spec.ts"]
    assert len(groups["tests/ui/register.spec.ts"]) == 3


def test_normalize_plan_and_the_coding_agent_agree_on_one_path(tmp_path):
    """The two sides of the handoff must not disagree about what the file is
    called — that disagreement is the whole bug."""
    raw = {"test_case_id": "TC-001", "target_file": ".\\tests\\ui\\register.spec.ts"}
    doc = tmp_path / "planner_doc.json"
    doc.write_text(json.dumps([raw]), encoding="utf-8")

    contract_side = adapters.normalize_plan(raw)["target_file"]
    coder_side = coding_agent._load_planner_plans(doc)[0]["target_file"]
    assert contract_side == coder_side == "tests/ui/register.spec.ts"
