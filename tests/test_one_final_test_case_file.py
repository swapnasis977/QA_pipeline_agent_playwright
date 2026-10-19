"""Agent 1 must leave ONE visible test-case file per run, not one per story.

The failure this prevents: a 27-story feature run left 27 per-story sheets and
27 story_context JSONs in the folder a person opens, and nobody could say which
was the final set of test cases. Per-story sheets are intermediates; the merged
sheet is the deliverable.
"""

import openpyxl

from agents import testcase_writer as tw


def _fake_story(story_key, epic_key):
    return {"title": f"Story {story_key}", "description": "d", "acceptance_criteria": ["a"]}


def _fake_generate(story_key, jira_data, epic_key, figma_data, num_sections, feature_dir,
                   app_design_doc="", knowledge_provider=None, api_contract="",
                   baseline_block="", hierarchy_screenshots=(), sheet_dir=None):
    # Same contract as the real function: write this story's sheet where it is
    # told to (sheet_dir), return (path, cases) with story-prefixed ids.
    from utils.run_layout import artifact_path
    cases = [{"id": f"{story_key}-TC-001", "name": f"Check {story_key}",
              "_story_key": story_key}]
    out = artifact_path(sheet_dir or feature_dir, f"{epic_key}_{story_key}_TestCases.xlsx")
    tw._write_excel(cases, out, epic_key)
    return out, cases


def _run(tmp_path, monkeypatch, stories):
    monkeypatch.setattr(tw, "fetch_jira_story", _fake_story)
    monkeypatch.setattr(tw, "_generate_one_story", _fake_generate)
    return tw.run(stories, "EP-1", "", tmp_path)


def _visible_sheets(run_dir):
    return sorted(p.name for p in (run_dir / "1_testcases").glob("*.xlsx"))


def test_several_stories_leave_exactly_one_visible_sheet(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, ["S-1", "S-2", "S-3"])

    assert _visible_sheets(tmp_path) == ["EP-1_TestCases.xlsx"]


def test_the_final_sheet_holds_every_story_s_cases(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, ["S-1", "S-2", "S-3"])

    ws = openpyxl.load_workbook(tmp_path / "1_testcases" / "EP-1_TestCases.xlsx").active
    ids = [row[-2] for row in ws.iter_rows(min_row=2, values_only=True)]
    assert sorted(ids) == ["S-1-TC-001", "S-2-TC-001", "S-3-TC-001"]


def test_per_story_sheets_are_kept_but_hidden(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, ["S-1", "S-2"])

    hidden = tmp_path / "_internal" / "per_story" / "1_testcases"
    assert sorted(p.name for p in hidden.glob("*.xlsx")) == [
        "EP-1_S-1_TestCases.xlsx", "EP-1_S-2_TestCases.xlsx"]


def test_a_single_story_s_own_sheet_is_the_final_output(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, "S-1")

    assert _visible_sheets(tmp_path) == ["EP-1_S-1_TestCases.xlsx"]
    assert not (tmp_path / "_internal" / "per_story").exists()
