"""Splitting a big module must not break anyone who imports from it.

The failure this prevents: testcase_writer.py (2,592 lines), coding_agent.py (1,916),
stages.py (1,750) and dom_inspector.py (1,752) were split into focused modules. Other code
and the test suite import and patch names through the ORIGINAL module paths, so each moved
name must still resolve there - and resolve to the SAME object, so a function is not
silently duplicated.
"""

import importlib

import pytest

#: module the name used to live in -> (module it lives in now, names)
MOVED = {
    "agents.testcase_writer": {
        "agents.testcase_sheet": ["COLUMNS", "MANUAL_COLUMNS", "_write_excel", "_to_cell_text"],
        "agents.testcase_design_docs": ["fetch_figma_context", "_read_local_design_doc"],
        "agents.testcase_api_spec": ["fetch_api_spec", "render_api_contract", "api_spec_to_stories",
                                     "_MAX_OPS_PER_API_STORY"],
        "agents.testcase_jira": ["fetch_jira_story", "fetch_jira_hierarchy",
                                 "fetch_jira_story_from_file"],
        "agents.testcase_import": ["import_existing_test_cases", "import_existing_test_cases_folder",
                                   "render_reference_baseline"],
        "agents.testcase_limits": ["_MAX_PARALLEL_STORIES"],
    },
    "agents.coding_agent": {
        "agents.ts_check": ["_check_syntax", "_check_syntax_status", "_SYNTAX_FAIL", "_run_with_retry",
                            "suggest_property_owner"],
        "agents.coding_cases": ["read_test_case_sheet", "filter_automatable_cases", "_load_planner_plans",
                                "_group_cases_by_target_file", "_part_label", "_insert_part_suffix",
                                "_missing_title_prefixes", "_TEST_TITLE_RE"],
        "agents.coding_dom": ["_cases_needing_reinspection", "_DOM_CONFIDENCE_THRESHOLD"],
        "agents.coding_repair": ["_prior_output_block", "_repair_unverified_locators",
                                 "_inline_locators_in_spec"],
    },
    "qa_pipeline.orchestrator.stages": {
        "qa_pipeline.orchestrator.heal_support": ["heal_succeeded", "environment_failure_reason",
                                                  "_regressions", "_dependent_specs"],
        "qa_pipeline.orchestrator.review_support": ["_classify_every_failure", "_merge_known_flaky"],
        "qa_pipeline.orchestrator.code_support": ["empty_generation_result", "_merge_repair",
                                                  "_restore_best_attempt"],
        "qa_pipeline.orchestrator.resume_support": ["_rehydrate_test_cases", "_rehydrate_run"],
    },
    "utils.dom_inspector": {
        "utils.dom_snapshot": ["_snapshot", "_parse_mcp_snapshot"],
    },
}

CASES = [(old, new, name) for old, targets in MOVED.items()
         for new, names in targets.items() for name in names]


@pytest.mark.parametrize("old,new,name", CASES, ids=[f"{o.split('.')[-1]}.{n}" for o, _, n in CASES])
def test_a_moved_name_still_resolves_to_the_same_object(old, new, name):
    old_module, new_module = importlib.import_module(old), importlib.import_module(new)
    assert hasattr(old_module, name), f"{old}.{name} no longer resolves"
    assert getattr(old_module, name) is getattr(new_module, name), (
        f"{old}.{name} is a different object from {new}.{name}")


def test_the_big_modules_stay_small():
    # A ratchet, not a target: nobody re-inflates these without noticing.
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    limits = {"agents/testcase_writer.py": 900, "agents/coding_agent.py": 1150,
              "qa_pipeline/orchestrator/stages.py": 1450, "utils/dom_inspector.py": 1600}
    for path, limit in limits.items():
        lines = len((root / path).read_text(encoding="utf-8").splitlines())
        assert lines <= limit, f"{path} has grown to {lines} lines (limit {limit}); split it, don't grow it"
