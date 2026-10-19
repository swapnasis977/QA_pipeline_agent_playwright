"""Prompt-building fixes: fewer input tokens and no silently lost cases.

Run with:  python -m pytest tests/test_fixes_prompt_building.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import (  # noqa: E402
    automation_planner,
    coding_dom,
    coding_prompt,
    coding_repair,
    testcase_jira,
    testcase_prompt,
)
from utils import llm_client  # noqa: E402


# --- ADF text: a formatted sentence is ONE line, not one criterion per run ----

def test_inline_formatted_text_is_not_split_into_separate_criteria():
    """A sentence with a bold word arrives as three ADF text nodes. Joining every
    text node with a newline turned it into three 'acceptance criteria'."""
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Acceptance Criteria"}]},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "The "},
            {"type": "text", "text": "capture", "marks": [{"type": "strong"}]},
            {"type": "text", "text": " button is enabled."}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Quality score is shown."}]},
    ]}
    text = testcase_jira._extract_text_from_adf(adf)
    assert text.split("\n") == ["Acceptance Criteria", "The capture button is enabled.",
                                "Quality score is shown."]
    assert len(testcase_jira._extract_acceptance_criteria(text)) == 2


# --- prompt copy of the story ------------------------------------------------

def test_ac_equal_to_description_is_not_sent_twice_but_the_gate_copy_keeps_it():
    story = {"description": "Register a face.", "acceptance_criteria": ["Register a face."]}
    assert "acceptance_criteria" not in testcase_prompt.story_for_prompt(story)
    assert story["acceptance_criteria"] == ["Register a face."]
    real = {"description": "d", "acceptance_criteria": ["a", "b"]}
    assert testcase_prompt.story_for_prompt(real)["acceptance_criteria"] == ["a", "b"]


def test_a_story_that_is_its_own_ancestor_is_listed_once():
    hierarchy = {"ancestors": [{"key": "PROJ-1"}, {"key": "PROJ-0"}], "linked_test_cases": []}
    merged = testcase_prompt.story_with_hierarchy({"story_key": "PROJ-1"}, hierarchy, "PROJ-1")
    assert [a["key"] for a in merged["hierarchy"]["ancestors"]] == ["PROJ-0"]


def test_design_sections_unrelated_to_the_story_are_dropped_unless_nothing_matches():
    figma = {"sections": [{"heading": "Iris capture", "content": "iris scanner screen"},
                          {"heading": "Billing", "content": "invoice table"}]}
    kept = testcase_prompt.relevant_design_data(figma, {"title": "Iris capture flow"})
    assert [s["heading"] for s in kept["sections"]] == ["Iris capture"]
    assert testcase_prompt.relevant_design_data(figma, {"title": "zzzz qqqq"}) is figma


# --- coding agent prompt ------------------------------------------------------

def test_identical_snapshots_are_sent_once_and_failed_inspections_never():
    cases = [{"Test Case ID": "A"}, {"Test Case ID": "B"}, {"Test Case ID": "C"}]
    snap = {"elements": [{"locator": "page.getByRole('button')"}]}
    snaps = {"A": snap, "B": dict(snap), "C": {"error": "timeout"}}
    usable = coding_prompt.usable_snapshots(cases, snaps)
    assert set(usable) == {"A", "B"}
    groups = coding_prompt.dedupe_snapshots(usable)
    assert len(groups) == 1 and groups[0]["test_case_ids"] == ["A", "B"]


def test_a_plan_with_a_fresh_snapshot_drops_its_own_stale_one():
    plans = [{"test_case_id": "A", "dom_inspection_result": {"x": 1}, "dom_inspection_context": "c"},
             {"test_case_id": "B", "dom_inspection_result": {"x": 1}}]
    slim = coding_prompt.slim_plans(plans, {"A"})
    assert "dom_inspection_result" not in slim[0] and "dom_inspection_context" not in slim[0]
    assert "dom_inspection_result" in slim[1]


def test_sheet_rows_drop_outcome_columns_and_empty_values():
    row = {"Test Case ID": "A", "Result": "Fail", "Status": "Reviewed", "Product": "", "Name": "n"}
    assert coding_prompt.prompt_cases([row]) == [{"Test Case ID": "A", "Name": "n"}]


def test_only_the_pages_for_the_chunks_screens_are_described():
    pages = [{"screen": "Register", "path": "p1"}, {"screen": "Billing", "path": "p2"}]
    plans = [{"elements": [{"screen": "Register"}]}]
    assert coding_prompt.pages_for_plans(pages, plans) == [pages[0]]
    assert coding_prompt.pages_for_plans(pages, [{"elements": []}]) == pages


def test_generated_page_objects_are_not_echoed_back_on_a_repair():
    """Their rewrite is discarded, so re-sending them costs tokens for nothing."""
    prior = {"tests/e2e/a.spec.ts": "import { RegisterPage } from '@pages/RegisterPage';",
             "src/pages/RegisterPage.ts": "class RegisterPage {}",
             "src/pages/OtherPage.ts": "class OtherPage {}"}
    block = coding_repair._prior_output_block(
        prior, [{"Test Case ID": "A"}], "tests/e2e/a.spec.ts",
        exclude_paths={"src/pages/registerpage.ts"})
    assert "OtherPage" in block and "class RegisterPage" not in block


def test_a_supporting_file_the_chunks_spec_does_not_import_is_left_out():
    prior = {"tests/e2e/a.spec.ts": "import { RegisterPage } from '@pages/RegisterPage';",
             "src/pages/RegisterPage.ts": "class RegisterPage {}",
             "src/pages/OtherPage.ts": "class OtherPage {}"}
    block = coding_repair._prior_output_block(prior, [{"Test Case ID": "A"}], "tests/e2e/a.spec.ts")
    assert "class RegisterPage" in block and "OtherPage" not in block


# --- re-inspection trigger ----------------------------------------------------

def test_a_plan_with_no_confidence_and_a_usable_snapshot_is_not_reinspected(tmp_path):
    """A missing confidence was read as 0, so every flagged plan was re-driven
    through a live browser. The Planner itself treats missing as 1.0."""
    import json
    doc = tmp_path / "planner_doc.json"
    doc.write_text(json.dumps([{
        "test_case_id": "A", "needs_dom_inspection": True,
        "dom_inspection_quality": "ok"}]), encoding="utf-8")
    assert coding_dom._cases_needing_reinspection(doc) == set()
    doc.write_text(json.dumps([{
        "test_case_id": "A", "needs_dom_inspection": True,
        "dom_inspection_quality": "login_page"}]), encoding="utf-8")
    assert coding_dom._cases_needing_reinspection(doc) == {"A"}


# --- planner prompt -----------------------------------------------------------

def test_planner_batch_size_is_measured_on_what_is_sent(monkeypatch):
    cases = [{"Test Case ID": f"T{i}", "Name": "x" * 50} for i in range(5)]
    per_case = len(automation_planner._compact(cases[0]))
    monkeypatch.setattr(automation_planner, "_BATCH_MAX_CHARS", per_case * 2 + 1)
    assert [len(b) for b in automation_planner._make_batches(cases)] == [2, 2, 1]


def test_planner_caps_what_it_resends_with_every_batch(tmp_path, monkeypatch):
    seen = []

    def fake_call(**kw):
        seen.append(kw["user_prompt"])
        return "[]"

    monkeypatch.setattr(automation_planner, "call_agent", fake_call)
    monkeypatch.setattr(automation_planner, "parse_json_response", lambda *a, **k: [])
    automation_planner.plan_cases([{"Test Case ID": "A"}], "P" * 50000, tmp_path,
                                  prior_context="C" * 50000)
    assert len(seen[0]) < 25000


# --- anthropic call -----------------------------------------------------------

class _FakeUsage:
    input_tokens, output_tokens = 1, 1


class _FakeResponse:
    content, usage, stop_reason = [], _FakeUsage(), "end_turn"


def _capture_system(monkeypatch, **kwargs):
    captured = {}

    class _Messages:
        def create(self, **kw):
            captured.update(kw)
            return _FakeResponse()

    class _Client:
        messages = _Messages()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_anthropic_client", lambda key: _Client())
    llm_client._call_anthropic("m", "SKILL", "EXTRA", "hi", 10,
                               shared_instructions="SHARED", **kwargs)
    return captured["system"]


def test_the_cache_breakpoint_sits_on_the_stable_prefix_not_the_per_call_block(monkeypatch):
    """With the breakpoint on a block that changes every call, the stable
    conventions+skill prefix was written to the cache and never read."""
    system = _capture_system(monkeypatch)
    assert "cache_control" in system[1] and "cache_control" not in system[2]
    stable = _capture_system(monkeypatch, extra_system_stable=True)
    assert "cache_control" in stable[1] and "cache_control" in stable[2]


def test_a_logging_failure_cannot_lose_a_paid_response(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_client, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(llm_client, "_call_anthropic",
                        lambda *a, **k: ("the answer", {"output_tokens": 3, "truncated": True}))
    monkeypatch.setattr(llm_client, "_log_usage",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(llm_client, "_resolve_model", lambda *a, **k: "m")
    monkeypatch.setattr(llm_client, "_load_skill", lambda *_: "skill")
    monkeypatch.setattr(llm_client, "_load_shared_instructions", lambda: "")
    assert llm_client.call_agent("coding_agent", "p", tmp_path) == "the answer"
    assert llm_client.last_usage.get("truncated") is True
