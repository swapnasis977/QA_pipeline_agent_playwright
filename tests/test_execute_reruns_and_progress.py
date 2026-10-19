"""`execute` must not report an environment hiccup as a defect, must not let an old attempt linger
in the report, and must not go silent for an hour.

The real failures these prevent (all seen on the admin tool run):
  * 12 of 17 failures were `page.goto` timeouts under 4 workers; 11 passed when run alone.
  * a killed run left half its results behind; a later targeted re-run was appended to them, and
    the report listed the same tests twice with totals that matched no run.
  * nothing printed until the whole suite ended.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from qa_pipeline import execute_suite, execute_support


def _case(i, status, message=""):
    return {"test_case_id": f"TC-{i}", "status": status, "error_message": message, "attempts": 1}


def test_only_environment_errors_are_retried_in_infra_mode_but_every_failure_in_all_mode():
    cases = [_case(1, "failed", "TimeoutError: page.goto: Timeout 30000ms exceeded."),
             _case(2, "failed", "expect(locator).toBeVisible() failed"),
             _case(3, "passed"), _case(4, "skipped"),
             _case(5, "failed", "net::ERR_TOO_MANY_REDIRECTS at https://x")]
    assert execute_support.rerun_candidates(cases, "infra") == ["TC-1", "TC-5"]
    assert execute_support.rerun_candidates(cases, "all") == ["TC-1", "TC-2", "TC-5"]
    assert execute_support.rerun_candidates(cases, "none") == []


def test_a_real_defect_that_fails_again_is_never_hidden_by_the_rerun():
    first = {"cases": [_case(1, "failed", "timeout"), _case(2, "failed", "real defect")]}
    second = {"cases": [_case(1, "passed"), _case(2, "failed", "real defect")]}
    merged, flaky = execute_support.merge_rerun(first, second)
    by_id = {c["test_case_id"]: c for c in merged["cases"]}
    assert flaky == ["TC-1"]
    assert by_id["TC-1"]["status"] == "passed" and by_id["TC-1"]["flaky"] is True
    assert by_id["TC-2"]["status"] == "failed" and by_id["TC-2"]["error_message"] == "real defect"


def _write_result(folder, name, start, status="passed", attachment=None):
    (folder / f"{name}-{start}-result.json").write_text(json.dumps({
        "name": name, "historyId": name, "start": start, "stop": start + 1000, "status": status,
        "attachments": [{"source": attachment}] if attachment else []}))
    if attachment:
        (folder / attachment).write_text("evidence")


def test_a_rerun_replaces_the_earlier_attempt_so_each_test_appears_once(tmp_path):
    _write_result(tmp_path, "TC-1 flaky", 1000, "failed", attachment="old.png")
    _write_result(tmp_path, "TC-1 flaky", 9000, "passed")
    _write_result(tmp_path, "TC-2 stable", 2000, "passed")
    assert execute_support.dedupe_allure_results(tmp_path) == 1
    names = sorted(json.loads(p.read_text())["status"] for p in tmp_path.glob("*-result.json"))
    assert names == ["passed", "passed"]
    assert not (tmp_path / "old.png").exists()          # the stale attempt's evidence goes with it


def test_progress_prints_one_line_per_test_and_a_total_and_never_repeats_a_test(tmp_path):
    lines: list[str] = []
    watcher = execute_support.ProgressWatcher(tmp_path, out=lines.append)
    _write_result(tmp_path, "TC-1 first", 1000, "passed")
    _write_result(tmp_path, "TC-2 second", 2000, "failed")
    assert watcher.poll() == 2
    assert watcher.poll() == 0
    text = "\n".join(lines)
    assert "[PASS  ] TC-1 first" in text and "[FAIL  ] TC-2 second" in text
    assert "finished 2 | pass 1 | fail 1" in text


def test_progress_is_silent_when_there_is_no_results_folder(tmp_path):
    lines: list[str] = []
    assert execute_support.ProgressWatcher(tmp_path / "missing", out=lines.append).poll() == 0
    assert lines == []


class _Profile:
    def __init__(self, tmp_path):
        self.workspace = tmp_path / "ws"
        self.context_root = tmp_path / "runs"
        self.workspace.mkdir(exist_ok=True)
        (self.workspace / "playwright.config.ts").write_text("export default {};")

    def get(self, key, default=None):
        return default


def _args(**over):
    base = dict(workers=None, shard=None, grep=None, retries=0, allure=False, allow_failures=False,
                rerun_failed="all", progress=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_a_flaky_failure_turns_the_suite_green_but_is_listed(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_execute(repo, feature, **kwargs):
        calls.append(kwargs)
        if kwargs.get("grep_ids"):
            return {"ran": True, "cases": [_case(1, "passed")]}
        return {"ran": True, "cases": [_case(0, "passed"), _case(1, "failed", "page.goto timeout")]}

    monkeypatch.setattr("qa_pipeline.agents.adapters.execute", fake_execute)
    code = execute_suite.cmd_execute(_args(), _Profile(tmp_path))
    out = capsys.readouterr().out
    assert code == 0
    assert calls[1]["workers"] == 1 and calls[1]["grep_ids"] == ["TC-1"] and calls[1]["retries"] == 0
    assert calls[1]["fresh_reports"] is False           # the rerun must not wipe the first results
    assert "Flaky (failed, then passed on the re-run): TC-1" in out


def test_a_failure_that_repeats_still_exits_6(tmp_path, monkeypatch):
    monkeypatch.setattr("qa_pipeline.agents.adapters.execute",
                        lambda *a, **k: {"ran": True, "cases": [_case(1, "failed", "real defect")]})
    assert execute_suite.cmd_execute(_args(), _Profile(tmp_path)) == execute_suite.EXIT_TESTS_FAILING


def test_known_issues_are_listed_apart_from_unexplained_failures(tmp_path, monkeypatch, capsys):
    case = {**_case(1, "failed", "expected 0"), "issue": "Zoom is recorded in history"}
    monkeypatch.setattr("qa_pipeline.agents.adapters.execute",
                        lambda *a, **k: {"ran": True, "cases": [case, _case(2, "failed", "boom")]})
    execute_suite.cmd_execute(_args(rerun_failed="none"), _Profile(tmp_path))
    out = capsys.readouterr().out
    assert "KNOWN ISSUE TC-1: Zoom is recorded in history" in out
    assert "FAILED TC-2: boom" in out


def test_a_run_cut_short_still_reports_what_it_finished(tmp_path, monkeypatch, capsys):
    profile = _Profile(tmp_path)
    folder = profile.workspace / "allure-results"
    folder.mkdir()
    _write_result(folder, "TC-1 done", 1000, "passed")
    _write_result(folder, "TC-2 done", 2000, "failed")
    monkeypatch.setattr("qa_pipeline.agents.adapters.execute",
                        lambda *a, **k: {"ran": False, "error": "Execution timed out", "cases": []})
    assert execute_suite.cmd_execute(_args(), profile) == execute_suite.EXIT_FAILED
    assert "2 test(s) had finished: 1 passed, 1 failed" in capsys.readouterr().out


def test_the_profile_can_set_the_time_limit_but_an_operator_variable_wins(monkeypatch):
    class P:
        def get(self, key, default=None):
            return {"execution.timeout_minutes": 120}.get(key, default)

    for name in ("EXECUTOR_TIMEOUT_SECONDS", "QA_GLOBAL_TIMEOUT_MS"):
        monkeypatch.setenv(name, "x")       # so the test's own writes are undone afterwards
        monkeypatch.delenv(name)
    execute_suite.apply_timeouts(P())
    import os
    assert os.environ["EXECUTOR_TIMEOUT_SECONDS"] == "7200"
    assert os.environ["QA_GLOBAL_TIMEOUT_MS"] == str(115 * 60 * 1000)

    monkeypatch.setenv("EXECUTOR_TIMEOUT_SECONDS", "60")
    monkeypatch.delenv("QA_GLOBAL_TIMEOUT_MS")
    execute_suite.apply_timeouts(P())
    assert os.environ["EXECUTOR_TIMEOUT_SECONDS"] == "60"


def test_script_errors_thrown_by_the_application_are_listed_after_the_run(tmp_path, monkeypatch, capsys):
    cases = [{**_case(i, "passed"), "app_errors": ["Cannot read properties of undefined"]} for i in range(3)]
    monkeypatch.setattr("qa_pipeline.agents.adapters.execute", lambda *a, **k: {"ran": True, "cases": cases})
    execute_suite.cmd_execute(_args(), _Profile(tmp_path))
    assert "App script error seen in 3 test(s): Cannot read properties of undefined" in capsys.readouterr().out


def test_progress_ignores_results_left_by_an_earlier_run(tmp_path):
    """A targeted re-run keeps the old results; counting them showed '93 finished' for a 12-test run."""
    _write_result(tmp_path, "TC-0 from an earlier run", 500, "passed")
    lines: list[str] = []
    watcher = execute_support.ProgressWatcher(tmp_path, out=lines.append)
    _write_result(tmp_path, "TC-1 this run", 9000, "failed")
    assert watcher.poll() == 1
    text = "\n".join(lines)
    assert "TC-1 this run" in text and "earlier run" not in text
    assert "finished 1 | pass 0 | fail 1" in text
