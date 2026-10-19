"""A failing test must end up healed, listed as a bug, or known flaky — never lost.

THE FAILURES.
  * An unknown or missing Reviewer verdict became `fixable_test_issue`, so a
    product failure the model mislabelled was sent to the Self-Healer.
  * A failing case the Reviewer simply did not mention vanished: not healed,
    not in the bug list.
  * Playwright retries once under CI, and every ATTEMPT became a row. A test
    that failed then passed counted as a failure AND a pass; a heal that passed
    on retry was judged to have failed, which blocklisted its good locator.

Run with:  python -m pytest tests/test_every_failure_is_classified.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.agents import adapters  # noqa: E402
from qa_pipeline.orchestrator.stages import _classify_every_failure, heal_succeeded  # noqa: E402


def _verdict(raw):
    return adapters.normalize_classification(
        {"test_case_id": "X-TC-1", "verdict": raw, "reason": "r"})["verdict"]


def test_an_unknown_verdict_is_surfaced_as_a_bug_not_healed():
    assert _verdict("mystery") == "genuine_bug"
    assert _verdict("") == "genuine_bug"
    entry = adapters.normalize_classification({"test_case_id": "X-TC-1", "reason": "boom"})
    assert "needs a person to triage" in entry["reason"]


def test_obvious_spellings_still_map_to_the_right_verdict():
    assert _verdict("flaky") == "known_flaky"
    assert _verdict("fixable") == "fixable_test_issue"
    assert _verdict("genuine_bug") == "genuine_bug"


def _report(*attempts_per_test):
    tests = []
    for title, statuses, final in attempts_per_test:
        tests.append({"title": title, "status": final, "tests": [{
            "status": final,
            "results": [{"status": s, "error": {"message": "boom"}} for s in statuses]}]})
    return {"suites": [{"file": "a.spec.ts", "specs": [
        {"title": t["title"], "file": "a.spec.ts", "tests": t["tests"]} for t in tests]}]}


def test_a_retried_test_is_one_case_judged_by_its_final_attempt():
    report = _report(("PROJ-1-TC-1 retried", ["failed", "passed"], "flaky"))
    cases = adapters._flatten_cases(report)
    assert len(cases) == 1 and cases[0]["status"] == "passed" and cases[0]["flaky"] is True


def test_a_heal_that_passes_on_retry_counts_as_fixed():
    report = _report(("PROJ-1-TC-1 retried", ["failed", "passed"], "flaky"))
    rerun = {"cases": adapters._flatten_cases(report), "returncode": 0}
    assert heal_succeeded(rerun, ["PROJ-1-TC-1"]) is True


def test_a_test_that_fails_every_attempt_is_still_one_failing_case():
    report = _report(("PROJ-1-TC-2 broken", ["failed", "failed"], "unexpected"))
    cases = adapters._flatten_cases(report)
    assert len(cases) == 1 and cases[0]["status"] == "failed"


def test_a_failing_case_the_reviewer_skipped_is_listed_for_triage():
    report = _report(("PROJ-1-TC-1 ok", ["passed"], "expected"),
                     ("PROJ-1-TC-2 broken", ["failed"], "unexpected"),
                     ("PROJ-1-TC-3 broken", ["failed"], "unexpected"))
    reviewed = [adapters.normalize_classification(
        {"test_case_id": "PROJ-1-TC-2", "verdict": "fixable_test_issue", "reason": "x"})]
    result = _classify_every_failure(report, reviewed)
    by_id = {c["test_case_id"]: c["verdict"] for c in result}
    assert by_id == {"PROJ-1-TC-2": "fixable_test_issue", "PROJ-1-TC-3": "genuine_bug"}


def test_nothing_is_added_when_every_failure_was_classified():
    report = _report(("PROJ-1-TC-2 broken", ["failed"], "unexpected"))
    reviewed = [adapters.normalize_classification(
        {"test_case_id": "PROJ-1-TC-2", "verdict": "genuine_bug", "reason": "x"})]
    assert len(_classify_every_failure(report, reviewed)) == 1


def test_a_case_id_with_a_middle_segment_is_read_whole():
    """`PROJ-68294-AUTO-TC-019` was read as just `PROJ-68294`, so every stage that
    joins on the id (healing, bug reports, screenshots, ledger promotion)
    matched the story key instead of the case."""
    assert adapters._case_id_from_title("PROJ-68294-AUTO-TC-019 Verify sliders") == "PROJ-68294-AUTO-TC-019"
    assert adapters._case_id_from_title("PROJ-68308-TC-012 x") == "PROJ-68308-TC-012"
    assert adapters._case_id_from_title("VENDOR-TC-001 x") == "VENDOR-TC-001"
