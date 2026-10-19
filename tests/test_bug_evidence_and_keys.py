"""A bug keeps its number between runs, and keeps its screenshot.

THE FAILURES.
  * Bugs were keyed on the Reviewer's free-text `reason`, which differs on every
    run, so "still open" almost never fired and each run issued a new BUG number
    for the same defect.
  * Heal re-runs make Playwright empty `test-results/`, so an unhealed failure
    reached the Bug Reporter with "none captured".
  * The Bug Reporter read the raw `classification`, the heal stage read the
    normalized `verdict`; a case with an unfamiliar verdict was healed and then
    missing from the bug list.

Run with:  python -m pytest tests/test_bug_evidence_and_keys.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import bug_reporter  # noqa: E402
from qa_pipeline.orchestrator.stages import _archive_evidence  # noqa: E402


def test_the_same_failure_worded_differently_keeps_its_bug_number():
    first = {"test_case_id": "TC-1", "reason": "The score was wrong this time",
             "citation": "Expected 80 received 79 at line 12"}
    again = {"test_case_id": "TC-1", "reason": "A totally different explanation",
             "citation": "Expected 81 received 70 at line 14"}
    assert bug_reporter._bug_key(first) == bug_reporter._bug_key(again)


def test_a_different_error_is_a_different_bug():
    one = {"test_case_id": "TC-1", "citation": "Timeout waiting for locator #brightness"}
    two = {"test_case_id": "TC-1", "citation": "Expected 80 received 79"}
    assert bug_reporter._bug_key(one) != bug_reporter._bug_key(two)


def test_screenshots_survive_a_rerun_that_empties_test_results(tmp_path):
    shot = tmp_path / "test-results" / "case-1" / "failed.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"png")
    report = {"suites": [{"specs": [{"title": "TC-1 shows score", "tests": [
        {"results": [{"attachments": [{"name": "screenshot", "path": str(shot),
                                       "contentType": "image/png"}]}]}]}]}]}
    feature = tmp_path / "feature"
    feature.mkdir()
    (feature / "run_results.json").write_text(json.dumps(report), encoding="utf-8")
    ctx = SimpleNamespace(artifact=lambda name: feature / name)

    assert _archive_evidence(ctx) == 1
    shot.unlink()  # the heal re-run clears test-results/

    rewritten = json.loads((feature / "run_results.json").read_text(encoding="utf-8"))
    kept = Path(rewritten["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"][0]["path"])
    assert kept.is_file() and kept.read_bytes() == b"png"


def test_a_report_with_no_attachments_changes_nothing(tmp_path):
    feature = tmp_path
    (feature / "run_results.json").write_text(json.dumps({"suites": []}), encoding="utf-8")
    assert _archive_evidence(SimpleNamespace(artifact=lambda n: feature / n)) == 0
