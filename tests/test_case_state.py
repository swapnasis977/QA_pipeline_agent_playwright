"""pipeline_state.yml must tell the truth about each case, and never invent progress.

Each test opens with the failure it prevents.
"""

import json

from qa_pipeline.orchestrator import case_state
from utils.run_layout import artifact_path


def _write(feature, name, data):
    path = artifact_path(feature, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _run(tmp_path, locator="#save", spec_text="spec v1", with_page=True):
    feature, repo = tmp_path / "run", tmp_path / "repo"
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "src/pages").mkdir(parents=True, exist_ok=True)
    _write(feature, "test_cases.json", [{"test_case_id": "TC-1", "title": "save"}])
    _write(feature, "automation_plan.json", [{
        "test_case_id": "TC-1", "test_kind": "ui",
        "elements": [{"logical_name": "Save", "screen": "Register", "locator": locator}]}])
    files = [{"path": "tests/a.spec.ts", "kind": "spec", "test_case_ids": ["TC-1"]}]
    (repo / "tests/a.spec.ts").write_text(spec_text, encoding="utf-8")
    if with_page:
        files.append({"path": "src/pages/P.ts", "kind": "page", "test_case_ids": ["TC-1"]})
        (repo / "src/pages/P.ts").write_text("page", encoding="utf-8")
    _write(feature, "generated_files.json", files)
    return feature, repo


def _status(feature, repo):
    case_state.refresh(feature, repo)
    return case_state.derive(feature, repo, {"built_from": case_state.load_built_from(
        artifact_path(feature, case_state.STATE_FILE))})["cases"]["TC-1"]


def test_an_unresolved_locator_blocks_the_case_instead_of_reporting_it_as_planned(tmp_path):
    feature, repo = _run(tmp_path, locator="")
    case = _status(feature, repo)
    assert case["locators"]["status"] == "blocked"
    assert case["page_object"]["status"] == "pending"
    assert case["script"]["status"] == "pending"
    assert case["next"] == "locators"


def test_a_script_is_not_done_just_because_its_file_was_listed_but_is_gone(tmp_path):
    feature, repo = _run(tmp_path)
    (repo / "tests/a.spec.ts").unlink()
    assert _status(feature, repo)["script"]["status"] == "missing"


def test_a_changed_locator_marks_the_untouched_script_stale_until_it_is_regenerated(tmp_path):
    feature, repo = _run(tmp_path)
    assert _status(feature, repo)["next"] == "complete"
    # the ledger re-resolves the element; nobody has rebuilt the page object or script
    _write(feature, "automation_plan.json", [{
        "test_case_id": "TC-1", "test_kind": "ui",
        "elements": [{"logical_name": "Save", "screen": "Register", "locator": "#save2"}]}])
    case = _status(feature, repo)
    assert case["page_object"]["status"] == "stale"
    assert case["script"]["status"] == "stale"
    assert case["next"] == "page_object"
    # regenerating (file content changes) makes it fresh again
    (repo / "src/pages/P.ts").write_text("page v2", encoding="utf-8")
    (repo / "tests/a.spec.ts").write_text("spec v2", encoding="utf-8")
    assert _status(feature, repo)["next"] == "complete"


def test_a_case_served_by_a_shared_page_object_is_not_stuck_waiting_for_its_own(tmp_path):
    feature, repo = _run(tmp_path, with_page=False)
    case = _status(feature, repo)
    assert case["page_object"]["status"] == "n/a"
    assert case["script"]["status"] == "done"
    assert case["next"] == "complete"


def test_the_state_file_is_valid_yaml_shaped_and_round_trips_its_hashes(tmp_path):
    feature, repo = _run(tmp_path)
    path = case_state.refresh(feature, repo)
    text = path.read_text(encoding="utf-8")
    assert '"TC-1":' in text and "locators:" in text
    built = case_state.load_built_from(path)
    assert set(built["TC-1"]) == {"locators", "page_object", "script"}
