"""The same bug must keep its number, and a fixed bug must say so.

The failure this prevents: a bug was keyed on the Reviewer model's prose, which differs
on every run, so the same defect got a new BUG-nn each time and "still open" almost
never fired. And a bug that had been fixed simply vanished from the list with no word -
nobody could tell "fixed" from "forgotten".
"""

import json

from agents import bug_reporter

ERROR = "Error: expect(locator).toHaveText(expected) failed\n  Expected: Saved\n  Received: Saving"


def _report(case_id, message=ERROR):
    return {"suites": [{"specs": [{"title": f"{case_id} saves", "tests": [
        {"results": [{"status": "failed", "error": {"message": message}}]}]}]}]}


def _review(tmp_path, *entries):
    path = tmp_path / "review_report.json"
    path.write_text(json.dumps({"failure_classification": [
        {"test_case_id": cid, "classification": "genuine_bug", "severity": "major",
         "reason": reason, "file": "tests/e2e/a.spec.ts"} for cid, reason in entries]}),
        encoding="utf-8")
    return path


def _run(tmp_path, entries, report):
    out = bug_reporter.run(_review(tmp_path, *entries), report, tmp_path / "run")
    return out.read_text(encoding="utf-8")


def test_the_same_bug_described_differently_keeps_its_number(tmp_path):
    first = _run(tmp_path, [("A-1", "The status text is wrong after saving.")], _report("A-1"))
    second = _run(tmp_path, [("A-1", "Saving leaves the label at a different value than expected.")],
                  _report("A-1"))

    assert "BUG-01 — A-1 (new)" in first
    assert "BUG-01 — A-1 (still open" in second          # same number, recognised as recurring
    assert "BUG-02" not in second


def test_a_different_error_is_a_different_bug(tmp_path):
    _run(tmp_path, [("A-1", "x")], _report("A-1"))
    other = _run(tmp_path, [("A-1", "x")], _report("A-1", "TimeoutError: locator.click: Timeout 15000ms"))
    assert "BUG-02 — A-1 (new)" in other


def test_numbers_in_the_error_do_not_split_one_bug_in_two(tmp_path):
    _run(tmp_path, [("A-1", "x")], _report("A-1", "Error: Timeout 15000ms exceeded"))
    again = _run(tmp_path, [("A-1", "x")], _report("A-1", "Error: Timeout 30000ms exceeded"))
    assert "(still open" in again


def test_a_bug_that_is_gone_is_reported_as_no_longer_failing(tmp_path):
    _run(tmp_path, [("A-1", "x"), ("A-2", "y")], _report("A-1"))
    later = _run(tmp_path, [("A-1", "x")], _report("A-1"))

    assert "No longer failing" in later
    assert "BUG-02 (A-2)" in later


def test_when_every_bug_is_fixed_the_report_still_says_which(tmp_path):
    _run(tmp_path, [("A-1", "x")], _report("A-1"))
    clean = _run(tmp_path, [], {"suites": []})

    assert "No genuine bugs" in clean and "BUG-01 (A-1)" in clean


def test_a_registry_from_the_old_scheme_is_retired_not_reported_as_fixed(tmp_path):
    feature = tmp_path / "run"
    old = feature / "_internal" / "bug_registry.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"A-1::oldsignature": 7}), encoding="utf-8")

    text = _run(tmp_path, [("A-1", "x")], _report("A-1"))

    assert "No longer failing" not in text            # not misreported as fixed
    assert "BUG-08" in text                           # numbering continues; 7 is never reused
