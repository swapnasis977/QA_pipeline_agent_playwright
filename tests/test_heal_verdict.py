"""A heal is verified against the CASES it healed, not the process exit code.

Heals are grouped by file on purpose — several cases sharing one broken login
flow must cost one heal and one verification, not five of each. But the
verification re-run executes the whole spec, and `success = returncode == 0`
then answers a different question than the one asked.

Two ways that hurt, both silent:

  - A heal that genuinely fixed its case was recorded as still-failing
    because some UNRELATED test in the same file was also red.
  - Worse, `mark_heal_result(success=False)` then BLOCKLISTED the locator fix
    that had actually worked. The memory learned the opposite of the truth
    and would refuse that fix on every future run.

Run with:  python -m pytest tests/test_heal_verdict.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.orchestrator.stages import heal_succeeded  # noqa: E402


def _case(case_id, status):
    return {"test_case_id": case_id, "status": status}


def test_the_healed_case_passing_is_a_success():
    rerun = {"returncode": 0, "cases": [_case("TC-001", "passed")]}
    assert heal_succeeded(rerun, ["TC-001"]) is True


def test_an_unrelated_failure_in_the_same_spec_does_not_sink_the_heal():
    """The defect, in miniature: exit code 1, but OUR case passed."""
    rerun = {"returncode": 1, "cases": [
        _case("TC-001", "passed"),
        _case("TC-009", "failed"),   # a different case, not being healed
    ]}
    assert heal_succeeded(rerun, ["TC-001"]) is True


def test_the_healed_case_still_failing_is_a_failure():
    rerun = {"returncode": 1, "cases": [
        _case("TC-001", "failed"), _case("TC-009", "passed")]}
    assert heal_succeeded(rerun, ["TC-001"]) is False


def test_every_case_in_a_grouped_heal_must_pass():
    """One heal covering several cases is only fixed if it fixed all of
    them — a group shares a file and a patch, so a half-fix is not a fix."""
    rerun = {"returncode": 1, "cases": [
        _case("TC-001", "passed"), _case("TC-002", "failed")]}
    assert heal_succeeded(rerun, ["TC-001", "TC-002"]) is False


def test_all_of_a_group_passing_is_a_success():
    rerun = {"returncode": 0, "cases": [
        _case("TC-001", "passed"), _case("TC-002", "passed")]}
    assert heal_succeeded(rerun, ["TC-001", "TC-002"]) is True


def test_a_flaky_status_is_not_a_pass():
    """Playwright reports timedOut/interrupted separately; only "passed" is."""
    rerun = {"returncode": 1, "cases": [_case("TC-001", "timedOut")]}
    assert heal_succeeded(rerun, ["TC-001"]) is False


# --- the fallback ---------------------------------------------------------

def test_unattributable_cases_fall_back_to_the_exit_code():
    """When generated titles don't carry their case id, nothing can be
    matched — the Coding Agent already warns about that separately, and the
    exit code is then the best signal there is. This is the old behaviour,
    kept for exactly that state."""
    rerun = {"returncode": 0, "cases": [_case("", "passed")]}
    assert heal_succeeded(rerun, ["TC-001"]) is True

    rerun = {"returncode": 1, "cases": [_case("", "failed")]}
    assert heal_succeeded(rerun, ["TC-001"]) is False


def test_an_empty_report_falls_back_to_the_exit_code():
    assert heal_succeeded({"returncode": 0, "cases": []}, ["TC-001"]) is True
    assert heal_succeeded({"returncode": 1}, ["TC-001"]) is False
