"""A run folder must stay readable: deliverables visible, bookkeeping hidden.

The failure this prevents is the one that prompted it: a feature run left 73
files side by side in one folder (27 sheets, 27 story_context JSONs, four
overlapping run_*.json files), and nobody could tell which to open. Layout is
decided in one function so a new agent that forgets to register its file adds
clutter to `_internal/`, never to the folders a person reads.
"""


from agents.artifact_io import write_json
from qa_pipeline import profiles
from utils.run_layout import AUTOMATION, INTERNAL, RESULTS, TESTCASES, artifact_path, folder_for


def test_an_unregistered_filename_lands_in_internal_not_in_a_visible_folder():
    # The default is the whole point: forgetting to register must be harmless.
    assert folder_for("some_brand_new_agent_diagnostics.json") == INTERNAL
    assert folder_for("coding_agent_rejected_files.json") == INTERNAL
    assert folder_for("token_log.json") == INTERNAL


def test_the_files_a_person_reads_are_not_buried():
    assert folder_for("summary.md") == ""
    assert folder_for("PROJ-1_PROJ-2_TestCases.xlsx") == TESTCASES
    assert folder_for("PROJ-1_Automation_Suite_Consolidated.xlsx") == TESTCASES
    assert folder_for("automation_plan.json") == AUTOMATION
    assert folder_for("BUG_LIST.md") == RESULTS
    assert folder_for("run_results.json") == RESULTS


def test_a_rotated_summary_archive_is_not_mistaken_for_the_live_summary():
    # `summary.20261007_120000.md` must not sit beside the real summary.md.
    assert folder_for("summary.20261007_120000.md") == INTERNAL


def test_a_subpath_is_routed_by_its_first_component(tmp_path):
    assert artifact_path(tmp_path, "evidence/TC-1/fail.png") == \
        tmp_path / RESULTS / "evidence" / "TC-1" / "fail.png"


def test_a_writer_and_a_reader_agree_on_where_a_file_is(tmp_path):
    # Both sides call artifact_path, so they cannot drift apart.
    write_json(artifact_path(tmp_path, "run_results.json"), {"ok": True})
    assert (tmp_path / RESULTS / "run_results.json").is_file()
    assert not (tmp_path / "run_results.json").exists()


def test_runs_are_per_application_so_two_apps_cannot_collide(tmp_path):
    # context/ was shared: two apps using epic "PROJ-1" overwrote each other.
    (tmp_path / "profiles" / "app_a").mkdir(parents=True)
    (tmp_path / "profiles" / "app_b").mkdir(parents=True)
    a = profiles.Profile("app_a", tmp_path / "profiles" / "app_a", {})
    b = profiles.Profile("app_b", tmp_path / "profiles" / "app_b", {})
    assert a.context_root != b.context_root
    assert a.context_root == tmp_path / "runs" / "app_a"


def test_a_profile_from_before_the_restructure_is_still_read(tmp_path):
    # Until scripts/migrate_layout.py runs, knowledge/ must not silently
    # become "no documents".
    root = tmp_path / "profiles" / "old"
    (root / "knowledge").mkdir(parents=True)
    (root / "recordings").mkdir()
    p = profiles.Profile("old", root, {})
    assert p.knowledge_dir == root / "knowledge"
    assert p.recordings_dir == root / "recordings"


def test_once_input_exists_it_wins_over_the_legacy_folder(tmp_path):
    # A half-migrated profile must read ONE folder, never a mix of both.
    root = tmp_path / "profiles" / "mid"
    (root / "knowledge").mkdir(parents=True)
    (root / "input" / "docs").mkdir(parents=True)
    p = profiles.Profile("mid", root, {})
    assert p.knowledge_dir == root / "input" / "docs"


def test_a_new_profile_is_created_on_the_new_layout(tmp_path):
    p = profiles.init("fresh", start=tmp_path)
    assert (p.root / "input" / "docs").is_dir()
    assert (p.root / "input" / "recordings").is_dir()
    assert not (p.root / "knowledge").exists()
