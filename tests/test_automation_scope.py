"""The final automation list must follow what is actually ON the screen, and what scripts already cover.

Failures these prevent:
  - 543 cases for one screen: stories were all given the same design doc, so most describe
    overlay / swipe / difference / sync modes this screen does not have. Automating them would
    produce tests that can never pass.
  - A tablet-sized overlay case slipping through as "new": a generic "tablet" rule matched before
    the "not on this screen" rules (it happened - first match wins, so ORDER is behaviour).
  - Writing a new script for something an existing script already checks.
  - Losing a case because a rule named a script that is not in the suite.
Rules are application knowledge, so they live in the PROFILE; the engine only applies them.
"""

import json
import re
from pathlib import Path

from utils import final_suite as fs
from utils import master_sheet as ms

SCOPE = {
    "decisions": dict(fs._DEFAULT_DECISIONS),
    "rules": [
        {"pattern": "overlay|swipe", "decision": "not_on_screen", "note": "second image"},
        {"pattern": "tablet|1024", "decision": "new", "note": "viewport"},
        {"pattern": "rotates? left", "decision": "covered", "by": ["AUTO-TC-091"]},
        {"pattern": "ghost", "decision": "covered", "by": ["AUTO-TC-999"]},      # target missing
        {"pattern": "needs axe", "decision": "blocked"},
    ],
    "default": "new",
}


def _row(name, execution="Automated", status=ms.AUTOMATABLE, auto_id="", test_id=None):
    return {"Name": name, "Description": "", "Execution Type (as written)": execution,
            "Automation Status": status, "Automation ID": auto_id,
            "Test Case ID": test_id or f"S-1-{abs(hash(name)) % 99999}",
            "Expected Result": "1. ok", "Test Step": "1. do", "Story": "PROJ-1", "Story title": "",
            "_source": {}}


def _decision(name, **kw):
    row = _row(name, **kw)
    fs.annotate([row], SCOPE)
    return row["Scope Decision"]


def test_a_tablet_sized_overlay_case_is_not_on_this_screen_not_a_new_script():
    # The ordering bug: the generic tablet rule must NOT outrank the off-screen rules.
    assert _decision("Verify Overlay view mode functions correctly at tablet resolution (1024x768)") \
        == "Not on this screen"
    assert _decision("Verify the Pan tool works at tablet resolution (1024x768)") == "New script needed"


def test_the_first_matching_rule_wins():
    assert _decision("Verify swipe at tablet size") == "Not on this screen"


def test_an_unmatched_case_takes_the_files_default():
    assert _decision("Verify something nobody wrote a rule for") == "New script needed"


def test_a_manual_case_stays_manual_and_a_scripted_case_stays_automated_whatever_the_rules_say():
    assert _decision("Verify overlay by eye", execution="Manual") == "Manual only"
    assert _decision("Verify overlay blend", status=ms.AUTOMATED, auto_id="AUTO-TC-001") \
        == "Already automated"


def test_a_covered_case_is_attached_to_the_script_that_covers_it_not_listed_again():
    rows = [_row("Verify Rotate Left turns the image", status=ms.AUTOMATED, auto_id="PROJ-1-AUTO-TC-091",
                 test_id="PROJ-1-AUTO-TC-091"),
            _row("Verify the toolbar rotates left correctly", test_id="S-7-TC-004")]
    result = fs.select(rows, SCOPE)

    assert [r["Test Case ID"] for r in result["suite"]] == ["PROJ-1-AUTO-TC-091"]
    assert result["covered"] == 1
    assert "S-7-TC-004" in result["suite"][0]["Merged case IDs"]


def test_a_rule_that_names_a_missing_script_never_loses_the_case():
    result = fs.select([_row("Verify the ghost control", test_id="S-1-ghost")], SCOPE)

    assert [r["Test Case ID"] for r in result["suite"]] == ["S-1-ghost"]
    assert result["suite"][0]["Scope Decision"] == "New script needed"
    assert "not found" in result["suite"][0]["Decision Note"]


def test_off_screen_cases_are_counted_and_left_out_of_the_suite():
    result = fs.select([_row("Verify Overlay mode blends two images"), _row("Verify Pan toggles")], SCOPE)

    assert result["excluded_not_on_screen"] == 1
    assert [r["Name"] for r in result["suite"]] == ["Verify Pan toggles"]


def test_blocked_cases_stay_in_the_suite_so_the_missing_prerequisite_is_visible():
    result = fs.select([_row("Verify colour contrast, needs axe")], SCOPE)

    assert result["suite"][0]["Scope Decision"] == "Blocked - needs test data or a tool"


def test_without_a_scope_file_selection_behaves_exactly_as_before():
    result = fs.select([_row("Verify Overlay mode blends two images")])

    assert len(result["suite"]) == 1 and "excluded_not_on_screen" in result


def test_a_missing_scope_file_is_not_an_error(tmp_path):
    assert fs.load_scope(tmp_path / "nope.json") is None


def test_an_owner_exclusion_is_counted_and_left_out_but_never_silent():
    scope = {**SCOPE, "decisions": {**SCOPE["decisions"], "excluded": "Excluded by decision"},
             "rules": [{"pattern": "aria", "decision": "excluded", "note": "removed on request"}]
             + SCOPE["rules"]}
    rows = [_row("Verify ARIA labels on all buttons"), _row("Verify Pan toggles")]

    result = fs.select(rows, scope)

    assert result["excluded_by_decision"] == 1
    assert [r["Name"] for r in result["suite"]] == ["Verify Pan toggles"]
    assert next(r for r in rows if "ARIA" in r["Name"])["Decision Note"] == "removed on request"


def test_an_exclusion_never_removes_a_case_that_already_has_a_script():
    scope = {**SCOPE, "decisions": {**SCOPE["decisions"], "excluded": "Excluded by decision"},
             "rules": [{"pattern": "tablet", "decision": "excluded"}] + SCOPE["rules"]}
    row = _row("Verify zoom at tablet size", status=ms.AUTOMATED, auto_id="PROJ-1-AUTO-TC-057")

    assert len(fs.select([row], scope)["suite"]) == 1
