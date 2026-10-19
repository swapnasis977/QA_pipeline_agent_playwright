"""The Executor's infra-failure path: it must produce a report, not a crash.

`executor.run` deliberately RETURNS its failures instead of raising, because
"no live test environment" is a normal outcome the orchestrator checks for.
Two defects broke that contract in opposite directions:

1. THE BRANCH NEVER RAN. `_run_once` assigned `json_report_path` inside its
   own body, which made the name local to the closure for the whole function.
   When Playwright failed BEFORE writing a report — config error, missing
   browser binaries, bad testMatch — no candidate existed, nothing was
   assigned, and the very next line read it: UnboundLocalError. That is a
   NameError subclass, so neither `except OSError` nor `except
   TimeoutExpired` caught it; it escaped `run()` entirely. The carefully
   written "No JSON report produced — check the json reporter's outputFile"
   message had never once been emitted.

2. THE FAILURE READ AS A CLEAN RUN. `adapters.normalize_run_result` set
   `ran = returncode is not None`. A config error exits NON-ZERO, so it has a
   returncode, so `ran` was True with zero cases. `stage_execute` checks only
   `ran`, so the pipeline recorded "0 passed", handed the Reviewer an empty
   result, and exited 0. `failure_kind` was computed correctly at every layer
   and consulted by nobody — a green run against a broken environment, which
   is the same silent-success hole `stage_execute`'s own "planned but no
   specs" guard exists to close one stage earlier.

Run with:  python -m pytest tests/test_executor_infra_failures.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from agents import executor  # noqa: E402
from qa_pipeline.agents import adapters  # noqa: E402


class _Completed:
    """Stands in for subprocess.CompletedProcess."""

    def __init__(self, returncode=1, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "test-results").mkdir()
    return tmp_path


def _patch(monkeypatch, completed, writes_report=None):
    """Runs the Executor without Node, spawning nothing."""
    monkeypatch.setattr(executor.shutil, "which", lambda _n: "npx")

    def fake_run(*_a, **_kw):
        if writes_report is not None:
            path, payload = writes_report
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        return completed
    monkeypatch.setattr(executor, "_run_capturing", fake_run)   # the seam; subprocess is an implementation detail


# --- 1. no report produced -------------------------------------------------

def test_no_report_produced_returns_an_infra_report_instead_of_crashing(
        repo, tmp_path, monkeypatch):
    """The regression test for the UnboundLocalError."""
    _patch(monkeypatch, _Completed(returncode=1, stderr="config error: bad testMatch"))

    report = executor.run(repo, env_config={}, feature_dir=tmp_path / "feat")

    assert report["failure_kind"] == "infra"
    assert "config error" in report["error"]
    assert report["returncode"] == 1


def test_the_infra_report_is_written_to_disk(repo, tmp_path, monkeypatch):
    """The crash also meant nothing was persisted — the caller got an
    exception where it expected a file it could read back."""
    _patch(monkeypatch, _Completed(returncode=1, stderr="boom"))
    feature_dir = tmp_path / "feat"

    executor.run(repo, env_config={}, feature_dir=feature_dir)

    written = json.loads((feature_dir / "3_results" / "run_results.json").read_text(encoding="utf-8"))
    assert written["failure_kind"] == "infra"


def test_a_stderrless_failure_still_explains_where_it_looked(repo, tmp_path, monkeypatch):
    """The message that had never been reachable."""
    _patch(monkeypatch, _Completed(returncode=1, stderr=""))

    report = executor.run(repo, env_config={}, feature_dir=tmp_path / "feat")

    assert "No JSON report produced" in report["error"]
    assert "results.json" in report["error"]


# --- 2. a real report still works -----------------------------------------

def test_a_real_report_is_read_and_marked_as_test_results(repo, tmp_path, monkeypatch):
    payload = {"suites": [{"file": "a.spec.ts", "specs": []}], "stats": {"expected": 1}}
    _patch(monkeypatch, _Completed(returncode=0),
           writes_report=(repo / "test-results" / "results.json", payload))

    report = executor.run(repo, env_config={}, feature_dir=tmp_path / "feat")

    assert report["failure_kind"] == "test_results"
    assert report["suites"] == payload["suites"]


def test_the_legacy_root_report_location_still_works(repo, tmp_path, monkeypatch):
    """Second candidate: a workspace scaffolded before the outputFile move."""
    payload = {"suites": [], "stats": {}}
    _patch(monkeypatch, _Completed(returncode=0),
           writes_report=(repo / "playwright-report.json", payload))

    report = executor.run(repo, env_config={}, feature_dir=tmp_path / "feat")

    assert report["failure_kind"] == "test_results"


# --- 3. "No tests found" is a broken run, not a clean one ------------------
#
# Found by actually running the Executor against the real workspace rather
# than by reading it: Playwright writes a VALID report for a collection
# failure — suites: [], stats.expected: 0, and the reason in top-level
# `errors`. A report existing was enough to classify it test_results, so
# `ran` came back True with zero cases and the run reported success having
# verified nothing.

def test_no_tests_collected_with_an_error_is_an_infra_failure(
        repo, tmp_path, monkeypatch):
    payload = {"suites": [], "stats": {"expected": 0, "unexpected": 0},
               "errors": [{"message": "Error: No tests found.\nMake sure that "
                                      "arguments are regular expressions..."}]}
    _patch(monkeypatch, _Completed(returncode=1),
           writes_report=(repo / "test-results" / "results.json", payload))

    report = executor.run(repo, env_config={}, feature_dir=tmp_path / "feat")

    assert report["failure_kind"] == "infra"
    assert "No tests found" in report["error"]
    assert adapters.normalize_run_result(report)["ran"] is False


def test_an_empty_suite_with_no_error_is_left_alone(repo, tmp_path, monkeypatch):
    """No `errors` means Playwright had nothing to complain about — that is
    the transient-collection-race shape the retry above owns, and it must not
    be reclassified out from under it."""
    payload = {"suites": [], "stats": {"expected": 0}}
    _patch(monkeypatch, _Completed(returncode=0),
           writes_report=(repo / "test-results" / "results.json", payload))

    report = executor.run(repo, env_config={}, feature_dir=tmp_path / "feat")

    assert report["failure_kind"] == "test_results"


def test_a_run_that_collected_tests_is_never_reclassified(repo, tmp_path, monkeypatch):
    """Top-level `errors` can accompany a real run too. Suites being
    non-empty is what proves tests were actually collected."""
    payload = {"suites": [{"file": "a.spec.ts", "specs": []}],
               "stats": {"expected": 1}, "errors": [{"message": "a worker warning"}]}
    _patch(monkeypatch, _Completed(returncode=1),
           writes_report=(repo / "test-results" / "results.json", payload))

    report = executor.run(repo, env_config={}, feature_dir=tmp_path / "feat")

    assert report["failure_kind"] == "test_results"
    assert adapters.normalize_run_result(report)["ran"] is True


# --- 4. an infra failure must not read as a run that happened -------------

def test_an_infra_failure_with_a_returncode_is_not_ran():
    """`ran` is the flag every caller branches on. A config error exits
    non-zero, so returncode alone cannot distinguish it from a test run."""
    result = adapters.normalize_run_result(
        {"returncode": 1, "failure_kind": "infra", "error": "config error"})
    assert result["ran"] is False


def test_a_genuine_test_failure_still_counts_as_ran():
    """The case that must NOT regress: tests ran and some failed. Exit code
    is non-zero here too, and this is a real result the Reviewer must see."""
    result = adapters.normalize_run_result({
        "returncode": 1, "failure_kind": "test_results",
        "suites": [{"file": "a.spec.ts", "specs": [
            {"title": "TC-001 login", "file": "a.spec.ts", "tests": [
                {"results": [{"status": "failed", "duration": 10}]}]}]}],
    })
    assert result["ran"] is True
    assert [c["status"] for c in result["cases"]] == ["failed"]


def test_a_passing_run_counts_as_ran():
    result = adapters.normalize_run_result(
        {"returncode": 0, "failure_kind": "test_results", "suites": []})
    assert result["ran"] is True


def test_an_unreachable_environment_is_not_ran():
    """npx missing / bad repo_dir: returncode is None AND kind is infra."""
    result = adapters.normalize_run_result(
        {"returncode": None, "failure_kind": "infra", "error": "npx not found"})
    assert result["ran"] is False


def test_the_adapters_infra_marker_matches_the_executors():
    """`adapters` mirrors the constant rather than importing it, to keep the
    translation layer independent of the agent's internals. That is only safe
    while the two agree."""
    assert adapters._INFRA_FAILURE == executor._INFRA


def test_npx_missing_is_reported_not_raised(tmp_path, monkeypatch):
    """The whole reason `run` returns failures instead of raising."""
    monkeypatch.setattr(executor.shutil, "which", lambda _n: None)

    report = executor.run(tmp_path, env_config={}, feature_dir=tmp_path / "feat")

    assert report["failure_kind"] == "infra"
    assert report["returncode"] is None
    assert "npx not found" in report["error"]


def test_a_missing_repo_dir_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(executor.shutil, "which", lambda _n: "npx")

    report = executor.run(tmp_path / "nope", env_config={},
                          feature_dir=tmp_path / "feat")

    assert report["failure_kind"] == "infra"
    assert "does not exist" in report["error"]
