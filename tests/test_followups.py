"""Follow-ups to the engine review; each test opens with the failure it prevents."""

import pathlib
import re

from agents import coding_prompt
from qa_pipeline.orchestrator import stages
from utils.case_index import case_id_from_title

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_coding_prompt_snapshot_is_the_compact_form_not_json_inside_json():
    # A role locator was serialised as a JSON string inside the JSON element,
    # repeating role+name with escaped quotes on every row.
    snap = {"url": "u", "raw_snapshot": "x" * 500, "elements": [
        {"tag": "button", "role": "button", "name": "Save", "id": None,
         "locatorType": "role", "locator": '{"role":"button","name":"Save"}'}]}
    groups = coding_prompt.dedupe_snapshots({"TC-1": snap, "TC-2": dict(snap)})
    assert len(groups) == 1 and groups[0]["test_case_ids"] == ["TC-1", "TC-2"]
    text = coding_prompt.compact_json(groups)
    assert "raw_snapshot" not in text and '\\"role\\"' not in text


def test_utils_no_longer_imports_the_engine_package():
    # utils/ must import nothing from qa_pipeline/ (CLAUDE.md layering rule).
    offenders = []
    for path in (_ROOT / "utils").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*(from|import)\s+qa_pipeline", line):
                offenders.append(f"{path.name}:{n}")
    assert offenders == []


def test_the_case_id_parser_still_keeps_the_story_half_of_the_id():
    assert case_id_from_title("VENDOR-TC-001 does x") == "VENDOR-TC-001"
    assert case_id_from_title("PROJ-68308-TC-012 does x") == "PROJ-68308-TC-012"


def test_a_story_that_produced_no_cases_is_named_in_the_stage_summary():
    # The failure list lived only in a JSON file; the stage line said success.
    stage = next(s for s in stages.build() if s.name == "testcase_writer")
    line = stage.summarize({"cases": [{}], "sheet": "S.xlsx", "failed_stories": ["PROJ-9"]})
    assert "INCOMPLETE" in line and "PROJ-9" in line
    assert "INCOMPLETE" not in stage.summarize({"cases": [{}], "sheet": "S.xlsx"})
