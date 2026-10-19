"""A green run must be able to promote the locators it proved.

THE FAILURE. Playwright's JSON report names a spec relative to `testDir`
(`e2e/a.spec.ts`) while a generated file is recorded repo-relative
(`tests/e2e/a.spec.ts`). `from_run` compared the two exactly, so nothing ever
matched: every file counted as "skipped_failing" and no locator was ever
promoted to `confirmed`, however many runs came back green.

Run with:  python -m pytest tests/test_ledger_learns_from_green_runs.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.brain.writeback import _is_passing, _passing_files  # noqa: E402


def test_a_testdir_relative_report_path_matches_the_repo_relative_file():
    report = {"cases": [{"file": "e2e/a.spec.ts", "status": "passed"}]}
    assert _is_passing("tests/e2e/a.spec.ts", _passing_files(report))


def test_the_same_path_still_matches():
    assert _is_passing("tests/e2e/a.spec.ts", {"tests/e2e/a.spec.ts"})


def test_windows_separators_match():
    backslash = chr(92)
    assert _is_passing(backslash.join(["tests", "e2e", "a.spec.ts"]), {"e2e/a.spec.ts"})

def test_a_different_file_with_a_similar_tail_does_not_match():
    assert not _is_passing("tests/e2e/b.spec.ts", {"e2e/a.spec.ts"})
    assert not _is_passing("tests/e2e/xa.spec.ts", {"a.spec.ts"})


def test_a_file_with_a_failing_case_is_not_passing():
    report = {"cases": [{"file": "e2e/a.spec.ts", "status": "passed"},
                        {"file": "e2e/a.spec.ts", "status": "failed"}]}
    assert not _is_passing("tests/e2e/a.spec.ts", _passing_files(report))
