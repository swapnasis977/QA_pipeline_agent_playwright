"""The final automation sheet must cover every tool control without padding or losing tests.

Failures these prevent:
  - 543 cases for ONE screen: every story was handed the same design doc, so even a
    "CI/CD setup" story produced "verify Upload mode shows a single image panel". Automating
    all of them is hours of duplicate runs against a slow app.
  - The opposite mistake, which is worse: merging by wording alone folds "Zoom In" into
    "Zoom Out" (the names differ by one word), silently dropping a control from the suite.
  - Dropping a case that already has a script, or one that is the only case for a feature.
"""

import openpyxl

from agents.coding_cases import read_test_case_sheet
from utils import final_suite as fs
from utils import master_sheet as ms


def _row(name, execution="Automated", status=ms.AUTOMATABLE, test_id=None, auto_id="", **extra):
    return {"Name": name, "Description": "", "Execution Type (as written)": execution,
            "Automation Status": status, "Test Case ID": test_id or f"S-1-{abs(hash(name)) % 9999}",
            "Automation ID": auto_id, "Expected Result": "1. ok", "Test Step": "1. do",
            "Story": "PROJ-1", "Story title": "t", "_source": {"Name": name, "Execution Type": execution},
            **extra}


def _names(result):
    return [r["Name"] for r in result["suite"]]


def test_zoom_in_and_zoom_out_are_never_merged_however_alike_their_wording():
    result = fs.select([_row("Verify the toolbar Zoom In button scales the image up"),
                        _row("Verify the toolbar Zoom Out button scales the image up")])
    assert len(result["suite"]) == 2


def test_rotate_left_and_rotate_right_and_each_flip_stay_distinct():
    names = ["Verify Rotate Left turns the image", "Verify Rotate Right turns the image",
             "Verify Flip Horizontal mirrors the image", "Verify Flip Vertical mirrors the image"]
    assert len(fs.select([_row(n) for n in names])["suite"]) == 4


def test_each_adjustment_slider_keeps_its_own_case():
    names = [f"Verify the {s} slider updates the image in real time"
             for s in ("Brightness", "Contrast", "Sharpen", "Blur", "Opacity")]
    assert len(fs.select([_row(n) for n in names])["suite"]) == 5


def test_the_same_behaviour_worded_differently_becomes_one_case_with_the_rest_recorded():
    result = fs.select([
        _row("Verify Overlay view renders blended composite of Applicant and Candidate images", test_id="A"),
        _row("Verify Overlay mode renders two images blended in a single container with Blend slider", test_id="B"),
        _row("Verify switching to Overlay mode shows both Applicant and Candidate panels blended", test_id="C")])
    suite = result["suite"]
    assert len(suite) < 3
    assert result["merged"] == 3 - len(suite)
    kept_ids = {r["Test Case ID"] for r in suite}
    recorded = {i.strip() for r in suite for i in r["Merged case IDs"].split(",") if i.strip()}
    assert kept_ids | recorded == {"A", "B", "C"}        # nothing silently lost


def test_a_case_that_already_has_a_script_is_always_kept_even_beside_a_near_twin():
    result = fs.select([
        _row("Verify the Grid toggle button shows and hides the grid on the canvas",
             status=ms.AUTOMATED, test_id="S-1", auto_id="AUTO-1"),
        _row("Verify the Grid toggle button shows and hides a grid on the canvas",
             status=ms.AUTOMATED, test_id="S-2", auto_id="AUTO-2")])
    assert len(result["suite"]) == 2


def test_an_existing_script_wins_over_a_better_worded_unscripted_twin_as_the_representative():
    result = fs.select([
        _row("Verify Hide Sidebar collapses the Adjustments panel", test_id="no-script"),
        _row("Verify Hide Sidebar collapses the Adjustments panel and expands the canvas",
             status=ms.AUTOMATED, test_id="has-script", auto_id="AUTO-9")])
    assert [r["Test Case ID"] for r in result["suite"]] == ["has-script"]


def test_manual_only_cases_are_left_out_but_counted():
    result = fs.select([_row("Verify the image looks sharp to a human eye", execution="Manual"),
                        _row("Verify the Pan tool toggles on and off")])
    assert len(result["suite"]) == 1 and result["excluded_manual"] == 1


def test_engineering_cases_are_left_out_but_counted():
    result = fs.select([_row("Verify the components are covered by unit tests and documentation"),
                        _row("Verify the Pan tool toggles on and off")])
    assert result["excluded_engineering"] == 1 and len(result["suite"]) == 1


def test_a_grid_that_overlays_the_image_is_a_toolbar_control_not_overlay_mode():
    assert fs.classify("Verify Grid toggle overlays a grid on the image").startswith("Toolbar")
    assert fs.classify("Verify Overlay mode renders two images blended") == "Overlay mode & blend"


def test_breadcrumb_and_cancel_are_not_filed_under_the_toolbar():
    assert fs.classify("Verify the breadcrumb navigation header shows context") == "Upload, cancel & exit"
    assert fs.classify("Verify cancelling the file browser dialog keeps the canvas state") == "Upload, cancel & exit"


def test_coverage_names_a_control_the_suite_has_no_case_for():
    gaps = [label for label, n in fs.coverage([_row("Verify the Pan tool toggles on and off")]) if n == 0]
    assert "Toolbar - Zoom in" in gaps and "Toolbar - Pan" not in gaps


def test_no_regex_in_the_rules_contains_a_control_character():
    # A "\b" typed into a non-raw string becomes a backspace character and silently breaks the
    # rule. It happened once while building this module.
    for _feature, pattern in fs.FEATURE_RULES:
        assert "\x08" not in pattern, pattern
    for _label, pattern in fs.CONTROLS + fs.BEHAVIOURS + fs.TOOL_CONTROLS:
        assert "\x08" not in pattern, pattern


def test_the_final_sheet_is_one_worksheet_the_pipelines_own_reader_can_load(tmp_path):
    result = fs.select([_row("Verify the Pan tool toggles on and off", test_id="S-1-1"),
                        _row("Verify Zoom In scales the image", test_id="S-1-2", auto_id="AUTO-2")])
    out = fs.write_final_sheet(result["suite"], tmp_path / "final.xlsx")

    assert len(openpyxl.load_workbook(out).sheetnames) == 1
    cases = read_test_case_sheet(out)                       # the Coding Agent's reader, strict
    assert len(cases) == 2
    by_name = {c["Name"]: c for c in cases}
    assert by_name["Verify Zoom In scales the image"]["Test Case ID"] == "AUTO-2"   # script's id wins
    assert {"Feature", "Control / behaviour", "Automation Status"} <= set(cases[0])
