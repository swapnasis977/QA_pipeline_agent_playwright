"""The Executor must not report a broken run as a clean one.

Failures these tests exist to prevent:
  - A failed sign-in (the `setup` project) skipped every dependent test with no
    result, so the run showed "0 cases, nothing failed" and the pipeline went green.
  - `--grep=A|B` was handed to `npx.cmd`; on Windows cmd.exe reads `|` as a pipe,
    so a grouped heal verification ran the wrong thing.
  - A heal verified with retries could pass on attempt 2 and be called "fixed".
  - allure-results/ accumulated across runs, so a report mixed old and new.
"""

import json
from types import SimpleNamespace

from agents import executor


def _report(setup_status):
    return {"stats": {"expected": 0, "unexpected": 0}, "suites": [{
        "file": "auth.setup.ts",
        "specs": [{"title": "sign in", "file": "auth.setup.ts", "tests": [{
            "results": [{"status": setup_status,
                         "error": {"message": "Sign-in ended on https://idp/login\nstack..."}}]}]}],
    }, {
        # the dependent project's tests: skipped, NO results at all
        "file": "tests/e2e/a.spec.ts",
        "specs": [{"title": "PROJ-1-TC-001 x", "file": "tests/e2e/a.spec.ts",
                   "tests": [{"status": "skipped", "results": []}]}],
    }]}


def _run_with(monkeypatch, tmp_path, report, captured=None):
    repo = tmp_path / "repo"
    (repo / "test-results").mkdir(parents=True)

    def fake_run(cmd, *, cwd, timeout, env=None):
        if captured is not None:
            captured.append(cmd)
        (repo / "test-results" / "results.json").write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(executor, "_run_capturing", fake_run)
    monkeypatch.setattr(executor.shutil, "which", lambda name: f"/bin/{name}")
    return executor.run(repo, {}, tmp_path / "feature", test_files=None), repo


def test_a_failed_sign_in_is_an_infra_failure_not_an_empty_clean_run(monkeypatch, tmp_path):
    result, _ = _run_with(monkeypatch, tmp_path, _report("failed"))

    assert result["failure_kind"] == "infra"
    assert "Sign-in/setup failed" in result["error"]
    assert "Sign-in ended on" in result["error"]


def test_a_passing_sign_in_is_still_a_normal_result(monkeypatch, tmp_path):
    result, _ = _run_with(monkeypatch, tmp_path, _report("passed"))

    assert result["failure_kind"] == "test_results"


def test_playwright_is_launched_through_node_so_a_pipe_in_grep_is_not_a_shell_pipe(
        monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    cli = repo / "node_modules" / "@playwright" / "test" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(executor.shutil, "which", lambda name: f"/bin/{name}")

    cmd = executor._playwright_command(repo, "/bin/npx")

    assert cmd == ["/bin/node", str(cli), "test"]


def test_without_a_local_playwright_it_falls_back_to_npx(tmp_path):
    assert executor._playwright_command(tmp_path, "/bin/npx") == ["/bin/npx", "playwright", "test"]


def test_a_verification_run_can_forbid_retries(monkeypatch, tmp_path):
    captured = []
    repo = tmp_path / "repo"
    (repo / "test-results").mkdir(parents=True)

    def fake_run(cmd, *, cwd, timeout, env=None):
        captured.append(cmd)
        (repo / "test-results" / "results.json").write_text(
            json.dumps({"stats": {}, "suites": [{"file": "a.spec.ts"}]}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(executor, "_run_capturing", fake_run)
    monkeypatch.setattr(executor.shutil, "which", lambda name: f"/bin/{name}")
    executor.run(repo, {}, tmp_path / "f", grep_ids=["PROJ-1-TC-001"], retries=0)

    assert "--retries=0" in captured[0]


def test_a_fresh_run_clears_old_reports_but_a_heal_rerun_does_not(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    for name in ("allure-results", "playwright-report"):
        (repo / name).mkdir(parents=True)
        (repo / name / "old.json").write_text("{}", encoding="utf-8")
    (repo / "test-results").mkdir()

    def fake_run(cmd, *, cwd, timeout, env=None):
        (repo / "test-results" / "results.json").write_text(
            json.dumps({"stats": {}, "suites": [{"file": "a.spec.ts"}]}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(executor, "_run_capturing", fake_run)
    monkeypatch.setattr(executor.shutil, "which", lambda name: f"/bin/{name}")

    executor.run(repo, {}, tmp_path / "f", grep_ids=["X-1"])           # a heal re-run
    assert (repo / "allure-results" / "old.json").exists()

    executor.run(repo, {}, tmp_path / "f", fresh_reports=True)         # the first full run
    assert not (repo / "allure-results").exists()
    assert not (repo / "playwright-report").exists()
