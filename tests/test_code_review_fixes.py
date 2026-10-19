"""Regressions found by a read-through of the engine; each opens with the failure it prevents."""

import pytest

from agents.reviewer import _prematch_known_flaky
from agents.self_healer import _is_timeout_only_change
from qa_pipeline.verifiers import scaffold_check
from utils.llm_client import parse_json_response


def test_a_cut_off_answer_is_an_error_not_an_empty_result(tmp_path):
    # The scan used to step into the file's string contents, decode the `[]`
    # in generated TypeScript, and return it: zero files, no error, no retry.
    raw = '{"files":[{"path":"a.ts","content":"const x = [];\\ntest.use({});"},{"path":"b'
    with pytest.raises(ValueError):
        parse_json_response(raw, tmp_path, "coding_agent")


def test_json_after_a_prose_preamble_with_brackets_still_parses(tmp_path):
    raw = 'follows `[action] button.` ok\n{"patched_content":"x","locator_memory_update":{"a":1}}'
    assert parse_json_response(raw, tmp_path, "self_healer")["patched_content"] == "x"


def test_changing_an_index_or_count_is_not_mistaken_for_a_bigger_timeout():
    before = "await expect(rows).toHaveCount(3);\nawait rows.nth(1).click();"
    after = "await expect(rows).toHaveCount(4);\nawait rows.nth(2).click();"
    assert _is_timeout_only_change(before, after) is False


def test_raising_a_timeout_is_still_refused():
    before = "await expect(a).toBeVisible({ timeout: 20000 });"
    after = ("await expect(a).toBeVisible({ timeout: 30000 });\n"
             "await page.waitForLoadState('networkidle');")
    assert _is_timeout_only_change(before, after) is True


def test_a_case_that_failed_every_attempt_is_not_prelabelled_known_flaky():
    known = [{"test_case_id": "A-1", "reason": "Timeout waiting for locator Save"}]
    hard = {"test_case_id": "A-1", "error": "Timeout waiting for locator Save", "flaky": False}
    flaky = {"test_case_id": "A-1", "error": "Timeout waiting for locator Save", "flaky": True}
    prematched, remaining = _prematch_known_flaky([hard], known)
    assert prematched == [] and remaining == [hard]
    prematched, _ = _prematch_known_flaky([flaky, dict(flaky)], known)
    assert len(prematched) == 1


def test_a_leading_dot_dot_path_is_rejected_not_silently_normalised():
    # `.lstrip("./")` stripped characters, turning "../../x" into "x" and
    # hiding the traversal from the ".." check.
    assert scaffold_check.check_path("../../etc/x.spec.ts").status == "fail"
